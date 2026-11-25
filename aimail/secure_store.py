"""Local credential encryption.

Secrets (IMAP/SMTP passwords and the AI API key) are encrypted with Fernet
(AES-128-CBC + HMAC) using a random key stored next to the settings file with
0600 permissions.  The settings file itself only ever contains ciphertext.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .util import secure_chmod

PREFIX = "enc:v1:"


class SecretError(Exception):
    """Raised when the local encryption key cannot be used."""


class DecryptError(SecretError):
    """Raised when a stored secret cannot be decrypted."""


class SecretBox:
    def __init__(self, key_path: Path):
        self.key_path = Path(key_path)
        self._fernet: Fernet | None = None

    # ------------------------------------------------------------------ key
    def _load_key(self) -> bytes:
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        if self.key_path.exists():
            try:
                key = self.key_path.read_bytes().strip()
            except OSError as exc:
                raise SecretError(f"无法读取加密密钥：{exc}") from exc
            if not key:
                raise SecretError(f"加密密钥文件为空：{self.key_path}")
            secure_chmod(self.key_path, 0o600)
            return key

        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL prevents two instances racing and overwriting each other's key.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self.key_path, flags, 0o600)
        except FileExistsError:
            return self.key_path.read_bytes().strip()
        with os.fdopen(fd, "wb") as handle:
            handle.write(key + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        secure_chmod(self.key_path, 0o600)
        return key

    @property
    def fernet(self) -> Fernet:
        if self._fernet is None:
            try:
                self._fernet = Fernet(self._load_key())
            except ValueError as exc:
                raise SecretError(
                    f"加密密钥格式无效（{self.key_path}），请备份后删除它再重新配置。"
                ) from exc
        return self._fernet

    # -------------------------------------------------------------- secrets
    @staticmethod
    def is_encrypted(value: str) -> bool:
        return isinstance(value, str) and value.startswith(PREFIX)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        token = self.fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return PREFIX + token

    def decrypt(self, stored: str) -> str:
        if not stored:
            return ""
        if not self.is_encrypted(stored):
            # Be forgiving with hand-edited/legacy plaintext configs.
            return stored
        try:
            return self.fernet.decrypt(stored[len(PREFIX) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
            raise DecryptError(
                "无法解密已保存的凭据：加密密钥可能已被更换。"
            ) from exc

    def key_permissions_note(self) -> str:
        if os.name != "posix":
            return ""
        try:
            mode = self.key_path.stat().st_mode & 0o777
        except OSError:
            return ""
        if mode & 0o077:
            return "警告：密钥文件权限较宽松，建议设置为 600。"
        return ""
