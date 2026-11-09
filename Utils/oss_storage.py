"""Aliyun OSS delivery with bounded retries and verified uploads."""
import hashlib
import os
from pathlib import Path
import re
import time
from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import urlsplit

from webapp.config_schema import OSS_DEFAULTS, OSS_FIELDS, OSS_INTEGER_FIELDS, OSS_SECRETS


class OSSUploadError(RuntimeError):
    """An OSS failure must never trigger another YouTube provider/account."""
    retryable = True


def credentials(config):
    return tuple(config.get(key) or os.environ.get(env, '') for key, env in (
        ('oss_access_key_id', 'OSS_ACCESS_KEY_ID'),
        ('oss_access_key_secret', 'OSS_ACCESS_KEY_SECRET'),
        ('oss_security_token', 'OSS_SECURITY_TOKEN')))


def validate_oss(config):
    for key in OSS_FIELDS - OSS_INTEGER_FIELDS.keys():
        value = config.get(key, OSS_DEFAULTS[key])
        if not isinstance(value, str) or len(value) > 4096 or '\x00' in value:
            raise ValueError(f'{key} 必须为有效文本。')
    for key, (low, high) in OSS_INTEGER_FIELDS.items():
        value = config.get(key, OSS_DEFAULTS[key])
        if isinstance(value, bool) or not str(value).isdigit() or not low <= int(value) <= high:
            raise ValueError(f'{key} 必须为 {low}～{high} 的整数。')
        config[key] = int(value)
    endpoint = str(config.get('oss_endpoint', '')).strip().rstrip('/')
    if endpoint and '://' not in endpoint:
        endpoint = 'https://' + endpoint
    parsed = urlsplit(endpoint)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path or parsed.port):
        raise ValueError('OSS Endpoint 请填写 HTTPS 地域地址，例如 https://oss-cn-hangzhou.aliyuncs.com。')
    if not re.fullmatch(r'oss-[a-z0-9-]+\.aliyuncs\.com', parsed.hostname):
        raise ValueError('OSS Endpoint 必须是阿里云 OSS 地域地址，可使用 -internal 内网地址。')
    bucket = config.get('oss_bucket', '')
    if not isinstance(bucket, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket):
        raise ValueError('OSS Bucket 名称必须是 3～63 位小写字母、数字或连字符。')
    prefix = config.get('oss_prefix', 'videos')
    if (not isinstance(prefix, str) or len(prefix.encode('utf-8')) > 512
            or '\\' in prefix or any(ord(c) < 32 for c in prefix)
            or any(p in ('.', '..') for p in prefix.split('/'))):
        raise ValueError('OSS 对象前缀不能包含 ..、反斜杠或控制字符，长度不能超过 512 字节。')
    if not all(credentials(config)[:2]):
        raise ValueError('请填写 OSS AccessKey ID 和 Secret，或设置 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET 环境变量。')
    config['oss_endpoint'] = endpoint
    config['oss_prefix'] = prefix.strip('/')
    return config


def public_oss_config(config):
    result = dict(config)
    for key in OSS_SECRETS:
        result[key + '_configured'] = bool(config.get(key))
        result[key] = ''
    return result


def archive_key(config, identity):
    if config.get('storage_mode') != 'oss':
        return identity
    target = '\0'.join(str(config.get(k, '')) for k in ('oss_endpoint', 'oss_bucket', 'oss_prefix'))
    return 'oss:' + hashlib.sha256(target.encode()).hexdigest()[:16] + ':' + identity


