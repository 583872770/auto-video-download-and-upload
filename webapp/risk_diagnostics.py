"""Explain observed restrictions without guessing bans or exposing request credentials."""
import hashlib
import json
import re
from pathlib import Path
import time

from sqlalchemy import select

from webapp.database import settings


def entry(code, category, title, impact, recovery, action, href='/#settings'):
    return dict(code=code, category=category, title=title, impact=impact, recovery=recovery, action=action, href=href)


GUIDE = [
    entry('http_429', '上游限流与验证', 'HTTP 429：请求过多', '可能限制当前请求、渠道、共享上游或出口；不能据此确定账号或 IP 被封。', '按已记录冷却时间等待；到期后重新检查，恢复窗口可能暂按 1 路放行。', '降低并发与字幕请求频率，查看触发阶段和共享范围，避免反复手动重试。'),
    entry('http_503', '上游限流与验证', 'HTTP 503 / 5xx：服务暂不可用', '可能是服务负载、临时故障或验证页面；503 不一定是限流。', '遵守 Retry-After；无此信息时按本系统退避策略重试，不保证上游何时恢复。', '查看任务阶段，等待后重试；连续失败时检查服务状态。'),
    entry('http_403', '上游限流与验证', 'HTTP 401 / 403：请求被拒绝', '登录、访问权限、签名链接到期、地区限制及出口策略均可能导致拒绝，状态码不能单独确定原因。', '只有明确的临时错误才自动重试；访问权限或登录问题通常需要人工处理。', '用同一下载出口和账号确认源视频可播放，检查 Cookie、系统时间及错误详情。'),
    entry('http_404', '上游限流与验证', 'HTTP 404 / 410：请求资源不存在或已失效', '可能是视频、媒体分片、临时链接或第三方接口不存在，不能只看状态码就认定源视频被删除。', '临时链接可能需要重新解析；固定接口或内容缺失通常需要人工核对。', '查看发生阶段和请求服务，确认原视频仍可访问，再检查渠道接口。'),
    entry('bot_check', '上游限流与验证', '机器人检测 / CAPTCHA / Cloudflare 验证', '可能影响当前入口或当前出口；自动重试不保证能通过验证。', '需要时由用户在当前出口的浏览器中完成验证，再重新检测或重试。', '查看登录或网页验证窗口，降低请求频率；系统不会自动绕过验证码。'),
    entry('quota', '上游限流与验证', '第三方配额、每日额度或接口限制', '额度可能按账号、出口或后端共享；多个入口域名也可能共用配额。', '仅在服务返回明确时间时可判断重置时间，否则需要查看服务说明。', '检查服务配额与授权；提高本机并发不会增加第三方额度。'),
    entry('auth_missing', '登录与视频权限', 'Cookie 缺失、无效或未登录', '使用 yt-dlp 的实例可能无法通过开始前登录检查；不代表源视频已删除。', '登录成功并重新检测后，恢复受影响实例；第三方渠道有各自访问条件。', '在对应实例上传有效 Cookie 或使用登录窗口。'),
    entry('auth_unknown', '登录与视频权限', '登录状态未确认或验证已过期', '网络、TLS、页面变化或检测过期会使状态未知，不能直接判定 Cookie 失效。', '开始前或下次领取时重新检测；检测暂时不可用时会退避。', '检查下载出口和网络，在当前实例重新检测登录。'),
    entry('auth_valid', '登录与视频权限', '最近一次登录检查通过（有有效期）', '只确认检查时该连接的登录状态；不保证所有视频都满足权限、地区和源站验证要求。', '结果到期后重新检测；Cookie 仍可能在后续会话中被轮换或拒绝。', '遇到具体视频失败时结合源站播放结果和错误判断，不把检测通过当作免限流保证。'),
    entry('cookie_rotation', '登录与视频权限', 'Cookie 被轮换或被 YouTube 拒绝', '导出后的浏览器会话仍可能轮换 Cookie；本系统检测通过也不能保证后续视频都可访问。', '需要新的有效会话；不能靠增加重试次数保证恢复。', '按上传页说明重新导出并上传 Cookie，用同一出口验证播放。'),
    entry('content_unavailable', '登录与视频权限', 'Video unavailable / 视频不可用', '可能为删除、私有、地区限制、登录验证或服务响应异常；这条通用错误不能确认永久下架。', '一般不作为瞬时错误自动重试；确认原因后可手动重试。', '在同一出口打开该视频，结合登录状态与源站具体提示判断。'),
    entry('content_permission', '登录与视频权限', '年龄、地区、私有、会员或付费权限', '当前账号和出口需要满足源站访问条件；不应将单个视频限制扩大为整个渠道故障。', '等待并发名额不会解除访问限制。', '核对视频权限、账号资格和源站提示；只处理有权访问的内容。'),
    entry('drm', '登录与视频权限', 'DRM 或受保护媒体', '当前下载方式可能无法处理受保护媒体。', '普通重试、提高额度或重新登录通常不能解决。', '确认源站提供的授权下载方式及工具支持情况。'),
    entry('format', '解析与网页渠道', '画质 / 音轨不可用、JS 挑战或解析器不兼容', '源站格式、运行时或提取规则变化会导致解析失败，不能直接归因于风控。', '配置或工具更新后重试；第三方最高画质也受实际接口能力约束。', '检查 Node / Deno、yt-dlp 版本、画质选项和任务日志。'),
    entry('website', '解析与网页渠道', '第三方网页改版、转换超时或接口异常', '可能尚未开始媒体传输；按顺序或自动策略是否切换渠道以本次任务配置为准。', '转换有阶段超时；失败后可能切换或按任务策略重试，不能保证成功。', '区分解析、验证、转换和下载阶段，查看服务状态及错误详情。'),
    entry('browser_wait', '解析与网页渠道', '浏览器名额 / 独立解析名额不足', '网页解析、验证和转换会占用浏览器；有空闲视频工作进程也可能等待。', '已有任务释放名额后继续；人工验证可能延长占用。', '结合浏览器上限、解析组额度和机器内存调整；手动 yt-dlp 的解析与传输共用名额。'),
    entry('environment', '解析与网页渠道', '浏览器、显示环境、FFmpeg 或 JS 工具缺失', '对应实例可能停止领取；其他健康实例和任务意愿保留。', '修复工具或容器环境并重新检查后恢复实例。', '查看工具检查与实例错误；容器桌面失败检查 Xvfb、x11vnc、noVNC 日志。', '/#instances'),
    entry('network', '网络与代理', '超时、连接重置、DNS 或 TLS 错误', '可能来自本机网络、代理、证书或远端服务，不能据此确定账号问题。', '部分临时网络错误按退避策略重试；超过次数会失败。', '检查 DNS、证书、系统时间及下载出口，优先确认源站可访问。'),
    entry('proxy_cooldown', '网络与代理', '代理线路熔断、渠道冷却或恢复探测', '连续连接故障可限制某节点上的渠道；跨渠道故障可能让整个节点暂时冷却。', '冷却后可能只允许一个探测任务；倒计时结束不代表线路已恢复。', '查看节点健康和出口；延迟测试通过不代表能下载该视频。', '/#clash'),
    entry('proxy_scope', '网络与代理', '直连、代理与多实例共用出口', '多个账号、实例或入口可能共用同一出口，源站限制可能一起生效；系统无法直接获知源站配额规则。', '本系统共享组保护与源站实际风控范围可能不同。', '核对当前实例连接；切换出口可能改变登录有效性和视频可用性。', '/#clash'),
    entry('channel_cap', '并发与共享额度', '实例并发与渠道传输上限不同', '设置 16 个进程不等于能同时传输 16 个视频，解析与合并也占工作进程。', '降低的额度不强制中断已有传输，已有占用可能暂时超过新上限。', '同时检查渠道、连接、浏览器和全局额度；按成功速度和错误率逐步调整。'),
    entry('connection_cap', '并发与共享额度', '本机 / 共享媒体连接预算不足', '一个视频可能预留多个分片连接；预留计数不是实际套接字数。', '连接名额释放后继续；当前额度只是准入上界估算。', '结合分片并发与本机、共享连接预算调整，避免仅提高进程数。'),
    entry('shared_cap', '并发与共享额度', '同组入口、多实例共享与全局上限', '同资源组取有效策略中的较小额度，别名入口不能直接叠加；其他实例也占名额。', '全局名额包含解析和合并，等待其他任务完成后才可继续领取。', '查看共享组和全部实例占用，确认是否有其他实例配置了较低上限。', '/#instances'),
    entry('auto_control', '并发与共享额度', '自动调节、资源约束及采样缺失', 'CPU、内存、数据库连接预算、有效需求及错误率可能使额度低于配置上限。', '新配置或采样过期时采用保守额度，获得样本后逐步调整。', '查看调节原因与样本时间；没有等待任务、源站慢或合并中都可能使并发不足。', '/#instances'),
    entry('bandwidth', '并发与共享额度', '带宽瓶颈、源站慢速与速度采样差异', '带宽目标上限不是实际速度保证；网卡流量、媒体接收和验证成品速度口径不同，短时可能不一致。', '上游供给、网络拥塞、合并和样本窗口都会影响显示速度。', '观察持续成功交付速度及错误率，避免只为跑满进程反复增加并发。', '/#instances'),
    entry('disk_budget', '磁盘与本机资源', '分类预算 / 总临时预算不足', '磁盘有剩余空间仍可能因测试、缓存或浏览器分类预算不足而等待。', '符合清理条件的临时文件释放后自动恢复；不能清理正在使用的数据。', '查看已用量、执行预留量、各分类预算及容量估算；估算不是下个视频大小保证。'),
    entry('disk_free', '磁盘与本机资源', '物理空间不足、配额不足或恢复水位未达到', '文件写入、合并、日志和 Cookie 临时文件均可能受影响。', '低水位触发后通常需达到恢复水位；释放少量空间不一定立即恢复。', '检查容器所在磁盘、挂载、主机配额与低 / 高水位。'),
    entry('file_permission', '磁盘与本机资源', '目录权限、路径过长或文件写入失败', '属于本机保存环境问题，不应归类为上游风控；视频或配套文件可能未完成。', '修复目录、挂载或路径配置后重试。', '检查目录可写性、文件占用、路径长度及容器用户权限。'),
    entry('telemetry', '磁盘与本机资源', '日志或吞吐统计暂不可写', '下载统计写入故障会告警并继续视频任务；页面数据可能缺失或延迟。业务完成事务仍需数据库成功提交。', '写入恢复后继续发布；无数据不能说明没有传输。', '查看实例日志、日志预算与磁盘状态，结合任务实际结果判断。', '/#instances'),
    entry('subtitle', '字幕与交付', '字幕独立队列、失败与缺少指定语言', '默认字幕共享低频额度，不占视频传输许可，不改变已成功视频的交付及计费。', '可选字幕按独立退避重试；无所选语言或视频已移除时可能不生成字幕。', '查看父视频关联字幕任务；测试模式和第三方渠道不创建后台字幕任务。', '/#tasks'),
    entry('subtitle_required', '字幕与交付', '必须完整字幕模式', '至少有一种所选语言且所选可用字幕写入成功才完成；字幕失败会影响视频任务。', '字幕请求仍在视频渠道内，可能触发视频渠道冷却；已落盘文件不等于任务成功。', '按需要选择后台补下载、必须完整或关闭，避免无意要求全部翻译语言。'),
    entry('paused', '任务、实例与恢复', '系统暂停、实例暂停、未开始任务或清理中', '系统、实例和业务任务有各自控制状态；恢复一层不一定恢复其他层。', '完成当前后停止不再领取新视频或字幕；立即停止会终止并回队。', '检查所选任务是否开始、实例是否恢复及素材清理状态。', '/#overview'),
    entry('retry', '任务、实例与恢复', '排队退避、自动重试与次数耗尽', '资源等待释放通常不消耗失败重试次数；下载错误只对识别出的临时错误重试。', '以任务状态、重试次数和下次可执行时间为准；次数耗尽后需要人工重试。', '先确认原因再重试，避免批量高频重试扩大上游限制。', '/#tasks'),
    entry('lease', '任务、实例与恢复', '执行租约失效、重启或所有权改变', '旧进程必须停止，避免两个进程交付同一任务；正在运行的显示可能需要等状态刷新。', '符合条件的过期任务由队列恢复；已停止 / 已变更任务以最新状态为准。', '检查实例在线状态、数据库连通性和任务详情，避免启动重复执行器。', '/#instances'),
    entry('database', '任务、实例与恢复', '数据库锁等待、死锁或连接失败', '会影响领取、心跳和完成状态；与上游账号风控无关。', '死锁 / 锁等待进行有限事务重试；连接断开不会盲目重放下载。', '查看实例日志与 MySQL 状态；确认最终任务和交付记录后再决定重试。', '/#instances'),
    entry('offline', '任务、实例与恢复', '实例离线、状态过期或管理连接不可用', '页面只能保留最后数据，无法确认当前并发、渠道或线路是否可用。', '心跳和管理请求恢复后重新确认；倒计时到零也需要新状态验证。', '检查实例服务、地址、可信主机、数据库和网络，不把旧快照当作实时状态。', '/#instances'),
    entry('execution_blocked', '任务、实例与恢复', '实例因检查失败被停止领取', '环境或登录检查失败可隔离对应实例，其他健康实例继续。', '先修复提示的问题，再检查并恢复该实例。', '打开实例详情核对错误；恢复系统领取不会自动清除所有实例问题。', '/#instances'),
    entry('unknown', '任务、实例与恢复', '未分类错误 / 证据不足', '现有错误信息不足以确认是限流、访问权限、网络还是工具故障。', '是否自动重试以实际队列状态为准；不要推断已被封禁。', '展开技术日志查看发生阶段、HTTP 状态和最后一次失败，提交问题时隐去 Cookie、密钥和签名地址。', '/#tasks'),
]

