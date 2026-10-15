"""Split via FFmpeg without retaining a MoviePy reader or background process."""
import math
from pathlib import Path
import subprocess
import tempfile
from Utils.paths import io_path


def get_video_duration(input_file):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', io_path(input_file),
    ], check=True, capture_output=True, text=True)
    duration = float(result.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f'视频时长无效：{input_file}')
    return duration


def segment_count(duration, max_duration=180):
    if not math.isfinite(max_duration) or max_duration <= 0:
        raise ValueError('分段时长必须是正数')
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('视频时长必须是正数')
    return math.ceil(duration / max_duration)


def split_video_ffmpeg(input_video, output_folder, max_duration=180):
    source, destination = Path(io_path(input_video)), Path(io_path(output_folder))
    duration = get_video_duration(source)
    count = segment_count(duration, max_duration)
    length = duration / count
    destination.mkdir(parents=True, exist_ok=True)
    outputs = []
    # A failed FFmpeg run must not leave a partial episode available for upload.
    # Short staging names avoid multiplying a long source filename inside its
    # own directory. Keep staging on the destination filesystem so the final
    # replace remains atomic even when the video is on another Windows drive.
    with tempfile.TemporaryDirectory(prefix='.split-', dir=destination) as staging:
        staged = []
        for index in range(count):
            name = f'{source.stem}【第{index + 1}集】.mp4'
            output = Path(staging) / f'part-{index + 1:06d}.mp4'
            subprocess.run([
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-ss', str(index * length), '-i', io_path(source), '-t', str(length),
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                '-c:a', 'aac', '-movflags', '+faststart', io_path(output),
            ], check=True)
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError(f'FFmpeg 未生成有效分段：{output}')
            staged.append((output, name))
        for output, name in staged:
            target = Path(io_path(destination / name))
            output.replace(target)
            outputs.append(target)
    return outputs
