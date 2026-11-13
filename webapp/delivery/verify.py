"""Inspect streams and subtitle content before publishing an output."""
import json
import re
import subprocess
from pathlib import Path


class LocalDeliveryError(RuntimeError):
    """Local verification/conversion says nothing about source account health."""


def inspect(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise LocalDeliveryError('交付文件无法解析为有效媒体。')
    info = json.loads(result.stdout)
    streams = info.get('streams', [])
    return {s.get('codec_type') for s in streams}, info


def verify(path, kind):
    path = Path(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise LocalDeliveryError('交付文件不存在或为空。')
    if kind == 'subtitle':
        text = path.read_text(encoding='utf-8-sig')
        if not re.search(r'\d{2}:\d{2}[.,]\d{3}\s+-->\s+(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}', text):
            raise LocalDeliveryError('字幕没有可用的时间轴条目。')
        return
    types, info = inspect(path)
    expected = {'video'} if kind == 'video_only' else {'audio'} if kind == 'audio_only' else {'video', 'audio'}
    if types & {'video', 'audio'} != expected:
        raise LocalDeliveryError('交付文件的画面/音轨与客户要求不一致。')
    if float(info.get('format', {}).get('duration') or 0) <= 0:
        raise LocalDeliveryError('交付文件没有有效时长。')


def convert(inputs, target, kind, *, mp4=False, mp3=False):
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-y']
    for source in inputs:
        command += ['-i', str(source)]
    if kind == 'subtitle':
        command += ['-map', '0:s:0']
    elif kind == 'video_only':
        command += ['-map', '0:v:0', '-an', '-c:v', 'libx264' if mp4 else 'copy']
    elif kind == 'audio_only':
        command += ['-map', '0:a:0', '-vn', '-c:a', 'libmp3lame' if mp3 else 'copy']
    else:
        command += ['-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264' if mp4 else 'copy',
                    '-c:a', 'aac' if mp4 else 'copy']
    if target.suffix == '.mp4':
        command += ['-movflags', '+faststart']
    result = subprocess.run(command + [str(target)], capture_output=True, timeout=1800)
    if result.returncode:
        raise LocalDeliveryError('交付文件转换失败，请检查源轨道、格式与 FFmpeg 环境。')
    verify(target, kind)
