"""Stream executor-owned files without exposing its node credentials."""
from flask import Response, request, stream_with_context
import requests


def remote_content(fleet, identity):
    row = fleet.resolve_target(identity)
    session = requests.Session();session.trust_env = False
    headers = {'Authorization': 'Bearer ' + row['token']}
    for key in ('Range', 'If-Range', 'If-None-Match'):
        if key in request.headers:
            headers[key] = request.headers[key]
    try:
        response = session.request(request.method, row['url'] + request.full_path.rstrip('?'), headers=headers,
                               stream=True, allow_redirects=False, timeout=(5, 45))
    except requests.RequestException:
        session.close()
        raise ValueError('交付实例暂时无法连接，请检查实例状态。') from None
    if response.status_code not in (200, 206, 304, 404, 416):
        response.close();session.close()
        raise ValueError('交付实例拒绝文件请求，请检查版本和认证。')
    def content():
        try:
            yield from response.iter_content(1024 * 1024)
        finally:
            response.close();session.close()
    allowed = ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges', 'Content-Disposition', 'ETag', 'Last-Modified')
    result = Response([] if request.method == 'HEAD' else stream_with_context(content()), status=response.status_code,
                      headers={key: response.headers[key] for key in allowed if key in response.headers})
    def close():
        response.close();session.close()
    result.call_on_close(close)
    if request.method == 'HEAD':
        close()
    return result
