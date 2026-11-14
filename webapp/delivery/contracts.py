"""Validate public requirements independently of executor configuration."""
import hashlib
import json
import re

KINDS = ('subtitle', 'video_only', 'audio_only', 'av')
LABELS = dict(zip(KINDS, ('字幕', '无声视频', '纯音频', '音视频成品')))
DEFAULT = {'schema_version': 1, 'outputs': ['av'], 'video': {'max_height': 1080, 'container': 'source'},
           'audio': {'format': 'source'}, 'av': {'container': 'auto'},
           'subtitles': {'mode': 'original', 'languages': ['zh-Hans'], 'format': 'srt',
                         'sources': ['manual', 'automatic'], 'language_policy': 'available_selected',
                         'missing_policy': 'review'}}


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def normalize(value=None):
    if value is None:
        value = DEFAULT
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('交付要求包含不支持的字段。')
    if type(value.get('schema_version', 1)) is not int or value.get('schema_version', 1) != 1:
        raise ValueError('不支持的交付要求版本。')
    outputs = value.get('outputs', DEFAULT['outputs'])
    if not isinstance(outputs, list) or not outputs or any(k not in KINDS for k in outputs if isinstance(k, str)) or any(not isinstance(k, str) for k in outputs):
        raise ValueError('请至少选择一种有效交付内容。')
    result = {'schema_version': 1, 'outputs': [k for k in KINDS if k in outputs]}
    for group in ('video', 'audio', 'av', 'subtitles'):
        supplied = value.get(group, {})
        if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT[group]):
            raise ValueError('交付格式包含不支持的字段。')
        result[group] = {**DEFAULT[group], **supplied}
        if group == 'subtitles' and 'languages' in supplied and 'mode' not in supplied:
            # Existing contracts keep their selected-language behavior and hash.
            result[group].pop('mode')
    video, audio, av, subs = (result[k] for k in ('video', 'audio', 'av', 'subtitles'))
    if type(video['max_height']) is not int or video['max_height'] not in (0, 360, 480, 720, 1080, 1440, 2160, 4320):
        raise ValueError('画质上限无效。')
    if video['container'] not in ('source', 'mp4') or audio['format'] not in ('source', 'mp3') or av['container'] not in ('auto', 'mp4', 'mkv'):
        raise ValueError('交付容器或音频格式无效。')
    languages = subs['languages']
    if subs.get('mode', 'selected') not in ('original', 'original_and_translated', 'selected'):
        raise ValueError('字幕下载模式无效。')
    if not isinstance(languages, list) or len(languages) > 8 or (not languages and subs.get('mode') != 'original') or any(not isinstance(s, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,30}', s) for s in languages):
        raise ValueError('字幕语言需要 1 到 8 个有效语言代码。')
    subs['languages'] = list(dict.fromkeys(languages))
    if subs['format'] not in ('srt', 'vtt') or subs['language_policy'] not in ('available_selected', 'all_selected') or subs['missing_policy'] != 'review':
        raise ValueError('字幕格式或缺失处理无效。')
    if not isinstance(subs['sources'], list) or not subs['sources'] or any(s not in ('manual', 'automatic') for s in subs['sources']):
        raise ValueError('请选择人工或自动字幕来源。')
    subs['sources'] = [s for s in ('manual', 'automatic') if s in subs['sources']]
    return result


def variant(spec, kind):
    # Output selection and contract revision do not alter an existing file's identity.
    options = spec['subtitles'] if kind == 'subtitle' else spec['audio'] if kind == 'audio_only' else {
        'video': spec['video'], **({'av': spec['av']} if kind == 'av' else {})}
    if kind == 'subtitle':
        options = {k: options[k] for k in ('format', 'sources')}
        # Original-only output must never reuse a prior machine translation.
        if spec['subtitles'].get('mode') in ('original', 'original_and_translated'):
            options['mode'] = spec['subtitles']['mode']
    return digest({'kind': kind, 'options': options})


def summary(spec):
    return ' + '.join(LABELS[k] for k in spec['outputs'])


def compatible(store, db):
    """Refuse enabling new requirements while older registered executors exist."""
    if not store.fleet:
        return
    from sqlalchemy import select
    from webapp.database import instances
    from werkzeug.exceptions import Conflict
    for row in db.execute(select(instances.c.config, instances.c.name).where(instances.c.role.in_(('worker', 'hybrid')))).mappings():
        if json.loads(row['config'] or '{}').get('delivery_schema_version', 0) < 1:
            raise Conflict('请先更新所有下载实例，再启用多类型交付；旧版本实例：' + row['name'])