RULES = [
    ('http_429', r'\b429\b|too many requests'),
    ('http_503', r'\b50[0234]\b|service unavailable'),
    ('http_403', r'\b40[13]\b|forbidden|unauthorized'),
    ('http_404', r'\b(?:404|410)\b|http.*not found'),
    ('bot_check', r'captcha|cloudflare|confirm.*bot|robot|机器人|额外验证|网页验证'),
    ('quota', r'quota|daily limit|每日|配额'),
    ('cookie_rotation', r'cookies? are no longer valid|cookie.*(?:失效|轮换|拒绝)|账号.*cookie'),
    ('auth_missing', r'未登录|没有.*登录凭据|cookie.*(?:无效|不存在)|请先登录|登录检查未通过'),
    ('content_unavailable', r'video unavailable|video is unavailable|视频不可用'),
    ('content_permission', r'private video|members.only|age.restrict|not available in your country|geo.restrict|sign in to confirm your age|视频已删除|私有视频|会员专享|年龄限制|地区限制'),
    ('drm', r'\bdrm\b|受保护媒体'),
    ('format', r'requested format.*not available|signature.*(?:failed|solv)|js.*challenge|javascript runtime|所选画质|格式不可用'),
    ('browser_wait', r'等待浏览器|浏览器名额|解析名额'),
    ('environment', r'xvfb|x11vnc|novnc|chromium|ffmpeg.*(?:missing|not found|未安装)|(?:缺少|未安装).*(?:浏览器|ffmpeg|node)|显示环境'),
    ('proxy_cooldown', r'代理.*冷却|线路.*冷却|出口.*冷却'),
    ('network', r'time.?out|timed out|connection (?:reset|refused|aborted)|dns|ssl|tls|证书|连接超时|网络请求失败|name resolution'),
    ('disk_budget', r'预算|diskpressure'),
    ('disk_free', r'no space left|disk full|disk quota|磁盘.*(?:不足|配额)|剩余空间不足'),
    ('file_permission', r'permission denied|read.only file|路径过长|目录.*不可写|access is denied'),
    ('lease', r'租约|lease.*(?:lost|expired)|ownership.*lost'),
    ('database', r'deadlock|\b1213\b|\b1205\b|database is locked|mysql|数据库'),
    ('website', r'conversion|转换.*(?:失败|超时)|解析.*失败|website.*(?:changed|error)'),
]