class OSSStorage:
    def __init__(self, config):
        self.config = validate_oss(dict(config))
        self.prefix = '/'.join(filter(None, (self.config['oss_prefix'], config.get('_oss_namespace', ''))))
        import oss2
        access_id, secret, token = credentials(self.config)
        auth = oss2.StsAuth(access_id, secret, token) if token else oss2.Auth(access_id, secret)
        self.bucket = oss2.Bucket(auth, self.config['oss_endpoint'], self.config['oss_bucket'],
                                  connect_timeout=30, enable_crc=True)

    def upload_video(self, video, root, report=None, *, include_video=True):
        """Upload only this video's final files, never unrelated files or partials."""
        video, root = Path(video).resolve(), Path(root).resolve()
        video.relative_to(root)
        stem = video.stem
        candidates = [video] if include_video else []
        candidates += sorted(p for p in video.parent.iterdir() if p != video and
            p.name.startswith(stem + '.') and p.suffix.lower() in
            {'.json', '.jpg', '.jpeg', '.png', '.webp', '.vtt', '.srt', '.ass', '.lrc', '.ttml',
             '.srv1', '.srv2', '.srv3', '.json3', '.sbv', '.dfxp'})
        uploaded = []
        for path in candidates:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                raise OSSUploadError('OSS 上传文件路径无效，本地文件已保留。')
            key = '/'.join(filter(None, (self.prefix, path.relative_to(root).as_posix())))
            if len(key.encode('utf-8')) > 1023:
                raise OSSUploadError('OSS 对象名称超过 1023 字节，请缩短目录或对象前缀。')
            from webapp.local_processing import local_processing
            state = {}
            status = SimpleNamespace(state=state, save=lambda force=False: report(dict(state)) if report else None)
            guard = local_processing(self.config['_disk_state'], self.config, status, 'oss_upload',
                size=path.stat().st_size, resource='oss-upload-slots', limit=self.config['oss_upload_limit']) if self.config.get('_disk_state') else nullcontext()
            with guard:
                self._upload(path, key, report)
            uploaded.append({'key': key, 'size': path.stat().st_size})
        return uploaded

    def _upload(self, path, key, report):
        import oss2
        size = path.stat().st_size
        last_report = [0.0]
        samples = deque(maxlen=8)
        last_bytes = [0, None]
        started = time.monotonic()
        def progress(consumed, total):
            now = time.monotonic()
            if consumed < last_bytes[0] or consumed == 0:
                last_bytes[:] = [0, None]
            if consumed > last_bytes[0]:
                last_bytes[:] = [consumed, time.time()]
            if not samples or consumed < samples[-1][1] or consumed == 0:
                samples.clear()
            if not samples or now-samples[-1][0] >= .25 or consumed == total:
                samples.append((now, consumed))
            elapsed = now - samples[0][0]
            speed = (consumed-samples[0][1])/elapsed if elapsed >= .5 else None
            if report and (consumed in (0, total) or now - last_report[0] >= 1):
                last_report[0] = now
                report({'stage': 'uploading_oss', 'oss_uploaded_bytes': consumed,
                        'oss_total_bytes': total, 'oss_object': key, 'speed': speed,
                        'oss_last_bytes_at': last_bytes[1], 'eta': (total-consumed)/speed if speed else None})
        for attempt in range(3):
            try:
                progress(0, size)
                oss2.resumable_upload(self.bucket, key, str(path),
                    multipart_threshold=16 * 1024**2, part_size=self.config['oss_part_size_mb'] * 1024**2,
                    num_threads=self.config['oss_upload_threads'], progress_callback=progress)
                if self.bucket.head_object(key).content_length != size:
                    raise OSSUploadError('OSS 远端文件大小校验失败，本地文件已保留。')
                progress(size, size)
                from webapp.diagnostics import event
                event('oss_file_uploaded', file_bytes=size, elapsed_seconds=round(time.monotonic()-started, 3),
                      attempts=attempt+1, threads=self.config['oss_upload_threads'], part_size_mb=self.config['oss_part_size_mb'])
                return
            except (oss2.exceptions.OssError, OSSUploadError) as error:
                status = getattr(error, 'status', None)
                if status in (400, 401, 403, 404) or attempt == 2:
                    detail = f'（HTTP {status}）' if status else ''
                    failure = OSSUploadError(f'OSS 上传或校验失败{detail}，请检查地域、Bucket、授权和网络；本地文件已保留，可重试。')
                    failure.retryable = status not in (400, 401, 403, 404)
                    raise failure from None
                time.sleep(attempt + 1)
