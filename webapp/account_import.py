"""Strict, bounded account import. Parsing errors never contain input secrets."""
import re
from urllib.parse import urlsplit


def email(value):
    if not isinstance(value, str):
        raise ValueError('请填写邮箱账号。')
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise ValueError('邮箱账号格式不正确。')
    return value


def url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('接码网址格式不正确。')
    value = re.sub(r'^接码网址\s*[:：]\s*', '', value.strip())
    if not value:
        return ''
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
                or any(ord(c) < 33 for c in value)):
            raise ValueError
        parts.port
    except ValueError:
        raise ValueError('接码网址只支持有效的 HTTP/HTTPS 地址。') from None
    return value


def secret_fields(data, *, creating=False):
    result = {}
    for key in ('password', 'extra', 'code_url'):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str) or len(value) > 16384:
            raise ValueError('密码或附加信息过长或格式无效。')
        if key == 'password' and not value:
            continue  # Blank edit password always means keep.
        result[key] = url(value) if key == 'code_url' else value
    if creating and not result.get('password'):
        raise ValueError('新增账号需要填写密码。')
    return result


def parse_import(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 1024 * 1024:
        raise ValueError('导入文本最大 1 MB。')
    lines = raw.lstrip('\ufeff').splitlines()
    if len(lines) > 1000:
        raise ValueError('每次最多导入 1000 行。')
    rows, seen = [], set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        entry = {'line': number}
        try:
            parts = line.split('----')
            if not 2 <= len(parts) <= 4:
                raise ValueError('字段数量应为 2～4 段，使用四个半角减号分隔。')
            name = email(parts[0])
            normalized = name.lower()
            if normalized in seen:
                raise ValueError('本批次邮箱重复，请保留一行。')
            seen.add(normalized)
            values = {'password': parts[1]}
            if len(parts) >= 3:
                values['extra'] = parts[2]
            if len(parts) == 4:
                values['code_url'] = parts[3]
            entry.update(email=name, normalized_email=normalized, secrets=secret_fields(values, creating=True))
        except ValueError as error:
            entry['error'] = str(error)
        rows.append(entry)
    if not rows:
        raise ValueError('请填写至少一行账号资料。')
    return rows
