"""Resolve configuration paths relative to the YAML file, independent of cwd."""
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'config.yaml'


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).expanduser().resolve()
    try:
        with path.open(encoding='utf-8') as source:
            config = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise ValueError(f'YAML 配置格式错误：{path}') from error
    if not isinstance(config, dict):
        raise ValueError('配置文件必须是 YAML 键值表')
    return config, path.parent


def resolve_path(base, value):
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path(base) / path).resolve()


def video_directory(config, base):
    return resolve_path(base, config.get('path', 'videos')) / config.get('dir', '')
