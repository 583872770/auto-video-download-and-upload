"""Actionable startup diagnoses without SQL parameters or connection secrets."""
from pathlib import Path
import re
import traceback
from urllib.parse import quote, quote_plus

from webapp.database import SchemaUpgradeInProgress
from webapp.modes import ModeTransitionError


class ClusterRoleConflict(ValueError):
    def __init__(self, role, identity):
        self.role, self.identity = role, identity
        label = {'manager': '管理后台', 'worker': '下载实例', 'hybrid': '管理与下载一体'}[role]
        super().__init__(f'此状态目录已属于集群实例，原角色为“{label}”，实例 ID 为 {identity}。'
                         '请在部署设置中确认单机转换，或恢复原实例配置后保存。')


def explain_startup_error(error, stage, profile):
    original = getattr(error, 'orig', None) or error
    code = original.args[0] if original.args and type(original.args[0]) is int else None
    message = str(error) if isinstance(error, ValueError) else ''
    summary = '服务初始化失败。'
    remedy = '展开下方技术详情查看失败阶段和代码位置，将详情提供给维护人员；无需反复修改数据库密码或实例 ID。'
    detail = ''
    if isinstance(error, ClusterRoleConflict):
        label = {'manager': '管理后台', 'worker': '下载实例', 'hybrid': '管理与下载一体'}[error.role]
        summary = '当前状态目录属于集群实例，不能直接改为单机模式。'
        remedy = (f'可点击“恢复原实例配置”恢复“{label}”和实例 ID {error.identity}；'
                  '需要单机时，选择单机模式、勾选转换确认，再保存并重启。')
        if error.role in ('worker', 'hybrid'):
            remedy += '下载实例还需填写管理端可访问的实例地址。'
    elif isinstance(error, ModeTransitionError):
        summary = str(error)
        remedy = '处理上述原因后，在部署设置中保存单机配置重试；已保存的转换请求会在下次重启时继续执行。'
    elif isinstance(error, SchemaUpgradeInProgress):
        summary = '数据库仍在升级或等待表结构锁。'
        remedy = '先等待现有数据库升级完成，再点“保存并启动服务”；不要反复提交或同时启动多个实例，不需要修改实例 ID 和密码。'
    elif code in (2006, 2013, 1205) and any(word in stage for word in ('升级', '表结构', '索引')):
        summary = f'数据库升级等待超时（MySQL {code}），服务尚未就绪。'
        remedy = '大表升级可能仍在 MySQL 后台执行。先等待升级完成，避免重复提交；若持续等待，请让数据库管理员检查正在执行的 ALTER TABLE 和表结构锁。'
    elif code in (1044, 1045, 1142, 1143, 1227):
        summary = f'数据库账号权限不足或认证失败（MySQL {code}）。'
        remedy = '确认当前账号能访问所填数据库，并具有 SELECT、INSERT、UPDATE、DELETE、CREATE、ALTER、INDEX 权限；由数据库管理员为该库授权后重试。'
    elif code == 1049:
        summary = '填写的 MySQL 数据库不存在（MySQL 1049）。'
        remedy = '确认数据库名称，或先在 MySQL 中创建该数据库，再保存启动。'
    elif code in (2002, 2003, 2005, 2006, 2013):
        summary = f'MySQL 连接失败或已断开（MySQL {code}）。'
        remedy = '在此页面测试连接，确认数据库地址、端口、服务器白名单及网络连通性。'
    elif code == 1060:
        summary = '字段已被另一个升级进程添加（MySQL 1060）。'
        remedy = '等待其他实例升级完成后重试，避免旧版和新版同时升级同一个数据库。'
    elif '实例 ID 已在线' in message:
        summary = '这个实例 ID 已被运行中的服务使用。'
        remedy = '关闭使用相同状态目录的旧服务后重试；不同下载实例请使用独立状态目录和不同实例 ID，不要删除原状态目录。'
        detail = message
    elif '此状态目录已属于集群实例' in message:
        summary = '当前状态目录属于集群实例，不能直接改为单机模式。'
        remedy = '在页面选择原来的管理后台或下载实例角色，再保存启动；新建单机服务应使用独立状态目录。'
    elif 'YAML 配置格式错误' in message or message == '配置文件必须是 YAML 键值表':
        summary = '本机 config.yaml 格式不正确。'
        remedy = '检查配置文件的缩进、冒号和引号；修正 YAML 格式后重新保存启动。'
        detail = message
    elif isinstance(error, OSError) and '浏览器桌面' in stage:
        summary = '浏览器桌面环境启动失败。'
        remedy = ('检查容器内 /tmp/xvfb.log、/tmp/x11vnc.log 和 /tmp/novnc.log；'
                  '确认镜像安装了 Chromium、Xvfb、x11vnc 和 websockify，再重试。')
        detail = f'系统错误码：{error.errno}' if error.errno is not None else ''
    elif isinstance(error, PermissionError):
        summary = '本机文件或目录没有读写权限。'
        remedy = '确认启动账号可以读写配置目录、状态目录和素材目录，Docker 中需检查对应卷的权限。'
    elif isinstance(error, FileNotFoundError):
        summary = '服务所需的本机文件或程序不存在。'
        remedy = '检查启动时选择的配置文件及浏览器程序路径，确认文件存在后重试。'
        detail = str(error.filename or '')
    elif code is not None:
        summary = f'数据库初始化失败（MySQL {code}）。'
        remedy = '展开技术详情查看 MySQL 原因；先处理对应数据库错误，再保存启动。'

    # Driver messages are useful, but SQLAlchemy's str(error) also includes SQL
    # parameters, so never return or log that full representation.
    if code is not None and len(original.args) > 1:
        detail = str(original.args[1])
    for secret in (profile.get('mysql_password', ''),):
        if secret:
            for variant in {secret, quote(secret, safe=''), quote_plus(secret)}:
                detail = detail.replace(variant, '[已隐藏]')
    detail = re.sub(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\x22\x27]+', '[连接地址已隐藏]', detail)
    location = [f'{Path(frame.filename).name}:{frame.lineno} · {frame.name}'
                for frame in traceback.extract_tb(error.__traceback__)[-8:]]
    return {'summary': summary, 'remedy': remedy, 'stage': stage, 'type': type(error).__name__,
            'mysql_code': code, 'detail': detail[:1200], 'location': location}
