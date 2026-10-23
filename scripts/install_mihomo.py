"""Install the pinned official Mihomo build for Windows x64 into this project."""
import io
from pathlib import Path
import platform
import subprocess
import tempfile
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'v1.19.33'


def main():
    if platform.system() != 'Windows' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('请为当前系统安装对应的 Mihomo，并放到 .tools/mihomo/。')
    folder = ROOT / '.tools/mihomo'
    target = folder / 'mihomo.exe'
    if target.is_file():
        subprocess.run([str(target), '-v'], check=True)
        print('Mihomo 已安装。')
        return
    filename = f'mihomo-windows-amd64-compatible-{VERSION}.zip'
    url = f'https://github.com/MetaCubeX/mihomo/releases/download/{VERSION}/{filename}'
    print(f'正在下载官方 Mihomo {VERSION}…', flush=True)
    with urlopen(url, timeout=60) as response:
        archive = response.read(50 * 1024 * 1024 + 1)
    if len(archive) > 50 * 1024 * 1024:
        raise SystemExit('下载包超过大小限制。')
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        member = next(name for name in bundle.namelist() if name.endswith('.exe'))
        content = bundle.read(member)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, suffix='.exe', delete=False) as output:
        temporary = Path(output.name)
        output.write(content)
    try:
        subprocess.run([str(temporary), '-v'], check=True, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Mihomo {VERSION} 已安装到项目目录。')


if __name__ == '__main__':
    main()
