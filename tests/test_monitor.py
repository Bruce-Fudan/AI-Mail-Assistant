from __future__ import annotations

import unittest
from email.message import EmailMessage
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from aimail.config import AppConfig
from aimail.mailer import MailError
from aimail.monitor import MailMonitor, MonitorEvent, MonitorState, StateStore
from aimail.util import AppPaths


def make_mail(
    subject: str = "请 发给我的AI 回复",
    body: str = "请帮我写一句问候语。",
    sender: str = "Alice <alice@example.com>",
    message_id: str = "<uid-mail@example.com>",
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "me@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    for key, value in (extra_headers or {}).items():
        message[key] = value
    message.set_content(body)
    return message.as_bytes()


def make_config() -> AppConfig:
    cfg = AppConfig()
    cfg.account_email = "me@example.com"
    cfg.account_name = "AI 助手"
    cfg.imap.host = "imap.example.com"
    cfg.imap.username = "me@example.com"
    cfg.imap.password = "test-password"
    cfg.smtp.host = "smtp.example.com"
    cfg.smtp.username = "me@example.com"
    cfg.smtp.password = "test-password"
    cfg.rule.keywords = ["发给我的AI"]
    cfg.ai.base_url = "http://127.0.0.1:8000"
    return cfg


class FakeImap:
    def __init__(self, uidvalidity: int, messages: dict[int, bytes]):
        self.uidvalidity = uidvalidity
        self.messages = dict(messages)
        self.flags: dict[int, set[str]] = {uid: set() for uid in self.messages}
        self.seen: set[int] = set()
        self.fetch_error: Exception | None = None
        self.fetch_missing: set[int] = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def max_uid(self):
        return max(self.messages) if self.messages else None

    def uids_since(self, last_uid: int):
        return sorted(uid for uid in self.messages if uid >= last_uid)

    def unseen_uids(self):
        return sorted(uid for uid in self.messages if uid not in self.seen)

    def fetch(self, uid: int):
        if self.fetch_error:
            raise self.fetch_error
        if uid in self.fetch_missing:
            raise MailError(f"读取邮件失败（UID {uid}）")
        return set(self.flags.get(uid, set())), self.messages[uid]

    def mark_seen(self, uid: int):
        self.seen.add(uid)
        self.flags.setdefault(uid, set()).add("\\Seen")


class FakeImapFactory:
    def __init__(self, client: FakeImap):
        self.client = client

    def __call__(self, config):
        return self.client


class FakeSmtp:
    def __init__(self, sink: list, config, account_email, account_name):
        self.sink = sink
        self.config = config
        self.account_email = account_email
        self.account_name = account_name

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def send_message(self, message):
        self.sink.append(message)


class FakeSmtpFactory:
    def __init__(self):
        self.sent: list[EmailMessage] = []

    def __call__(self, config, account_email, account_name):
        return FakeSmtp(self.sent, config, account_email, account_name)


class FakeAI:
    def __init__(self, answer: str = "AI 生成的回答。", error: Exception | None = None):
        self.answer = answer
        self.error = error

    def generate(self, prompt: str):
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.answer)


class FakeAIFactory:
    def __init__(self, answer: str = "AI 生成的回答。", error: Exception | None = None):
        self.answer = answer
        self.error = error

    def __call__(self, config):
        return FakeAI(self.answer, self.error)


