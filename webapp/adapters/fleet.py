"""Authenticated instance RPC and bounded streaming request bodies."""
import requests


class UploadBody:
    """Supply a known length so requests doesn't add chunked beside Content-Length."""
    def __init__(self, stream, length):
        self.stream, self.length = stream, length

    def __len__(self):
        return self.length

    def __iter__(self):
        while chunk := self.stream.read(128 * 1024):
            yield chunk


def instance_auth_error(row):
    return (f'下载实例“{row["name"]}”拒绝了实例认证。请检查实例地址是否指向正确的下载服务、各实例版本是否一致，'
            '以及反向代理是否保留 Authorization 请求头；这不是管理账户登录失效。')


def rpc(fleet, target, path, *, method='GET', data=None, timeout=45):
    row = fleet.resolve_target(target)
    if row['id'] == fleet.id and path == '/api/fleet/preflight':
        value, status = fleet.check_local(data or {})
        if status >= 400:
            raise ValueError(value['error'])
        return value
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.request(method, row['url'] + path, headers={'Authorization': 'Bearer ' + row['token']},
                                   json=data, timeout=(5, timeout), allow_redirects=False)
        if response.status_code in (401, 403):
            raise ValueError(instance_auth_error(row))
        try:
            value = response.json()
        except ValueError:
            raise ValueError('下载实例返回了无效响应，请检查实例地址。') from None
        if not response.ok:
            raise ValueError(value.get('error', '下载实例请求失败。'))
        return value
    except requests.RequestException:
        raise ValueError('无法连接下载实例，请检查实例地址、网络及运行状态。') from None
    finally:
        session.close()


