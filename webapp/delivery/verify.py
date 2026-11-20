"""Inspect streams and subtitle content before publishing an output."""
import json
import math
import re
import subprocess
from pathlib import Path
from PIL import Image, UnidentifiedImageError


class LocalDeliveryError(RuntimeError):
    """Local verification/conversion says nothing about source account health."""


class InvalidSourceMedia(LocalDeliveryError):
    """Repeating conversion cannot repair broken or missing source tracks."""
    retryable = False
    recovery = 'redownload'


def inspect(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode or result.stderr.strip():
        raise InvalidSourceMedia('媒体文件不完整或无法解析，请重新下载后处理。')
    info = json.loads(result.stdout)
    streams = info.get('streams', [])
    return {s.get('codec_type') for s in streams}, info


def verify(path, kind, *, media=None):
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise LocalDeliveryError('交付文件不存在或为空。')
    if kind == 'thumbnail':
        try:
            with Image.open(path) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise ValueError('unsupported image format')
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
            raise LocalDeliveryError('封面文件不是有效的 JPG、PNG 或 WebP 图片。') from error
        return
    if kind == 'metadata':
        try:
            info = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(info, dict) or info.get('schema_version') != 1 or not isinstance(info.get('id'), str) or not info['id'] or not isinstance(info.get('title'), str):
                raise ValueError('invalid metadata')
        except (ValueError, UnicodeError) as error:
            raise LocalDeliveryError('视频信息不是有效的 JSON 视频资料。') from error
        return
    if kind == 'subtitle':
        text = path.read_text(encoding='utf-8-sig')
        if not re.search(r'\d{2}:\d{2}[.,]\d{3}\s+-->\s+(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}', text):
            raise LocalDeliveryError('字幕没有可用的时间轴条目。')
        return
    types, info = media if media is not None else inspect(path)
    expected = {'video'} if kind == 'video_only' else {'audio'} if kind == 'audio_only' else {'video', 'audio'}
    if types & {'video', 'audio'} != expected:
        raise LocalDeliveryError('交付文件的画面/音轨与客户要求不一致。')
    try:
        duration = float(info.get('format', {}).get('duration') or 0)
    except (ValueError, TypeError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        raise LocalDeliveryError('交付文件没有有效时长。')
    return types, info


def media_output(inputs, kind, *, mp4=False, mp3=False, probes=None):
    """Inspect once; passthrough requires actual streams AND a suitable container."""
    probes = probes if probes is not None else [inspect(path) for path in inputs]
    required = [('video', probes[0])] if kind == 'video_only' else [('audio', probes[0])] if kind == 'audio_only' else [
        ('video', probes[0]), ('audio', probes[-1])]
    for track, probe in required:
        if track not in probe[0]:
            raise InvalidSourceMedia('源文件缺少所需的画面或音轨，请重新下载后处理。')
    types, info = probes[0]
    streams = info.get('streams', [])
    video = next((s.get('codec_name') for s in streams if s.get('codec_type') == 'video'), '')
    audio = next((s.get('codec_name') for s in streams if s.get('codec_type') == 'audio'), '')
    containers = set(info.get('format', {}).get('format_name', '').split(','))
    if kind == 'video_only':
        ext = '.mp4' if mp4 else Path(inputs[0]).suffix
        direct = (types == {'video'} and
                  (not mp4 or bool(containers & {'mp4', 'mov'}) and video in ('h264', 'hevc', 'av1', 'mpeg4')))
    elif kind == 'audio_only':
        ext = '.mp3' if mp3 or audio == 'mp3' else '.m4a' if audio in ('aac', 'alac') else '.webm' if audio in ('opus', 'vorbis') else '.mka'
        suitable = {'mp3'} if ext == '.mp3' else {'mov', 'mp4', 'm4a'} if ext == '.m4a' else {'webm'} if ext == '.webm' else {'matroska'}
        direct = (types == {'audio'} and bool(containers & suitable)
                  and (not mp3 or audio == 'mp3'))
    else:
        ext = '.mp4' if mp4 else '.mkv'
        direct = (len(set(map(str, inputs))) == 1 and types == {'video', 'audio'} and
                  (bool(containers & {'mp4', 'mov'}) and video in ('h264', 'hevc', 'av1', 'mpeg4')
                   and audio in ('aac', 'alac', 'mp3') if mp4 else bool(containers & {'matroska', 'webm'})))
    if direct:
        verify(inputs[0], kind, media=probes[0])
    codecs = [next((s.get('codec_name') for s in p[1].get('streams', []) if s.get('codec_type') == 'audio'), '') for p in probes]
    return dict(extension=ext, direct=direct, probes=probes,
                video_codec=video, audio_codec=codecs[-1] if kind == 'av' else audio)


def convert(inputs, target, kind, *, mp4=False, mp3=False, media=None):
    media = media or (media_output(inputs, kind, mp4=mp4, mp3=mp3) if kind != 'subtitle' else {})
    compatible_video = media.get('video_codec') in ('h264', 'hevc', 'av1', 'mpeg4')
    compatible_audio = media.get('audio_codec') in ('aac', 'alac', 'mp3')
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-y']
    for source in inputs:
        command += ['-i', str(source)]
    if kind == 'subtitle':
        command += ['-map', '0:s:0']
    elif kind == 'video_only':
        command += ['-map', '0:v:0', '-an', '-c:v', 'libx264' if mp4 and not compatible_video else 'copy']
    elif kind == 'audio_only':
        command += ['-map', '0:a:0', '-vn', '-c:a', 'libmp3lame' if mp3 and media.get('audio_codec') != 'mp3' else 'copy']
    else:
        command += ['-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264' if mp4 and not compatible_video else 'copy',
                    '-c:a', 'aac' if mp4 and not compatible_audio else 'copy']
    if target.suffix == '.mp4':
        command += ['-movflags', '+faststart']
    # Keep transcoding bounded on small servers; stream copy ignores this option.
    result = subprocess.run(command + ['-threads', '1', str(target)], capture_output=True, timeout=1800)
    if result.returncode:
        raise LocalDeliveryError('交付文件转换失败，请检查源轨道、格式与 FFmpeg 环境。')
    return verify(target, kind)