def classify(message):
    """Return only fixed codes; never echo potentially sensitive exception text."""
    text = str(message or '')[:12000].lower()
    text = re.sub(r'https?://[^\s<>"\']+', '[url]', text)
    codes = [code for code, pattern in RULES if re.search(pattern, text, re.I)]
    if 'quota' in codes and ('disk_free' in codes or 'disk_budget' in codes):
        codes.remove('quota')
    return codes


def task_risks(task):
    progress = task.get('progress') or {}
    codes = classify(task.get('error'))
    if task['action'] == 'subtitles':
        codes.append('subtitle')
    if task['status'] == 'queued' and progress.get('stage') == 'retrying':
        codes.append('retry')
    if task['status'] in ('failed', 'interrupted') and not codes:
        codes.append('unknown')
    return dict(codes=list(dict.fromkeys(codes)), status=task['status'],
                attempts=task.get('attempts', 0), max_attempts=task.get('max_attempts', 0),
                available_at=task.get('available_at', 0), stage=progress.get('stage'),
                retry_scheduled=task['status'] == 'queued' and progress.get('stage') == 'retrying')


def read_auth(db, identities, *, standalone=False):
    keys = {('youtube_auth_latest' if standalone else
             f'i:{hashlib.sha256(identity.encode()).hexdigest()[:16]}:youtube_auth_latest'): identity for identity in identities}
    result = {}
    for key, raw in db.execute(select(settings.c.key, settings.c.value).where(settings.c.key.in_(keys))):
        try:
            value = json.loads(raw)
            if isinstance(value, dict):
                result[keys[key]] = {k: value.get(k) for k in ('state', 'checked_at', 'expires_at')}
        except (ValueError, TypeError):
            pass
    return result


