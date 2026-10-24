"""Install a pinned official Linux Mihomo release, checking the asset SHA256."""
import gzip
import hashlib
from http.client import HTTPException
import os
from pathlib import Path
import platform
import subprocess
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

VERSION = 'v1.19.33'
ASSETS = {
    'x86_64': ('amd64-compatible', '386508bfb1c72c43b6a0eda73708ecdce82546581b0ebcb16366c53757c622b6'),
    'aarch64': ('arm64', 'ba408b317080ee265c46cb923a50018c1b82cbd708551cdd46a1e3e307396187'),
}
OFFICIAL_BASE = 'https://github.com/MetaCubeX/mihomo/releases/download'
DOWNLOAD_BASES = (
    'https://ghfast.top/' + OFFICIAL_BASE,
    'https://gh-proxy.com/' + OFFICIAL_BASE,
    OFFICIAL_BASE,
)
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024


def download_archive(filename, checksum, base_url=''):
    bases = list(dict.fromkeys(([base_url.rstrip('/')] if base_url else []) + list(DOWNLOAD_BASES)))
    if any(urlsplit(base).scheme not in ('http', 'https') or not urlsplit(base).hostname for base in bases):
        raise RuntimeError('MIHOMO_DOWNLOAD_BASE_URL must be an HTTP(S) release download base URL.')
    for attempt in range(1, 3):
        for base in bases:
            host = urlsplit(base).hostname
            print(f'[mihomo] Downloading {filename} from {host} (round {attempt}/2)', flush=True)
            try:
                request = Request(f'{base}/{VERSION}/{filename}', headers={
                    'User-Agent': 'youtube-downloader-build/1.0', 'Accept': 'application/octet-stream'})
                with urlopen(request, timeout=30) as response:
                    archive = response.read(MAX_ARCHIVE_BYTES + 1)
                if len(archive) > MAX_ARCHIVE_BYTES:
                    raise ValueError('archive exceeds size limit')
                if hashlib.sha256(archive).hexdigest() != checksum:
                    raise ValueError('archive SHA256 mismatch')
                print('[mihomo] Archive SHA256 verified.', flush=True)
                return archive
            except (OSError, HTTPException, ValueError) as error:
                print(f'[mihomo] {host} failed: {type(error).__name__}; trying another source.', flush=True)
        if attempt < 2:
            time.sleep(2)
    raise RuntimeError('Mihomo download failed from all sources after retries. '
                       'Check the Docker build network or set MIHOMO_DOWNLOAD_BASE_URL in .env '
                       'to an accessible release download base URL.')


def main():
    if platform.system() != 'Linux' or platform.machine() not in ASSETS:
        raise SystemExit('This Docker image supports Linux amd64 and arm64.')
    architecture, checksum = ASSETS[platform.machine()]
    filename = f'mihomo-linux-{architecture}-{VERSION}.gz'
    try:
        archive = download_archive(filename, checksum, os.environ.get('MIHOMO_DOWNLOAD_BASE_URL', '').strip())
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    target = Path('/app/.tools/mihomo/mihomo')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(gzip.decompress(archive))
    target.chmod(0o755)
    subprocess.run([str(target), '-v'], check=True, timeout=10)


if __name__ == '__main__':
    main()

