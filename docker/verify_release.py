"""Verify the running image against the source manifest shipped in a release archive."""
import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    path = root / 'docker' / 'deployment-manifest.json'
    if not path.is_file():
        print('未找到发布清单；此镜像未包含部署包的版本标识。')
        return 2
    manifest = json.loads(path.read_text(encoding='utf-8'))
    mismatches = []
    for relative, expected in manifest['files'].items():
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            mismatches.append(relative)
        elif hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            mismatches.append(relative)
    print('应用代码版本：' + manifest['version'])
    if mismatches:
        print('文件校验不一致：' + ', '.join(mismatches))
        return 1
    print(f'校验通过：{len(manifest["files"])} 个应用文件与部署包一致。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
