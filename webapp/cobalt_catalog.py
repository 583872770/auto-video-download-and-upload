"""Public Cobalt entrances tested from the deployment, without media credentials.

Verification is a dated observation, not an availability or independence promise.
Only entries with a fully downloaded and probed video can be added with one click.
"""

TESTED_AT = '2026-10-29'

_SITES = [
    ('bergung', 'bergung.hoffnungfuerdiezukunft.net', 'bergung-api.hoffnungfuerdiezukunft.net', 'verified', '系统原有入口，完整下载及校验通过'),
    ('cobalt_girlskissing', 'cobalt.girlskissing.top', 'apicobalt.girlskissing.top', 'verified', '完整下载及音视频校验通过'),
    ('cobalt_meowing', 'cobalt.meowing.de', 'cobalt-api.meowing.de', 'verified', '两路媒体下载、合并及校验通过'),
    ('cobalt_lamps', 'cobalt.lamps-dev.dev', 'cobalt-api.lamps-dev.dev', 'verified', '有完整下载成功，复测出现空媒体响应'),
    ('cobalt_canine', 'cobalt.canine.tools', 'cobalt-backend.canine.tools', 'verified', '两路媒体下载、合并及校验通过，验证有波动'),
    ('cobalt_clxxped', 'cobalt.clxxped.lol', 'cobalt-api.clxxped.lol', 'unavailable', '上游返回 YouTube 登录错误'),
    ('cobalt_squair', 'cobalt.squair.xyz', 'cobaltapi.squair.xyz', 'unavailable', '上游报告测试视频不可用，尚无完整下载成功'),
    ('cobalt_kittycat', 'cobalt.kittycat.boo', 'cobaltapi.kittycat.boo', 'unavailable', '当前测试出口未完成网站验证'),
    ('cobalt_tame', 'cobalt.tame.gg', 'kitty.tame.gg', 'verified', '完整下载及音视频校验通过，验证有波动'),
    ('cobalt_3kh0', 'cobalt.3kh0.net', 'capi.3kh0.net', 'verified', '完整下载及音视频校验通过，验证有波动'),
    ('cobalt_xenon', 'cobalt.xenon.zone', 'rue-cobalt.xenon.zone', 'verified', '完整下载及音视频校验通过'),
    ('cobalt_eversiege', 'cobalt.eversiege.network', 'api-cobalt.eversiege.network', 'unavailable', '媒体响应为空，未通过校验'),
    ('cobalt_qwkuns', 'qwkuns.me', 'api.qwkuns.me', 'unavailable', '上游返回 YouTube 登录错误'),
    ('cobalt_cjs', 'cobalt.cjs.nz', '', 'unavailable', '网站返回 HTTP 404'),
]


def catalog():
    entries = []
    for identity, host, api, status, reason in _SITES:
        channel = dict(id=identity, adapter='bergung', label=host, site_url='https://' + host + '/',
            api_url='https://' + api + '/' if api else '', verification='auto',
            upstream_group='unverified-media', resolver_group=identity, quota_group=identity,
            transfer_limit=32, resolver_limit=2, connection_limit=64, task_limit=4, weight=1)
        entries.append(dict(host=host, status=status, reason=reason, tested_at=TESTED_AT, channel=channel))
    return entries


def verified_channels():
    return [item['channel'] for item in catalog() if item['status'] == 'verified' and item['channel']['id'] != 'bergung']
