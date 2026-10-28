"""One public service role; old configuration names remain readable on upgrade."""

SERVICE_ROLE = 'hybrid'
LEGACY_ROLES = {'all', 'manager', 'worker'}


def normalize_role(value=None):
    if value in (None, '', SERVICE_ROLE) or value in LEGACY_ROLES:
        return SERVICE_ROLE
    raise ValueError('服务角色已统一为管理与下载一体（hybrid）。')
