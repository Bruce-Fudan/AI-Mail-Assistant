"""Background mailbox monitor and auto-reply workflow."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .ai_client import AIClient, AIError
from .config import AppConfig
from .mailer import (
    ImapClient,
    MailError,
    ParsedMail,
    SmtpSender,
    build_reply,
    format_ai_reply_body,
    ignore_reason,
    match_keyword,
    parse_mail,
)
from .util import AppPaths, atomic_write_json

logger = logging.getLogger("aimail.monitor")

ERROR_NOTICE_TEXT = "抱歉，AI 服务暂时不可用，请稍后再试。"
MAX_HANDLED_IDS = 2000
MAX_PENDING_UIDS = 10_000
MAX_PER_POLL = 25
ERROR_LOG_INTERVAL = 60.0


@dataclass
class MonitorEvent:
    kind: str  # status | log | stats | check
    level: str = "info"
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class MonitorState:
    uidvalidity: int | None = None
    last_uid: int | None = None
    handled_message_ids: list[str] = field(default_factory=list)
    pending_uids: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MonitorState":
        data = data or {}
        uidvalidity = data.get("uidvalidity")
        last_uid = data.get("last_uid")
        try:
            uidvalidity = int(uidvalidity) if uidvalidity is not None else None
        except (TypeError, ValueError):
            uidvalidity = None
        try:
            last_uid = int(last_uid) if last_uid is not None else None
        except (TypeError, ValueError):
            last_uid = None
        if last_uid is not None and last_uid < 1:
            last_uid = None
        handled = data.get("handled_message_ids") or []
        if not isinstance(handled, list):
            handled = []
        handled = [str(item) for item in handled][-MAX_HANDLED_IDS:]
        pending: list[int] = []
        raw_pending = data.get("pending_uids") or []
        if isinstance(raw_pending, list):
            for item in raw_pending[:MAX_PENDING_UIDS]:
                try:
                    uid = int(item)
                except (TypeError, ValueError):
                    continue
                if uid >= 1:
                    pending.append(uid)
        return cls(
            uidvalidity=uidvalidity,
            last_uid=last_uid,
            handled_message_ids=handled,
            pending_uids=pending,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "uidvalidity": self.uidvalidity,
            "last_uid": self.last_uid,
            "handled_message_ids": self.handled_message_ids[-MAX_HANDLED_IDS:],
            "pending_uids": self.pending_uids[:MAX_PENDING_UIDS],
        }


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> MonitorState:
        with self._lock:
            if not self.path.exists():
                return MonitorState()
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict):
                    raise ValueError("state.json 不是 JSON 对象")
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                logger.warning("无法读取运行状态，将重新建立：%s", exc)
                return MonitorState()
        return MonitorState.from_dict(data)

    def save(self, state: MonitorState) -> None:
        with self._lock:
            atomic_write_json(self.path, state.to_dict())


class MailMonitor:
    """Polls the IMAP inbox and runs the ack -> AI -> reply workflow."""

    def __init__(
        self,
        config: AppConfig,
        paths: AppPaths,
        event_sink: Callable[[MonitorEvent], None] | None = None,
        *,
        imap_factory: Callable[..., Any] = ImapClient,
        smtp_factory: Callable[..., Any] = SmtpSender,
        ai_factory: Callable[..., Any] = AIClient,
    ):
        self.config = config
        self.paths = paths
        self._event_sink = event_sink
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory
        self._ai_factory = ai_factory
        self._state_store = StateStore(paths.state)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._stats = {"matched": 0, "acked": 0, "replied": 0, "errors": 0}
        self.last_check: str | None = None
        self.last_check_ok: bool | None = None
        self.last_error: str = ""
        self._last_error_key = ""
        self._last_error_time = 0.0

    # ------------------------------------------------------------------ API
    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and not self._stop.is_set())

    def is_stopping(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and self._stop.is_set())

    def is_alive(self) -> bool:
        """True while the worker thread exists, even if it is shutting down."""
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self.is_running():
                return False
            if self._thread and self._thread.is_alive():
                # Previous stop still in progress; do not overlap sessions.
                return False
            self._stop.clear()
            self._wake.clear()
            self._last_error_key = ""
            self._last_error_time = 0.0
            self._thread = threading.Thread(
                target=self._run, name="ai-mail-monitor", daemon=True
            )
            self._thread.start()
            return True

    def stop(self, wait: bool = False, timeout: float = 8.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if wait and thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def check_now(self) -> bool:
        if not self.is_running():
            return False
        self._wake.set()
        return True

    def close(self) -> None:
        self.stop(wait=True, timeout=5.0)

    # --------------------------------------------------------------- helpers
    def _emit(self, event: MonitorEvent) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(event)
        except Exception:
            logger.exception("事件回调失败")

    def _log(self, level: str, message: str) -> None:
        getattr(logger, level if level in {"debug", "info", "warning", "error"} else "info")(
            message
        )
        self._emit(MonitorEvent(kind="log", level=level, message=message))

    def _emit_status(self, state: str, detail: str = "") -> None:
        self._emit(MonitorEvent(kind="status", data={"state": state, "detail": detail}))

    def _emit_stats(self) -> None:
        self._emit(MonitorEvent(kind="stats", data=dict(self._stats)))

    def _emit_check(self, ok: bool, detail: str) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        self.last_check = stamp
        self.last_check_ok = ok
        if not ok:
            self.last_error = detail
        self._emit(
            MonitorEvent(
                kind="check",
                data={"ok": ok, "detail": detail, "time": stamp},
            )
        )

    def _log_error_throttled(self, message: str, key: str) -> None:
        now = time.monotonic()
        if key == self._last_error_key and now - self._last_error_time < ERROR_LOG_INTERVAL:
            logger.warning("%s", message)
            return
        self._last_error_key = key
        self._last_error_time = now
        self._log("error", message)

    # ------------------------------------------------------------- lifecycle
    def _run(self) -> None:
        interval = max(5, int(self.config.rule.poll_seconds))
        self._emit_status("running", "监控运行中")
        self._log("info", f"开始监控 {self.config.account_email}，每 {interval} 秒检查一次")
        try:
            while not self._stop.is_set():
                try:
                    detail = self._check_once()
                    self._log("debug", detail)
                    self._emit_check(True, detail)
                except Exception as exc:  # protocol, network, or unexpected failure
                    message = self._friendly_exception(exc)
                    self._stats["errors"] += 1
                    self._emit_stats()
                    self._log_error_throttled(message, key=f"{type(exc).__name__}:{message}")
                    self._emit_check(False, message)
                    logger.debug("检查邮件时出错", exc_info=True)
                if self._stop.is_set():
                    break
                self._wake.wait(interval)
                self._wake.clear()
        finally:
            self._emit_status("stopped", "监控已停止")
            self._log("info", "监控已停止")

    @staticmethod
    def _friendly_exception(exc: Exception) -> str:
        if isinstance(exc, (MailError, AIError)):
            return str(exc)
        if isinstance(exc, OSError):
            return f"网络或文件操作失败：{exc}"
        return f"检查邮件失败：{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ poll
    def _check_once(self) -> str:
        state = self._state_store.load()
        with self._imap_factory(self.config.imap) as imap:
            uidvalidity = getattr(imap, "uidvalidity", None)
            needs_init = state.last_uid is None or (
                uidvalidity is not None and state.uidvalidity != uidvalidity
            )
            if needs_init:
                return self._initialize_baseline(imap, state, uidvalidity)

            matched = 0
            handled = 0

            # 1) New mail since the watermark gets priority, so someone writing
            # to us now is not stuck behind an opt-in historical backlog.
            uids = imap.uids_since(state.last_uid)
            batch = uids[:MAX_PER_POLL]
            for uid in batch:
                if self._stop.is_set():
                    break
                matched += self._process_uid(imap, uid, state)
                handled += 1
            if batch:
                self._log("info", f"发现 {len(batch)} 封新邮件，开始检查")

            # 2) Continue a historical backlog (from process_existing_unread)
            # using its explicit UID list. This avoids re-downloading every
            # already-read message between the backlog UIDs and the watermark.
            if state.pending_uids:
                # Drop pending UIDs that have been expunged or read elsewhere;
                # otherwise one vanished mail would poison the backlog forever.
                still_unseen = set(imap.unseen_uids())
                filtered = [uid for uid in state.pending_uids if uid in still_unseen]
                if filtered != state.pending_uids:
                    state.pending_uids = filtered
                    self._state_store.save(state)
            pending = list(state.pending_uids)
            capacity = MAX_PER_POLL - handled
            pending_seen = 0
            while pending and pending_seen < capacity and not self._stop.is_set():
                uid = pending.pop(0)
                state.pending_uids = pending
                try:
                    matched += self._process_uid(imap, uid, state)
                except MailError as exc:
                    # FETCH returned NO for this specific backlog entry; it is
                    # gone or unreadable. Do not let it poison the whole queue
                    # forever. Connection-level failures are not MailError.
                    self._log("warning", f"历史未读邮件 UID {uid} 无法读取，已跳过：{exc}")
                    self._state_store.save(state)
                pending_seen += 1
                handled += 1
            state.pending_uids = pending
            if pending_seen:
                self._state_store.save(state)
                self._log("info", f"继续处理历史未读邮件 {pending_seen} 封")

            if not handled:
                return "检查完成，没有新邮件"
            suffix = ""
            if len(batch) == MAX_PER_POLL or state.pending_uids:
                suffix = "（单轮处理上限，剩余邮件将在下一轮继续）"
            return f"检查完成，处理 {handled} 封邮件，匹配 {matched} 封{suffix}"

    def _initialize_baseline(self, imap, state: MonitorState, uidvalidity: int | None) -> str:
        is_change = state.uidvalidity is not None and uidvalidity != state.uidvalidity
        if is_change:
            self._log(
                "warning",
                "邮箱的 UIDVALIDITY 已变化（可能迁移或重建了邮箱），重新建立监控基线。",
            )
        max_uid = imap.max_uid()
        matched = 0
        consumed = 0
        unseen: list[int] = []
        # Always discard the previous watermark when establishing a new
        # baseline. Otherwise an old (possibly larger) last_uid would survive a
        # UIDVALIDITY change and hide every mail in the new mailbox.
        state.uidvalidity = uidvalidity
        state.last_uid = None
        state.pending_uids = []
        if self.config.rule.process_existing_unread:
            # A transient SEARCH failure must abort baseline creation rather
            # than silently marking the old unread mail as already handled.
            unseen = imap.unseen_uids()
            unseen = [uid for uid in unseen if max_uid is None or uid <= max_uid]
            if unseen:
                self._log("info", f"处理已有的 {len(unseen)} 封未读邮件")
                # Persist the backlog before processing so a quit/crash in the
                # middle of the initial batch cannot silently skip the rest.
                state.pending_uids = unseen[:MAX_PENDING_UIDS]
                for index, uid in enumerate(unseen[:MAX_PER_POLL]):
                    if self._stop.is_set():
                        break
                    state.pending_uids = unseen[index:][:MAX_PENDING_UIDS]
                    matched += self._process_uid(imap, uid, state)
                    consumed += 1
        leftover = unseen[consumed:]
        state.pending_uids = leftover[:MAX_PENDING_UIDS]
        state.last_uid = (max_uid + 1) if max_uid is not None else 1
        self._state_store.save(state)
        if matched:
            detail = f"已建立监控基线，自动处理了 {matched} 封已有匹配邮件"
        else:
            detail = (
                f"已建立监控基线（最新 UID {max_uid if max_uid is not None else '无'}），"
                "之后的新邮件会自动处理"
            )
        if state.pending_uids:
            detail += f"（还有 {len(state.pending_uids)} 封历史未读邮件将在后续轮次处理）"
        self._log("info", detail)
        return detail

    # --------------------------------------------------------------- one mail
    def _process_uid(self, imap, uid: int, state: MonitorState) -> int:
        """Handle one message. Returns 1 if it matched the keyword."""
        try:
            _flags, raw = imap.fetch(uid)
        except MailError:
            raise
        try:
            mail = parse_mail(raw)
        except Exception as exc:
            self._log("warning", f"无法解析邮件 UID {uid}，已跳过：{exc}")
            self._advance(state, uid)
            return 0

        if mail.fingerprint in state.handled_message_ids:
            self._advance(state, uid)
            return 0
        reason = ignore_reason(mail, self.config)
        subject = (mail.subject or "（无主题）").strip()
        if len(subject) > 80:
            subject = subject[:80] + "…"
        if reason:
            logger.debug("跳过邮件 UID %s：%s", uid, reason)
            self._log("info", f"跳过邮件「{subject}」：{reason}")
            self._advance(state, uid)
            return 0
        keyword = match_keyword(mail, self.config.rule)
        if not keyword:
            rule = self.config.rule
            if rule.match_subject and not rule.match_body:
                scope = "仅主题"
            elif rule.match_body and not rule.match_subject:
                scope = "仅正文"
            else:
                scope = "主题或正文"
            self._log("info", f"邮件「{subject}」未匹配关键词（当前匹配范围：{scope}）")
            self._advance(state, uid)
            return 0

        self._stats["matched"] += 1
        self._emit_stats()
        self._log(
            "info",
            f"匹配关键词「{keyword}」：{mail.subject or '（无主题）'} — {mail.display_sender}",
        )

        # Mark the message as read before any network reply is attempted. If
        # the process is quit or crashes during the (potentially slow) AI call,
        # the next launch must not fetch and acknowledge the same mail again.
        try:
            imap.mark_seen(uid)
        except Exception:
            pass

        claimed = False

        def claim() -> None:
            nonlocal claimed
            if claimed:
                return
            claimed = True
            if mail.fingerprint not in state.handled_message_ids:
                state.handled_message_ids.append(mail.fingerprint)
                state.handled_message_ids = state.handled_message_ids[-MAX_HANDLED_IDS:]
            self._advance(state, uid)

        try:
            self._handle_match(mail, claim)
        except Exception as exc:
            self._stats["errors"] += 1
            self._emit_stats()
            self._log("error", f"处理邮件时发生意外错误：{exc}")
            logger.debug("处理邮件失败", exc_info=True)
        finally:
            # Persist the claim even if the AI request or SMTP send failed or
            # was interrupted; otherwise a restart would send "收到" twice.
            claim()
            try:
                imap.mark_seen(uid)
            except Exception:
                pass
        return 1

    def _advance(self, state: MonitorState, uid: int) -> None:
        current = state.last_uid or 0
        state.last_uid = max(current, int(uid) + 1)
        try:
            self._state_store.save(state)
        except Exception as exc:
            logger.warning("保存运行状态失败：%s", exc)

    def _handle_match(self, mail: ParsedMail, claim) -> None:
        # 1) Immediately acknowledge so the sender knows the mail was received.
        ack_text = self.config.behavior.ack_text or "收到"
        try:
            self._send_body(mail, ack_text)
        except Exception as exc:
            self._stats["errors"] += 1
            self._emit_stats()
            self._log("error", f"发送“{ack_text}”确认邮件失败：{exc}")
        else:
            self._stats["acked"] += 1
            self._emit_stats()
            self._log("info", f"已回复“{ack_text}”给 {mail.sender_email}")
        finally:
            # Persist the handled marker before the AI call, so quitting during
            # a long request cannot cause a second acknowledgement on restart.
            claim()

        # 2) Ask the local AI and mail its answer back.
        try:
            ai_client = self._ai_factory(self.config.ai)
            # No prompt wrapping: the mail body itself is the user message.
            result = ai_client.generate(mail.body)
            answer = getattr(result, "text", "") or ""
            if not answer.strip():
                raise AIError("AI 返回了空内容")
            model_name = (
                (getattr(result, "model", "") or "").strip()
                or (self.config.ai.model or "").strip()
                or "AI"
            )
            answer = format_ai_reply_body(mail.sender_email, answer, model_name)
        except Exception as exc:
            self._stats["errors"] += 1
            self._emit_stats()
            self._log("error", f"AI 生成失败：{exc}")
            if self.config.behavior.send_error_notice:
                try:
                    self._send_body(mail, ERROR_NOTICE_TEXT)
                    self._log("info", "已向发件人发送 AI 失败提示")
                except Exception as send_exc:
                    self._log("error", f"发送失败提示也失败：{send_exc}")
            return

        try:
            self._send_body(mail, answer)
        except Exception as exc:
            self._stats["errors"] += 1
            self._emit_stats()
            self._log("error", f"发送 AI 回复失败：{exc}")
        else:
            self._stats["replied"] += 1
            self._emit_stats()
            self._log("info", f"已发送 AI 回复（{len(answer)} 字）给 {mail.sender_email}")

    def _send_body(self, mail: ParsedMail, body: str) -> None:
        sender = self._smtp_factory(
            self.config.smtp, self.config.account_email, self.config.account_name
        )
        with sender as smtp:
            message = build_reply(mail, self.config, body)
            smtp.send_message(message)
