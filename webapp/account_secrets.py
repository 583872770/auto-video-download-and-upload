"""Authenticated account secrets. The shared key never lives in the database."""
import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AccountSecrets:
    def __init__(self):
        value = os.environ.get('VIDEO_ACCOUNT_ENCRYPTION_KEY', '')
        try:
            self.key = base64.b64decode(value.encode('ascii'), altchars=b'-_', validate=True)
        except (ValueError, UnicodeError):
            self.key = b''
        if len(self.key) != 32:
            raise ValueError('账号资料加密密钥未配置或格式无效；请给所有管理实例配置同一个 VIDEO_ACCOUNT_ENCRYPTION_KEY（32 字节的 Base64 密钥）。')
        self.key_id = hashlib.sha256(self.key).hexdigest()[:24]

    def encrypt(self, identity, data):
        nonce = os.urandom(12)
        raw = json.dumps(data, ensure_ascii=False).encode()
        return base64.urlsafe_b64encode(nonce + AESGCM(self.key).encrypt(nonce, raw, identity.encode())).decode()

    def decrypt(self, row):
        if row['key_id'] != self.key_id:
            raise ValueError('当前实例的账号资料密钥与保存时不一致，请恢复原密钥；资料未被修改。')
        try:
            raw = base64.urlsafe_b64decode(row['secrets_ciphertext'])
            return json.loads(AESGCM(self.key).decrypt(raw[:12], raw[12:], row['id'].encode()))
        except (InvalidTag, ValueError, KeyError):
            raise ValueError('账号资料无法解密，请检查密钥及备份；资料未被修改。') from None
