"""Install a pinned official Linux Mihomo release, checking the asset SHA256."""
import gzip
import hashlib
from pathlib import Path
import platform
import subprocess
from urllib.request import urlopen

VERSION = 'v1.19.33'
ASSETS = {
    'x86_64': ('amd64-compatible', '386508bfb1c72c43b6a0eda73708ecdce82546581b0ebcb16366c53757c622b6'),
    'aarch64': ('arm64', 'ba408b317080ee265c46cb923a50018c1b82cbd708551cdd46a1e3e307396187'),
}


def main():
    if platform.system() != 'Linux' or platform.machine() not in ASSETS:
        raise SystemExit('This Docker image supports Linux amd64 and arm64.')
    architecture, checksum = ASSETS[platform.machine()]
    filename = f'mihomo-linux-{architecture}-{VERSION}.gz'
    url = f'https://github.com/MetaCubeX/mihomo/releases/download/{VERSION}/{filename}'
    with urlopen(url, timeout=120) as response:
        archive = response.read(50 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != checksum:
        raise SystemExit('Mihomo download SHA256 mismatch')
    target = Path('/app/.tools/mihomo/mihomo')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(gzip.decompress(archive))
    target.chmod(0o755)
    subprocess.run([str(target), '-v'], check=True, timeout=10)


if __name__ == '__main__':
    main()
