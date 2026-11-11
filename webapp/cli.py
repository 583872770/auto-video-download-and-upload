"""HTTP service command-line entrypoint."""
import argparse
from pathlib import Path
from Utils.config import DEFAULT_CONFIG
from Utils.runtime import ROOT
from webapp.service_role import normalize_role


def main():
    parser = argparse.ArgumentParser(description='视频工作台 Flask 服务')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=5000, type=int)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--env-file', type=Path, help='数据库环境文件，默认读取项目目录中的 .env')
    parser.add_argument('--role', type=normalize_role, metavar='hybrid', help='固定为管理与下载一体；兼容读取旧角色名称')
    parser.add_argument('--state', type=Path, help='实例独立状态目录')
    parser.add_argument('--instance-id', help='固定实例 ID，不同实例必须不同')
    parser.add_argument('--instance-url', default='', help='管理端能访问的下载实例地址')
    parser.add_argument('--instance-name', help='页面展示名称')
    args = parser.parse_args()
    from waitress import create_server
    from webapp.deployment import DeploymentGateway
    service = DeploymentGateway(args.config, args.state or ROOT / '.cache' / 'web', env_file=args.env_file,
                                role=args.role, instance_id=args.instance_id, instance_url=args.instance_url,
                                instance_name=args.instance_name)
    server = create_server(service, host=args.host, port=args.port, threads=8,
                           max_request_body_size=8 * 1024 ** 3, channel_timeout=300)
    print(f'视频工作台：http://{args.host}:{args.port}', flush=True)
    print(f'部署设置：http://{args.host}:{args.port}/setup', flush=True)
    print(f'部署设置访问码：{service.access_code}', flush=True)
    service.start()
    try:
        server.run()
    finally:
        server.close()
