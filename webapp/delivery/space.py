"""A file plan's space cost. No network probes or database work on this path."""
import math
from pathlib import Path

MIB = 1024 ** 2
GIB = 1024 ** 3


def positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else 0
    except (ValueError, TypeError):
        return 0


def codec_family(codec):
    """Normalize extractor codec labels only for space forecasts."""
    value = str(codec or '').lower()
    prefix = value.split('.', 1)[0]
    if prefix in ('avc1', 'avc3'):
        return 'h264'
    if prefix in ('hvc1', 'hev1'):
        return 'hevc'
    if prefix == 'av01':
        return 'av1'
    if prefix == 'mp4v':
        return 'mpeg4'
    if value.startswith('mp4a.40'):
        return 'aac'
    if value in ('mp4a.69', 'mp4a.6b'):
        return 'mp3'
    return value


def source_bytes(fmt, duration=0):
    exact = positive(fmt.get('filesize'))
    approximate = positive(fmt.get('filesize_approx'))
    rate = positive(fmt.get('tbr')) or positive(fmt.get('vbr')) + positive(fmt.get('abr'))
    size = exact or approximate or rate * positive(duration) * 1000 / 8
    return math.ceil(size * (1 if exact else 1.2)) if size else GIB


def output_bytes(inputs, kind, *, probes=None, duration=0, mp3=False, transcode=False):
    """Bound stream-copy cost by selected track, not the whole multiplexed file.

    Unknown track rates use a conservative fallback; the live writer guard is
    still required. Transcoding is deliberately not treated as stream copy.
    """
    sizes = [Path(path).stat().st_size for path in dict.fromkeys(inputs)]
    total = sum(sizes)
    if kind == 'subtitle':
        return max(MIB, total * 3)
    if transcode:
        return max(16 * MIB, total * 3)
    if kind == 'audio_only' and probes:
        _, info = probes[0]
        audio = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), {})
        seconds = positive(audio.get('duration')) or positive(info.get('format', {}).get('duration')) or positive(duration)
        rate = positive(audio.get('bit_rate'))
        if mp3:
            rate = max(rate, 320000)
        # 512 kbit/s accommodates ordinary YouTube AAC/Opus audio when TS
        # metadata omits its bitrate, without reserving a multi-GiB video again.
        if seconds:
            return max(MIB, min(total * 3 if mp3 else total,
                math.ceil(seconds * (rate or 512000) / 8 * 1.2) + MIB))
    return max(MIB, math.ceil(total * 1.05))


def acquisition_peak(sources, jobs, duration, *, test_mode):
    """Forecast before transfer, counting each shared source only once."""
    by_path = {str(source['path']): source for source in sources}
    original = sum(source['bytes'] for source in by_path.values())
    for job in jobs:
        selected = [by_path[path] for path in dict.fromkeys(job['inputs'])]
        kind = job['kind']
        fmt = selected[0]['format']
        compatible = codec_family(fmt.get('vcodec')) in ('h264', 'hevc', 'av1', 'mpeg4') and codec_family(fmt.get('acodec')) in ('aac', 'alac', 'mp3')
        mp4_output = job['mp4'] or job.get('auto') and compatible
        av_container = fmt.get('ext') in (('mp4', 'mov') if mp4_output else ('mkv', 'webm'))
        direct_av = (kind == 'av' and len(selected) == 1 and av_container and
                     (compatible or not mp4_output) and not str(fmt.get('protocol', '')).startswith('m3u8'))
        direct = len(selected) == 1 and (
            kind == 'video_only' and selected[0]['format'].get('acodec') == 'none' and not job['mp4'] or
            kind == 'audio_only' and selected[0]['format'].get('vcodec') == 'none' and not job['mp3'] or direct_av)
        if direct:
            job['output_estimate'] = 0
            continue
        amount = sum(item['bytes'] for item in selected)
        video = codec_family(selected[0]['format'].get('vcodec'))
        audio = codec_family(selected[-1]['format'].get('acodec'))
        transcode = (job['mp4'] and (kind in ('video_only', 'av') and video not in ('h264', 'hevc', 'av1', 'mpeg4') or
                     kind == 'av' and audio not in ('aac', 'alac', 'mp3')))
        if transcode:
            amount *= 3
        if kind == 'audio_only':
            fmt = selected[0]['format']
            rate = max(positive(fmt.get('abr')), 320) if job['mp3'] else positive(fmt.get('abr')) or 512
            amount = min(amount, math.ceil(positive(duration) * rate * 1000 / 8 * 1.2)) if duration else amount
        amount = max(MIB, math.ceil(amount * 1.1))
        job['output_estimate'] = amount
    # Test outputs remain in the workspace until receipt/sample settlement.
    # Production publication moves them out after each step.
    scratch = processing_peak(jobs, test_mode=test_mode)
    return dict(source_bytes=original, processing_bytes=scratch,
                peak_bytes=original + scratch + 16 * MIB,
                estimated=any(not positive(s['format'].get('filesize')) for s in sources))


def refine_processing_estimates(jobs):
    """Bound pending writes by actual acquired files, without network or probes.

    Codec-sensitive MP4/MP3 conversions retain a conservative transcode bound.
    A caption no longer reserves 24 MiB merely because its estimate is absent.
    """
    changed = False
    for job in jobs:
        if job.get('done') or job.get('prepared'):
            continue
        paths = [Path(p) for p in dict.fromkeys(job['inputs'])]
        if not all(p.is_file() for p in paths):
            continue
        if job['kind'] == 'subtitle':
            amount = output_bytes(paths, 'subtitle')
        else:
            total = sum(p.stat().st_size for p in paths)
            factor = 3 if job.get('mp4') or job.get('mp3') else 1.05
            amount = max(MIB, math.ceil(total * factor))
            if job.get('output_estimate') is not None:
                amount = min(amount, int(job['output_estimate']))
        if job.get('output_estimate') != amount:
            job['output_estimate'] = amount
            changed = True
    return changed


def unused_inputs(jobs):
    return {path for job in jobs if job.get('done') for path in job['inputs']} - {
        path for job in jobs if not job.get('done') for path in job['inputs']}


def processing_peak(jobs, *, test_mode):
    """Extra writes still needed, including a paired stream-copy operation."""
    pending = [j for j in jobs if not j.get('done') and not j.get('prepared')]
    costs = [int(j.get('output_estimate') or (24 * MIB if j['kind'] == 'subtitle' else 0)) for j in pending]
    if test_mode:
        return sum(costs)
    peak = max(costs, default=0)
    pairs = {}
    for job, cost in zip(pending, costs):
        if job['kind'] in ('video_only', 'audio_only') and len(set(job['inputs'])) == 1:
            key = tuple(job['inputs'])
            pairs[key] = pairs.get(key, 0) + cost
    return max([peak, *pairs.values()])