def local_health(directory, config, clash_directory=None):
    """Read bounded local health files only; never probe or switch a proxy."""
    result = {'time': time.time(), 'route_known': False, 'cooling': 0, 'probing': 0, 'seconds': 0}
    if config.get('proxy_mode') == 'clash' and clash_directory:
        path = Path(clash_directory) / 'route-health.json'
        try:
            if path.stat().st_size <= 1024 * 1024:
                data = json.loads(path.read_text(encoding='utf-8'))
                stamp = result['time']
                for row in data.values():
                    until = max([row.get('until', 0), *(v.get('until', 0) for v in row.get('providers', {}).values())])
                    result['cooling'] += int(until > stamp)
                    result['probing'] += int(row.get('probe_until', 0) > stamp)
                    result['seconds'] = max(result['seconds'], max(0, int(until - stamp)))
                result['route_known'] = bool(data)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    try:
        path = Path(directory) / 'log-write-warning.json'
        if path.stat().st_size <= 16384:
            warning = json.loads(path.read_text(encoding='utf-8'))
            result['log_warning'] = 0 <= result['time'] - warning['time'] < 300
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return result


def observed(config, diagnostic, auth, stamp, *, error=''):
    """Facts supplement admission notices; missing evidence is explicitly unknown."""
    from webapp.settings import download_methods
    methods = download_methods(config)
    result = []
    def add(code, detail, severity='info'):
        result.append(dict(code=code, detail=detail, severity=severity))
    if error:
        add('execution_blocked', '实例保存了检查失败记录，请核对实例详情。', 'warning')
        for code in classify(error):
            add(code, '依据：该实例记录的检查错误；具体原因仍需结合任务和源站验证。', 'warning')
    if 'yt_dlp' in methods:
        state = (auth or {}).get('state', 'unchecked')
        expired = state == 'valid' and (auth.get('expires_at') or 0) <= stamp
        if state in ('missing', 'invalid', 'not_logged_in'):
            add('auth_missing', '最近一次登录检查未通过；这是登录检查结果，不是对全部视频可用性的判断。', 'warning')
        elif state == 'verification_required':
            add('bot_check', '最近一次登录检查要求额外验证或遇到访问限制；请在同一出口确认。', 'warning')
        elif state != 'valid' or expired:
            add('auth_unknown', '登录检测未完成、不可用或结果过期；页面未实时探测源站。')
            if state == 'network_error':
                add('network', '最近一次登录检查记录了网络错误；这不足以判断 Cookie 是否失效。', 'warning')
        else:
            add('auth_valid', '最近一次登录检查通过；仅代表当时该连接的登录状态，不保证每个视频可下载。')
    stages = diagnostic.get('stages', {})
    browsers = sum(stages.get(stage, 0) for stage in ('waiting_browser', 'waiting_verification', 'converting', 'waiting_site_form'))
    if browsers:
        add('browser_wait', f'有 {browsers} 个视频处于浏览器等待、验证或转换阶段；尚不能都视为正在传输。')
    if any(r['scope'] == 'route-probe' and r['used'] >= r['limit']
           for channel in diagnostic.get('channels', []) for r in channel.get('resources', [])):
        add('auto_control', '当前实例正在试探尚无成功样本的入口；同时只允许一个未验证入口探测，其他准备任务可能等待。')
    for channel in diagnostic.get('channels', []):
        if channel.get('failures'):
            add('website', f"{channel['label']} 最近连续失败 {channel['failures']} 次；这是渠道健康记录，不包含所有历史失败，也不能单独确认出口被限制。", 'warning')
    if config.get('subtitle_mode', 'optional') == 'required' and 'yt_dlp' in methods:
        add('subtitle_required', '当前实例选择了必须完整字幕模式。', 'warning')
    if config.get('proxy_mode') in ('direct', 'clash', 'manual'):
        add('proxy_scope', '当前连接方式：' + {'direct': '直连', 'clash': 'Clash 代理', 'manual': '手动连接'}[config['proxy_mode']] + '。出口实际风控范围未由源站公开，本系统无法确认。')
    health = diagnostic.get('local_health') or {}
    if abs(stamp - health.get('time', 0)) < 30:
        if health.get('cooling') or health.get('probing'):
            add('proxy_cooldown', f"最近线路健康记录：{health.get('cooling', 0)} 条线路存在渠道或节点冷却，{health.get('probing', 0)} 条处于单任务探测期。记录可能包含之前使用的节点，不等于全部当前候选节点不可用。", 'warning')
        if health.get('log_warning'):
            add('telemetry', '近 5 分钟记录了日志写入告警，请检查空间和日志预算。', 'warning')
        if config.get('proxy_mode') == 'clash' and not health.get('route_known'):
            add('proxy_cooldown', '当前没有可用的线路健康记录；不能据此确认全部线路健康。')
    elif config.get('proxy_mode') == 'clash':
        add('proxy_cooldown', '当前实例未发布新鲜线路健康摘要，无法确认是否存在节点冷却；请查看对应实例节点页面。')
    return result
