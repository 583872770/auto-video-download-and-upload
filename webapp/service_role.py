"""One public service role for management and downloads."""

SERVICE_ROLE = 'hybrid'


def normalize_role(value=None):
    if value in (None, '', SERVICE_ROLE):
        return SERVICE_ROLE
    raise ValueError('服务角色已统一为管理与下载一体（hybrid）。')
