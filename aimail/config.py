"""Application settings, defaults and encrypted persistence."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .secure_store import DecryptError, SecretBox, SecretError
from .util import AppPaths, atomic_write_json, read_json

DEFAULT_KEYWORDS = ["发给我的AI"]
DEFAULT_ACK_TEXT = "收到"

SECURITY_LABELS = {
    "ssl": "SSL/TLS（推荐）",
    "starttls": "STARTTLS",
    "none": "不加密（仅本机）",
}
SECURITY_VALUES = list(SECURITY_LABELS.keys())

MATCH_SCOPE_LABELS = ["主题或正文", "仅主题", "仅正文"]


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or saved."""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，;；\n]+", value) if item.strip()]
    return list(default)


def split_keywords(text: str) -> list[str]:
    """Split a comma/分号/newline separated keyword string."""
    result: list[str] = []
    for item in re.split(r"[,，;；\n]+", text or ""):
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return result


def join_keywords(keywords: list[str]) -> str:
    return "，".join(k.strip() for k in keywords if k.strip())


def split_addresses(text: str) -> list[str]:
    result: list[str] = []
    for item in re.split(r"[,，;；\n]+", text or ""):
        item = item.strip().lower()
        if item and item not in result:
            result.append(item)
    return result


def is_valid_email(value: str) -> bool:
    value = (value or "").strip()
    return bool(re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", value))


@dataclass
class ImapConfig:
    host: str = ""
    port: int = 993
    security: str = "ssl"
    username: str = ""
    password: str = ""
    mailbox: str = "INBOX"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ImapConfig":
        data = _as_dict(data)
        security = _as_str(data.get("security"), "ssl")
        if security not in SECURITY_VALUES:
            security = "ssl"
        return cls(
            host=_as_str(data.get("host")).strip(),
            port=_as_int(data.get("port"), 993),
            security=security,
            username=_as_str(data.get("username")).strip(),
            password=_as_str(data.get("password")),
            mailbox=_as_str(data.get("mailbox"), "INBOX").strip() or "INBOX",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "security": self.security,
            "username": self.username,
            "password": self.password,
            "mailbox": self.mailbox,
        }


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 465
    security: str = "ssl"
    username: str = ""
    password: str = ""
    use_auth: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SmtpConfig":
        data = _as_dict(data)
        security = _as_str(data.get("security"), "ssl")
        if security not in SECURITY_VALUES:
            security = "ssl"
        return cls(
            host=_as_str(data.get("host")).strip(),
            port=_as_int(data.get("port"), 465),
            security=security,
            username=_as_str(data.get("username")).strip(),
            password=_as_str(data.get("password")),
            use_auth=_as_bool(data.get("use_auth"), True),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "security": self.security,
            "username": self.username,
            "password": self.password,
            "use_auth": self.use_auth,
        }


@dataclass
class RuleConfig:
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    match_subject: bool = True
    match_body: bool = True
    poll_seconds: int = 10
    process_existing_unread: bool = False
    ignore_auto_submitted: bool = True
    allowed_senders: list[str] = field(default_factory=list)

    @property
    def match_scope(self) -> int:
        if self.match_subject and not self.match_body:
            return 1
        if self.match_body and not self.match_subject:
            return 2
        return 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RuleConfig":
        data = _as_dict(data)
        if "keywords" in data:
            keywords = _as_list(data.get("keywords"), [])
        else:
            keywords = list(DEFAULT_KEYWORDS)
        return cls(
            keywords=keywords,
            match_subject=_as_bool(data.get("match_subject"), True),
            match_body=_as_bool(data.get("match_body"), True),
            poll_seconds=max(5, _as_int(data.get("poll_seconds"), 10)),
            process_existing_unread=_as_bool(data.get("process_existing_unread"), False),
            ignore_auto_submitted=_as_bool(data.get("ignore_auto_submitted"), True),
            allowed_senders=_as_list(data.get("allowed_senders"), []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "keywords": list(self.keywords),
            "match_subject": self.match_subject,
            "match_body": self.match_body,
            "poll_seconds": self.poll_seconds,
            "process_existing_unread": self.process_existing_unread,
            "ignore_auto_submitted": self.ignore_auto_submitted,
            "allowed_senders": list(self.allowed_senders),
        }


@dataclass
class AIConfig:
    base_url: str = "http://127.0.0.1:8000"
    endpoint_path: str = "/v1/chat/completions"
    model: str = ""
    api_key: str = ""
    system_prompt: str = ""
    temperature: float = 0.7
    max_tokens: int = 1024
    timeout_seconds: int = 120
    ignore_output_limit: bool = False
    ignore_timeout: bool = False
    use_backend_defaults: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AIConfig":
        data = _as_dict(data)
        return cls(
            base_url=_as_str(data.get("base_url"), "http://127.0.0.1:8000").strip(),
            endpoint_path=_as_str(data.get("endpoint_path"), "/v1/chat/completions").strip()
            or "/v1/chat/completions",
            model=_as_str(data.get("model")).strip(),
            api_key=_as_str(data.get("api_key")),
            system_prompt=_as_str(data.get("system_prompt"), ""),
            temperature=min(2.0, max(0.0, _as_float(data.get("temperature"), 0.7))),
            max_tokens=max(1, _as_int(data.get("max_tokens"), 1024)),
            timeout_seconds=max(5, _as_int(data.get("timeout_seconds"), 120)),
            ignore_output_limit=_as_bool(data.get("ignore_output_limit"), False),
            ignore_timeout=_as_bool(data.get("ignore_timeout"), False),
            use_backend_defaults=_as_bool(data.get("use_backend_defaults"), False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "endpoint_path": self.endpoint_path,
            "model": self.model,
            "api_key": self.api_key,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "ignore_output_limit": self.ignore_output_limit,
            "ignore_timeout": self.ignore_timeout,
            "use_backend_defaults": self.use_backend_defaults,
        }


@dataclass
class BehaviorConfig:
    ack_text: str = DEFAULT_ACK_TEXT
    send_error_notice: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BehaviorConfig":
        data = _as_dict(data)
        return cls(
            ack_text=_as_str(data.get("ack_text"), DEFAULT_ACK_TEXT).strip() or DEFAULT_ACK_TEXT,
            send_error_notice=_as_bool(data.get("send_error_notice"), False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ack_text": self.ack_text,
            "send_error_notice": self.send_error_notice,
        }


@dataclass
class AppConfig:
    version: int = 1
    account_email: str = ""
    account_name: str = "AI 邮件助手"
    imap: ImapConfig = field(default_factory=ImapConfig)
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    rule: RuleConfig = field(default_factory=RuleConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AppConfig":
        data = _as_dict(data)
        return cls(
            version=_as_int(data.get("version"), 1),
            account_email=_as_str(data.get("account_email")).strip(),
            account_name=_as_str(data.get("account_name"), "AI 邮件助手").strip()
            or "AI 邮件助手",
            imap=ImapConfig.from_dict(data.get("imap")),
            smtp=SmtpConfig.from_dict(data.get("smtp")),
            rule=RuleConfig.from_dict(data.get("rule")),
            ai=AIConfig.from_dict(data.get("ai")),
            behavior=BehaviorConfig.from_dict(data.get("behavior")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "account_email": self.account_email,
            "account_name": self.account_name,
            "imap": self.imap.to_dict(),
            "smtp": self.smtp.to_dict(),
            "rule": self.rule.to_dict(),
            "ai": self.ai.to_dict(),
            "behavior": self.behavior.to_dict(),
        }

    def copy(self) -> "AppConfig":
        return copy.deepcopy(self)

    # ----------------------------------------------------------- validation
    def from_address(self) -> str:
        return (self.account_email or self.imap.username or "").strip()

    def invalid_fields(self) -> list[str]:
        missing: list[str] = []
        if not is_valid_email(self.account_email):
            missing.append("邮箱地址（需要形如 name@example.com）")
        if not self.imap.host:
            missing.append("IMAP 服务器地址")
        if not (1 <= int(self.imap.port) <= 65535):
            missing.append("IMAP 端口（1–65535）")
        if not self.imap.username:
            missing.append("IMAP 用户名")
        if not self.imap.password:
            missing.append("IMAP 密码 / 授权码")
        if not self.smtp.host:
            missing.append("SMTP 服务器地址")
        if not (1 <= int(self.smtp.port) <= 65535):
            missing.append("SMTP 端口（1–65535）")
        if self.smtp.use_auth and not (self.smtp.username or self.account_email):
            missing.append("SMTP 用户名")
        if self.smtp.use_auth and not self.smtp.password:
            missing.append("SMTP 密码 / 授权码")
        if not self.rule.keywords:
            missing.append("触发关键词")
        if not self.ai.base_url:
            missing.append("AI 接口地址")
        return missing

    def is_configured(self) -> bool:
        return not self.invalid_fields()

    def summary(self) -> str:
        if not self.account_email:
            return "尚未配置邮箱"
        model = self.ai.model or "默认模型"
        return f"{self.account_email} · 关键词：{join_keywords(self.rule.keywords)} · {model}"


class SettingsStore:
    """Loads/saves settings, encrypting the secret fields on disk."""

    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.paths.ensure()
        self.box = SecretBox(paths.secret_key)
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ load
    def load(self) -> AppConfig:
        self.warnings = []
        if not self.paths.settings.exists():
            return AppConfig()
        try:
            data = read_json(self.paths.settings)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            self.warnings.append(f"无法读取设置文件，已使用默认设置：{exc}")
            return AppConfig()

        for section, label in (
            ("imap", "IMAP"),
            ("smtp", "SMTP"),
            ("rule", "触发规则"),
            ("ai", "AI 接口"),
            ("behavior", "自动回复"),
        ):
            value = data.get(section)
            if value is not None and not isinstance(value, dict):
                self.warnings.append(f"设置文件中的「{label}」段格式异常，已使用默认值。")
        try:
            cfg = AppConfig.from_dict(data)
        except Exception as exc:
            self.warnings.append(f"设置文件结构异常，已使用默认设置：{exc}")
            return AppConfig()
        if self.plaintext_secrets_present():
            self.warnings.append("检测到设置文件中的明文凭据；保存后会自动加密。")
        key_note = self.box.key_permissions_note()
        if key_note:
            self.warnings.append(key_note)
        secrets = [
            ("IMAP 密码", cfg.imap, "password"),
            ("SMTP 密码", cfg.smtp, "password"),
            ("AI API Key", cfg.ai, "api_key"),
        ]
        for label, target, attr in secrets:
            stored = getattr(target, attr)
            if not stored:
                continue
            try:
                setattr(target, attr, self.box.decrypt(stored))
            except DecryptError as exc:
                self.warnings.append(f"{label}：{exc}")
                setattr(target, attr, "")
            except SecretError as exc:
                self.warnings.append(f"{label}：{exc}")
                setattr(target, attr, "")

        if os.name == "posix":
            try:
                mode = self.paths.settings.stat().st_mode & 0o777
                if mode & 0o077:
                    self.warnings.append("设置文件权限较宽松（建议 600），已尝试收紧。")
                    try:
                        os.chmod(self.paths.settings, 0o600)
                    except OSError:
                        pass
            except OSError:
                pass
        return cfg

    # ------------------------------------------------------------------ save
    def save(self, cfg: AppConfig) -> None:
        self.paths.ensure()
        data = cfg.to_dict()
        try:
            # Encrypt secrets before they touch the disk. Empty values stay empty.
            data["imap"]["password"] = self.box.encrypt(cfg.imap.password)
            data["smtp"]["password"] = self.box.encrypt(cfg.smtp.password)
            data["ai"]["api_key"] = self.box.encrypt(cfg.ai.api_key)
            atomic_write_json(self.paths.settings, data)
        except SecretError as exc:
            raise ConfigError(f"无法加密凭据：{exc}") from exc
        except OSError as exc:
            raise ConfigError(f"无法保存设置：{exc}") from exc

    def reset(self) -> None:
        try:
            self.paths.settings.unlink(missing_ok=True)
            self.paths.state.unlink(missing_ok=True)
        except OSError as exc:
            raise ConfigError(f"无法清除设置：{exc}") from exc

    def plaintext_secrets_present(self) -> bool:
        """True if a hand-edited settings file still contains plaintext secrets."""
        try:
            data = read_json(self.paths.settings)
        except (OSError, ValueError, UnicodeError):
            return False
        for section, key in (("imap", "password"), ("smtp", "password"), ("ai", "api_key")):
            section_data = data.get(section)
            if not isinstance(section_data, dict):
                continue
            value = section_data.get(key)
            if value and not self.box.is_encrypted(value):
                return True
        return False