def message_body(message: EmailMessage) -> str:
    payload = message.get_payload(decode=True)
    if payload:
        return payload.decode("utf-8")
    return str(message.get_content())


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.paths = AppPaths(Path(self.tmp.name) / "data")
        self.paths.ensure()
        self.config = make_config()
        self.events: list[MonitorEvent] = []
        self.smtp = FakeSmtpFactory()
        self.ai = FakeAIFactory()
        self.state_store = StateStore(self.paths.state)

    def tearDown(self):
        self.tmp.cleanup()

    def make_monitor(self, imap: FakeImap) -> MailMonitor:
        return MailMonitor(
            self.config,
            self.paths,
            self.events.append,
            imap_factory=FakeImapFactory(imap),
            smtp_factory=self.smtp,
            ai_factory=self.ai,
        )

    def test_ack_and_ai_reply(self):
        raw = make_mail()
        imap = FakeImap(42, {2: raw})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=2))
        monitor = self.make_monitor(imap)
        detail = monitor._check_once()

        self.assertIn("匹配 1", detail)
        self.assertEqual(len(self.smtp.sent), 2)
        ack, answer = self.smtp.sent
        self.assertEqual(ack["Subject"], "Re: 请 发给我的AI 回复")
        self.assertEqual(message_body(ack).strip(), "收到")
        self.assertIn("AI 生成的回答。", message_body(answer))
        self.assertEqual(ack["In-Reply-To"], "<uid-mail@example.com>")
        self.assertEqual(imap.seen, {2})
        state = self.state_store.load()
        self.assertEqual(state.last_uid, 3)
        self.assertIn("<uid-mail@example.com>", state.handled_message_ids)
        stats = monitor.stats
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["acked"], 1)
        self.assertEqual(stats["replied"], 1)

    def test_self_sent_mail_is_processed_when_whitelisted(self):
        self.config.rule.allowed_senders = ["me@example.com"]
        imap = FakeImap(42, {1: make_mail(sender="me@example.com")})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertEqual(message_body(self.smtp.sent[0]).strip(), "收到")

    def test_ai_receives_only_raw_body_and_reply_is_wrapped(self):
        captured = {}

        class CapturingAI:
            def generate(self, prompt):
                captured["prompt"] = prompt
                return SimpleNamespace(text="模型正文", model="qwen-test")

        class Factory:
            def __call__(self, _config):
                return CapturingAI()

        raw = make_mail(body="只发送这段正文", sender="alice@example.com")
        imap = FakeImap(42, {1: raw})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = MailMonitor(
            self.config,
            self.paths,
            self.events.append,
            imap_factory=FakeImapFactory(imap),
            smtp_factory=self.smtp,
            ai_factory=Factory(),
        )
        monitor._check_once()
        self.assertEqual(captured["prompt"], "只发送这段正文")
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertEqual(
            message_body(self.smtp.sent[1]).rstrip("\n"),
            "Dear alice@example.com,\n\n模型正文\n\nYours,\nqwen-test",
        )

    def test_non_matching_and_auto_mail_are_skipped(self):
        normal = make_mail(subject="普通邮件", body="没有关键词")
        auto = make_mail(
            subject="发给我的AI 自动回复",
            extra_headers={"Auto-Submitted": "auto-replied"},
        )
        imap = FakeImap(42, {1: normal, 2: auto})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(self.smtp.sent, [])
        state = self.state_store.load()
        self.assertEqual(state.last_uid, 3)
        self.assertEqual(imap.seen, set())

    def test_seen_new_mail_is_still_processed(self):
        # A user may open the mail before the next poll; the UID watermark (not
        # the \\Seen flag) is the source of truth for "not handled yet".
        raw = make_mail()
        imap = FakeImap(42, {1: raw})
        imap.flags[1] = {"\\Seen"}
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertEqual(self.state_store.load().last_uid, 2)

    def test_first_run_baseline_does_not_process_old_mail(self):
        imap = FakeImap(42, {1: make_mail(), 2: make_mail(message_id="<two@x>")})
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(self.smtp.sent, [])
        state = self.state_store.load()
        self.assertEqual(state.uidvalidity, 42)
        self.assertEqual(state.last_uid, 3)
        self.assertTrue(any("基线" in event.message for event in self.events))

    def test_first_run_process_existing_unread(self):
        self.config.rule.process_existing_unread = True
        imap = FakeImap(42, {1: make_mail()})
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(len(self.smtp.sent), 2)
        state = self.state_store.load()
        self.assertEqual(state.last_uid, 2)

    def test_uidvalidity_change_rebuilds_baseline(self):
        imap = FakeImap(99, {1: make_mail()})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=10))
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(self.smtp.sent, [])
        state = self.state_store.load()
        self.assertEqual(state.uidvalidity, 99)
        self.assertEqual(state.last_uid, 2)
        self.assertTrue(any("UIDVALIDITY" in event.message for event in self.events))

    def test_dedupe_by_message_id(self):
        raw = make_mail()
        imap = FakeImap(42, {1: raw})
        self.state_store.save(
            MonitorState(
                uidvalidity=42,
                last_uid=1,
                handled_message_ids=["<uid-mail@example.com>"],
            )
        )
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(self.state_store.load().last_uid, 2)

    def test_ai_failure_still_sends_ack(self):
        self.ai = FakeAIFactory(error=RuntimeError("AI 不可用"))
        imap = FakeImap(42, {1: make_mail()})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(message_body(self.smtp.sent[0]).strip(), "收到")
        self.assertGreaterEqual(monitor.stats["errors"], 1)
        self.assertEqual(self.state_store.load().last_uid, 2)

    def test_ai_error_notice_optional(self):
        self.config.behavior.send_error_notice = True
        self.ai = FakeAIFactory(error=RuntimeError("AI 不可用"))
        imap = FakeImap(42, {1: make_mail()})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertIn("抱歉", message_body(self.smtp.sent[1]))

    def test_process_existing_chunks_large_unread_backlog(self):
        self.config.rule.process_existing_unread = True
        messages = {
            uid: make_mail(message_id=f"<msg-{uid}@example.com>")
            for uid in range(1, 31)
        }
        imap = FakeImap(42, messages)
        monitor = self.make_monitor(imap)
        detail = monitor._check_once()
        self.assertIn("还有 5 封", detail)
        state = self.state_store.load()
        self.assertEqual(state.last_uid, 31)
        self.assertEqual(state.pending_uids, list(range(26, 31)))
        self.assertEqual(len(self.smtp.sent), 50)

    def test_state_is_claimed_before_the_ai_call(self):
        observed = {}

        class InspectingAI:
            def generate(self, _prompt):
                observed["state"] = self.state_store.load()
                return SimpleNamespace(text="ok")

        class InspectingFactory:
            def __call__(self, _config):
                return self.ai_instance

        factory = InspectingFactory()
        factory.ai_instance = InspectingAI()
        factory.ai_instance.state_store = self.state_store
        imap = FakeImap(42, {1: make_mail()})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = MailMonitor(
            self.config,
            self.paths,
            self.events.append,
            imap_factory=FakeImapFactory(imap),
            smtp_factory=self.smtp,
            ai_factory=factory,
        )
        monitor._check_once()
        self.assertEqual(observed["state"].last_uid, 2)
        self.assertIn("<uid-mail@example.com>", observed["state"].handled_message_ids)

    def test_interrupted_ai_does_not_duplicate_ack_on_restart(self):
        self.ai = FakeAIFactory(error=KeyboardInterrupt())
        imap = FakeImap(42, {1: make_mail()})
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = self.make_monitor(imap)
        with self.assertRaises(KeyboardInterrupt):
            monitor._check_once()
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(self.state_store.load().last_uid, 2)

        # A fresh monitor (as after restarting the app) must not re-ack / re-reply.
        self.ai = FakeAIFactory()
        monitor2 = self.make_monitor(FakeImap(42, {1: make_mail()}))
        monitor2._check_once()
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertEqual(monitor2.stats["replied"], 0)

    def test_uidvalidity_change_with_large_old_watermark_keeps_backlog(self):
        # Regression: old UIDVALIDITY had a huge last_uid; the new mailbox has
        # lower UIDs. The baseline must not inherit the old watermark.
        self.config.rule.process_existing_unread = True
        messages = {
            uid: make_mail(message_id=f"<new-{uid}@example.com>")
            for uid in range(1, 31)
        }
        imap = FakeImap(99, messages)
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1_000_000))
        monitor = self.make_monitor(imap)
        first = monitor._check_once()
        self.assertIn("还有 5 封", first)
        state = self.state_store.load()
        self.assertEqual(state.last_uid, 31)
        self.assertEqual(state.pending_uids, list(range(26, 31)))
        second = monitor._check_once()
        self.assertIn("匹配 5", second)
        self.assertEqual(self.state_store.load().last_uid, 31)
        self.assertEqual(self.state_store.load().pending_uids, [])
        self.assertEqual(len(self.smtp.sent), 60)

    def test_pending_expunged_uid_is_dropped(self):
        self.config.rule.process_existing_unread = True
        imap = FakeImap(42, {1: make_mail()})
        # Simulate a backlog entry (UID 2) whose message no longer exists.
        self.state_store.save(
            MonitorState(uidvalidity=42, last_uid=100, pending_uids=[1, 2])
        )
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(len(self.smtp.sent), 2)
        state = self.state_store.load()
        self.assertEqual(state.pending_uids, [])
        self.assertEqual(state.last_uid, 100)

    def test_pending_fetch_no_is_dropped_and_queue_continues(self):
        self.config.rule.process_existing_unread = True
        imap = FakeImap(42, {1: make_mail(), 2: make_mail(message_id="<two@x>")})
        imap.fetch_missing = {2}
        self.state_store.save(
            MonitorState(uidvalidity=42, last_uid=100, pending_uids=[1, 2])
        )
        monitor = self.make_monitor(imap)
        monitor._check_once()
        self.assertEqual(len(self.smtp.sent), 2)
        state = self.state_store.load()
        self.assertEqual(state.pending_uids, [])
        self.assertGreaterEqual(monitor.stats["errors"], 0)
        self.assertTrue(any("无法读取" in event.message for event in self.events))

    def test_fetch_error_propagates(self):
        imap = FakeImap(42, {1: make_mail()})
        imap.fetch_error = MailError("连接断开")
        self.state_store.save(MonitorState(uidvalidity=42, last_uid=1))
        monitor = self.make_monitor(imap)
        with self.assertRaises(MailError):
            monitor._check_once()
        self.assertEqual(self.state_store.load().last_uid, 1)


if __name__ == "__main__":
    unittest.main()
