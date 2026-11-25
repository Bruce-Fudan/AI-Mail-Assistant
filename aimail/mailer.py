"""IMAP fetching, keyword matching and SMTP reply delivery."""

from __future__ import annotations

import hashlib
import imaplib
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_email_policy
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from html.parser import HTMLParser
from typing import Iterable

from .config import AppConfig, ImapConfig, RuleConfig, SmtpConfig, join_keywords

MAX_BODY_CHARS = 200_000
AUTO_LOCAL_PARTS = {
    "mailer-daemon",
    "postmaster",
    "no-reply",
    "noreply",
    "do-not-reply",
    "donotreply",
    "bounce",
    "bounces",
}


class MailError(Exception):
    """A user-presentable mail protocol failure."""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {
        "br",
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "tr",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "hr",
    }
    _SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "li":
            self._parts.append("\n• ")
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = raw.replace("\r", "")
        raw = re.sub(r"[ \t\xa0]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(markup: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup or "").strip()
    return parser.text()


def _part_text(part) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if payload is None:
        try:
            value = part.get_payload()
            return value if isinstance(value, str) else ""
        except Exception:
            return ""
    charset = (part.get_content_charset() or "").strip()
    encodings = [charset, "utf-8", "gb18030", "big5", "latin-1"]
    seen: set[str] = set()
    for encoding in encodings:
        encoding = (encoding or "").lower()
        if not encoding or encoding in seen:
            continue
        seen.add(encoding)
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def extract_body(message, max_chars: int = MAX_BODY_CHARS) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart():
                continue
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment" or part.get_filename():
                continue
            ctype = (part.get_content_type() or "").lower()
            if ctype == "text/plain":
                text = _part_text(part).strip()
                if text:
                    plain_parts.append(text)
            elif ctype == "text/html":
                text = _part_text(part).strip()
                if text:
                    html_parts.append(text)
    else:
        ctype = (message.get_content_type() or "").lower()
        text = _part_text(message).strip()
        if ctype == "text/html":
            if text:
                html_parts.append(text)
        elif text:
            plain_parts.append(text)

    body = "\n\n".join(plain_parts).strip()
    if not body and html_parts:
        body = html_to_text("\n".join(html_parts))
    body = body.replace("\x00", "")
    return body[:max_chars]


def _safe_header(message, name: str) -> str:
    try:
        value = message.get(name, "")
    except Exception:
        return ""
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


@dataclass
class ParsedMail:
    message_id: str
    fingerprint: str
    subject: str
    sender_name: str
    sender_email: str
    reply_to: str
    to: str
    date: str
    body: str
    references: str
    auto_submitted: str
    precedence: str
    list_id: str
    list_unsubscribe: str
    auto_response_suppress: str
    is_test: bool = False
    is_generated: bool = False
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def display_sender(self) -> str:
        if self.sender_name and self.sender_email:
            return f"{self.sender_name} <{self.sender_email}>"
        return self.sender_email or self.sender_name or "未知发件人"


def parse_mail(raw: bytes) -> ParsedMail:
    message = message_from_bytes(raw, policy=default_email_policy)
    from_header = _safe_header(message, "From")
    name, address = parseaddr(from_header)
    reply_to_header = _safe_header(message, "Reply-To")
    _, reply_to = parseaddr(reply_to_header)
    message_id = _safe_header(message, "Message-ID")
    fingerprint = message_id or "sha256:" + hashlib.sha256(raw).hexdigest()
    body = extract_body(message)
    headers: dict[str, str] = {}
    for header in ("Auto-Submitted", "Precedence", "List-Id", "List-Unsubscribe"):
        headers[header] = _safe_header(message, header)
    return ParsedMail(
        message_id=message_id,
        fingerprint=fingerprint,
        subject=_safe_header(message, "Subject"),
        sender_name=name,
        sender_email=address.strip(),
        reply_to=(reply_to or "").strip(),
        to=_safe_header(message, "To"),
        date=_safe_header(message, "Date"),
        body=body,
        references=_safe_header(message, "References"),
        auto_submitted=_safe_header(message, "Auto-Submitted"),
        precedence=_safe_header(message, "Precedence"),
        list_id=_safe_header(message, "List-Id"),
        list_unsubscribe=_safe_header(message, "List-Unsubscribe"),
        auto_response_suppress=_safe_header(message, "X-Auto-Response-Suppress"),
        is_test=bool(_safe_header(message, "X-AI-Mail-Assistant-Test")),
        is_generated=bool(_safe_header(message, "X-AI-Mail-Assistant")),
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def match_keyword(mail: ParsedMail, rule: RuleConfig) -> str | None:
    include_subject = rule.match_subject or not rule.match_body
    include_body = rule.match_body or not rule.match_subject
    haystack_parts: list[str] = []
    if include_subject:
        haystack_parts.append(mail.subject)
    if include_body:
        haystack_parts.append(mail.body)
    haystack = "\n".join(haystack_parts).casefold()
    if not haystack.strip():
        return None
    for keyword in rule.keywords:
        keyword = keyword.strip()
        if keyword and keyword.casefold() in haystack:
            return keyword
    return None


def _sender_allowed(email: str, allowed: Iterable[str]) -> bool:
    allowed_values = [item.strip().lower() for item in allowed if item.strip()]
    if not allowed_values:
        return True
    email = (email or "").strip().lower()
    if not email:
        return False
    _, _, domain = email.partition("@")
    for item in allowed_values:
        item = item.lower()
        if item in {"*", "all", "any"}:
            return True
        if item.startswith("*@") and domain == item[2:]:
            return True
        if item.startswith("@") and domain == item[1:]:
            return True
        if item == email:
            return True
        if item == domain:
            return True
    return False


def ignore_reason(mail: ParsedMail, config: AppConfig) -> str | None:
    """Return a human-readable reason to skip this mail, or None if it is eligible."""
    own_addresses = {
        (config.account_email or "").strip().lower(),
        (config.imap.username or "").strip().lower(),
        (config.smtp.username or "").strip().lower(),
    }
    own_addresses.discard("")
    sender = (mail.sender_email or "").strip().lower()
    if not sender:
        return "无法识别发件人地址"
    # Never process our own generated acknowledgement / AI reply, even if the
    # user whitelisted their own address for self-testing.
    if mail.is_generated:
        return "本应用生成的自动回复"
    if mail.is_test:
        return "本应用发送的测试邮件"
    if config.rule.ignore_auto_submitted:
        auto_submitted = (mail.auto_submitted or "").lower()
        if auto_submitted and auto_submitted != "no":
            return f"自动提交邮件（Auto-Submitted: {mail.auto_submitted}）"
        if mail.auto_response_suppress:
            return "发件人要求不要自动回复（X-Auto-Response-Suppress）"
        if (mail.precedence or "").lower() in {"bulk", "list", "junk"}:
            return f"批量/列表邮件（Precedence: {mail.precedence}）"
        if mail.list_id or mail.list_unsubscribe:
            return "邮件列表/订阅邮件"
    local_part = sender.split("@", 1)[0]
    if local_part in AUTO_LOCAL_PARTS:
        return "系统/退信地址"

    allowed = config.rule.allowed_senders
    sender_is_own = sender in own_addresses
    if sender_is_own:
        # Sending from the same mailbox to itself is a very common way to test.
        # Only allow it when the user explicitly whitelisted their own address;
        # generated replies are filtered above, so this cannot form a loop.
        if not (allowed and _sender_allowed(sender, allowed)):
            return "发件人是本账号自己（如需自测，请把本账号加入发件人白名单）"
        if mail.auto_submitted or mail.auto_response_suppress:
            return "本账号的自动提交邮件"
    reply_to = (mail.reply_to or "").strip().lower()
    if reply_to and reply_to in own_addresses and not sender_is_own:
        return "回复地址是本账号自己"
    if allowed:
        if not _sender_allowed(sender, allowed):
            return "发件人不在允许列表中"
        # Reply to the effective destination only if it is trusted too; a
        # spoofed allowlisted From must not redirect the reply off-list.
        if reply_to and not _sender_allowed(reply_to, allowed):
            return "Reply-To 地址不在允许列表中"
    return None


# ---------------------------------------------------------------------------
# Reply construction
# ---------------------------------------------------------------------------


def _sanitize_header(value: str) -> str:
    return (value or "").replace("\r", " ").replace("\n", " ").strip()


def _reply_subject(subject: str, prefix: str = "Re: ") -> str:
    subject = _sanitize_header(subject) or "（无主题）"
    if re.match(r"^\s*re\s*:", subject, re.IGNORECASE):
        return subject
    return prefix + subject


def _from_address(config: AppConfig) -> str:
    return (config.account_email or config.imap.username or "").strip()


def format_ai_reply_body(sender_email: str, ai_text: str, model_name: str) -> str:
    """Wrap the raw AI output in the requested mail salutation/signature."""
    return f"Dear {sender_email},\n\n{ai_text}\n\nYours,\n{model_name}"


def build_reply(mail: ParsedMail, config: AppConfig, body: str) -> EmailMessage:
    from_email = _from_address(config)
    if not from_email:
        raise MailError("未配置发件邮箱地址")
    to_email = (mail.reply_to or mail.sender_email or "").strip()
    if not to_email:
        raise MailError("无法确定回复收件人")

    message = EmailMessage()
    display_name = (config.account_name or "").strip()
    message["From"] = formataddr((display_name, from_email)) if display_name else from_email
    message["To"] = (
        formataddr((_sanitize_header(mail.sender_name), to_email))
        if mail.sender_name
        else to_email
    )
    message["Subject"] = _reply_subject(mail.subject)
    message["Date"] = formatdate(localtime=True)
    domain = from_email.rsplit("@", 1)[-1] if "@" in from_email else "localhost"
    message["Message-ID"] = make_msgid(domain=domain)
    if mail.message_id:
        message["In-Reply-To"] = mail.message_id
        references = " ".join(x for x in (mail.references, mail.message_id) if x).strip()
        if references:
            message["References"] = references
    # RFC 3834 headers prevent mail loops with other auto-responders.
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"
    message["X-AI-Mail-Assistant"] = "1"
    message.set_content((body or "").strip() or "（没有内容）", subtype="plain", charset="utf-8")
    return message


def build_test_message(config: AppConfig) -> EmailMessage:
    from_email = _from_address(config)
    to_email = (config.account_email or "").strip()
    if not to_email:
        raise MailError("未配置邮箱地址")
    message = EmailMessage()
    display_name = (config.account_name or "").strip()
    message["From"] = formataddr((display_name, from_email)) if display_name else from_email
    message["To"] = to_email
    message["Subject"] = "[AI 邮件助手] 测试邮件"
    message["Date"] = formatdate(localtime=True)
    domain = from_email.rsplit("@", 1)[-1] if "@" in from_email else "localhost"
    message["Message-ID"] = make_msgid(domain=domain)
    message["Auto-Submitted"] = "auto-generated"
    message["X-AI-Mail-Assistant-Test"] = "1"
    message.set_content(
        "这是一封由 AI 邮件助手发送的测试邮件。\n\n"
        "如果你能在收件箱中看到它，说明 SMTP 发信配置正确。\n"
        f"当前触发关键词：{join_keywords(config.rule.keywords)}\n",
        subtype="plain",
        charset="utf-8",
    )
    return message


# ---------------------------------------------------------------------------
# IMAP
# ---------------------------------------------------------------------------


class ImapClient:
    def __init__(self, config: ImapConfig):
        self.config = config
        self._conn: imaplib.IMAP4 | None = None
        self.uidvalidity: int | None = None
        self.exists: int = 0

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "ImapClient":
        try:
            self.connect()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        cfg = self.config
        if not cfg.host:
            raise MailError("未配置 IMAP 服务器地址")
        if not (1 <= int(cfg.port) <= 65535):
            raise MailError("IMAP 端口无效")
        context = ssl.create_default_context()
        try:
            if cfg.security == "ssl":
                conn = imaplib.IMAP4_SSL(
                    cfg.host, int(cfg.port), ssl_context=context, timeout=30
                )
            else:
                conn = imaplib.IMAP4(cfg.host, int(cfg.port), timeout=30)
            self._conn = conn
            if cfg.security == "starttls":
                conn.starttls(context)
            conn.login(cfg.username, cfg.password)
        except imaplib.IMAP4.error as exc:
            detail = str(exc)
            upper = detail.upper()
            if any(
                marker in upper
                for marker in ("AUTHENTICATIONFAILED", "LOGIN", "AUTHENTICATE", "CREDENTIALS")
            ):
                raise MailError(
                    "IMAP 登录失败。请确认用户名和密码；Gmail、QQ、163 等邮箱通常需要使用"
                    "应用专用密码/授权码，而不是网页登录密码。"
                ) from exc
            raise MailError(f"IMAP 操作失败：{detail}") from exc
        except (ssl.SSLError, ssl.CertificateError) as exc:
            raise MailError(f"IMAP SSL 连接失败：{exc}") from exc
        except (socket.gaierror, TimeoutError, OSError) as exc:
            raise MailError(f"无法连接 IMAP 服务器 {cfg.host}:{cfg.port}：{exc}") from exc

        try:
            self._send_client_id()
        except Exception:
            pass
        try:
            status, data = conn.select(cfg.mailbox or "INBOX", readonly=False)
            if status != "OK":
                raise MailError(f"无法打开 IMAP 文件夹 {cfg.mailbox or 'INBOX'}")
            try:
                self.exists = int(data[0])
            except (TypeError, ValueError, IndexError):
                self.exists = 0
            status, data = conn.response("UIDVALIDITY")
            if status == "UIDVALIDITY" and data and data[0] is not None:
                try:
                    self.uidvalidity = int(data[0])
                except (TypeError, ValueError):
                    self.uidvalidity = None
        except imaplib.IMAP4.error as exc:
            raise MailError(f"无法打开收件箱：{exc}") from exc

    def _send_client_id(self) -> None:
        """Best effort IMAP ID (required by 163/126/188 for some clients)."""
        host = (self.config.host or "").lower()
        if not any(marker in host for marker in ("163.com", "126.com", "188.com", "yeah.net")):
            return
        assert self._conn is not None
        try:
            self._conn._simple_command(
                "ID",
                '("name" "AI Mail Assistant" "version" "1.0" "vendor" "local")',
            )
            self._conn._untagged_response("OK", [None], "ID")
        except Exception:
            pass

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.logout()
        except Exception:
            try:
                conn.shutdown()
            except Exception:
                pass

    # -- queries -----------------------------------------------------------
    def _require(self) -> imaplib.IMAP4:
        if self._conn is None:
            raise MailError("IMAP 连接未建立")
        return self._conn

    @staticmethod
    def _parse_uids(data) -> list[int]:
        if not data or data[0] is None:
            return []
        raw = data[0]
        if isinstance(raw, list):
            values = b" ".join(raw)
        else:
            values = raw
        if isinstance(values, bytes):
            values = values.decode("ascii", errors="ignore")
        uids: list[int] = []
        for item in str(values).split():
            try:
                uids.append(int(item))
            except ValueError:
                continue
        return uids

    def max_uid(self) -> int | None:
        conn = self._require()
        status, data = conn.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise MailError("IMAP 搜索失败")
        uids = self._parse_uids(data)
        return max(uids) if uids else None

    def uids_since(self, last_uid: int) -> list[int]:
        conn = self._require()
        status, data = conn.uid("SEARCH", None, f"UID {int(last_uid)}:*")
        if status != "OK":
            raise MailError("IMAP 搜索新邮件失败")
        # IMAP's "n:*" also returns the highest existing UID when n > max;
        # filtering is required.
        return sorted(uid for uid in self._parse_uids(data) if uid >= int(last_uid))

    def unseen_uids(self) -> list[int]:
        conn = self._require()
        status, data = conn.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            raise MailError("IMAP 搜索未读邮件失败")
        return sorted(self._parse_uids(data))

    def fetch(self, uid: int) -> tuple[set[str], bytes]:
        conn = self._require()
        status, data = conn.uid("FETCH", str(int(uid)), "(FLAGS BODY.PEEK[])")
        if status != "OK":
            raise MailError(f"读取邮件失败（UID {uid}）")
        raw = b""
        flags: set[str] = set()
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            head, body = item[0], item[1]
            if isinstance(body, bytes):
                raw += body
            if isinstance(head, bytes):
                match = re.search(rb"FLAGS \(([^)]*)\)", head)
                if match:
                    flags.update(
                        part.decode("ascii", errors="ignore")
                        for part in match.group(1).split()
                    )
        return flags, raw

    def mark_seen(self, uid: int) -> None:
        conn = self._require()
        try:
            conn.uid("STORE", str(int(uid)), "+FLAGS", "(\\Seen)")
        except Exception:
            pass

    def test_connection(self) -> int:
        """Connect, log in, select the inbox; returns the number of messages."""
        with self:
            return self.exists


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------


class SmtpSender:
    def __init__(self, config: SmtpConfig, account_email: str, account_name: str = ""):
        self.config = config
        self.account_email = (account_email or config.username or "").strip()
        self.account_name = account_name.strip()
        self._conn: smtplib.SMTP | None = None

    def __enter__(self) -> "SmtpSender":
        try:
            self.connect()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        cfg = self.config
        if not cfg.host:
            raise MailError("未配置 SMTP 服务器地址")
        if not (1 <= int(cfg.port) <= 65535):
            raise MailError("SMTP 端口无效")
        context = ssl.create_default_context()
        try:
            if cfg.security == "ssl":
                conn = smtplib.SMTP_SSL(cfg.host, int(cfg.port), timeout=30, context=context)
            else:
                conn = smtplib.SMTP(cfg.host, int(cfg.port), timeout=30)
            self._conn = conn
            conn.ehlo()
            if cfg.security == "starttls":
                conn.starttls(context=context)
                conn.ehlo()
            if cfg.use_auth:
                username = cfg.username or self.account_email
                if not username:
                    raise MailError("未配置 SMTP 用户名")
                conn.login(username, cfg.password)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(
                "SMTP 认证失败。请检查账号和密码/授权码；"
                "很多邮箱需要“客户端授权码”或应用专用密码。"
            ) from exc
        except smtplib.SMTPServerDisconnected as exc:
            raise MailError("SMTP 服务器意外断开连接") from exc
        except (ssl.SSLError, ssl.CertificateError) as exc:
            raise MailError(f"SMTP SSL 连接失败：{exc}") from exc
        except (socket.gaierror, TimeoutError, OSError) as exc:
            raise MailError(f"无法连接 SMTP 服务器 {cfg.host}:{cfg.port}：{exc}") from exc
        except smtplib.SMTPException as exc:
            raise MailError(f"SMTP 操作失败：{exc}") from exc

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.quit()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def send_message(self, message: EmailMessage) -> None:
        conn = self._conn
        if conn is None:
            raise MailError("SMTP 连接未建立")
        from_addr = self.account_email
        to_addrs = [addr for _, addr in getaddresses(message.get_all("To", [])) if addr]
        if not from_addr:
            raise MailError("未配置发件邮箱")
        if not to_addrs:
            raise MailError("回复收件人为空")
        try:
            conn.send_message(message, from_addr=from_addr, to_addrs=to_addrs)
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailError(f"收件人被 SMTP 服务器拒绝：{exc}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"发送邮件失败：{exc}") from exc

    def send_test(self) -> None:
        message = build_test_message(
            AppConfig(
                account_email=self.account_email,
                account_name=self.account_name,
                imap=ImapConfig(username=self.account_email),
                smtp=self.config,
            )
        )
        self.send_message(message)

    def send_text(self, mail: ParsedMail, config: AppConfig, body: str) -> None:
        self.send_message(build_reply(mail, config, body))


def test_smtp(config: SmtpConfig, account_email: str, account_name: str) -> None:
    with SmtpSender(config, account_email, account_name) as sender:
        sender.send_test()


def test_imap(config: ImapConfig) -> int:
    with ImapClient(config) as client:
        return client.exists
