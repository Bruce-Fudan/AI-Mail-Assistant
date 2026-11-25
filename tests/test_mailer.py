from __future__ import annotations

import email
import email.policy
import socketserver
import threading
import unittest
from email.message import EmailMessage

from aimail.config import AppConfig
from aimail.mailer import (
    SmtpSender,
    build_reply,
    format_ai_reply_body,
    html_to_text,
    ignore_reason,
    match_keyword,
    parse_mail,
)


def make_mail(
    subject: str = "请 发给我的AI 帮个忙",
    body: str = "你好，请帮我写一段 Python 代码。",
    sender: str = "Alice <alice@example.com>",
    message_id: str = "<mail-1@example.com>",
    extra_headers: dict[str, str] | None = None,
    html_body: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "me@example.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 24 Nov 2025 10:00:00 +0800"
    message["Message-ID"] = message_id
    for key, value in (extra_headers or {}).items():
        message[key] = value
    if html_body is not None:
        message.set_content(body)
        message.add_alternative(html_body, subtype="html")
    else:
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


class ParsingAndRulesTests(unittest.TestCase):
    def test_parse_plain_mail(self):
        mail = parse_mail(make_mail())
        self.assertEqual(mail.sender_email, "alice@example.com")
        self.assertEqual(mail.sender_name, "Alice")
        self.assertIn("Python", mail.body)
        self.assertEqual(mail.message_id, "<mail-1@example.com>")

    def test_parse_multipart_uses_plain_text(self):
        raw = make_mail(body="纯文本内容", html_body="<p>HTML 内容</p>")
        mail = parse_mail(raw)
        self.assertIn("纯文本内容", mail.body)
        self.assertNotIn("<p>", mail.body)

    def test_html_fallback(self):
        message = EmailMessage()
        message["From"] = "a@example.com"
        message["Subject"] = "test"
        message.set_content("<p>Hello <b>world</b></p><p>Second line</p>", subtype="html")
        mail = parse_mail(message.as_bytes())
        self.assertIn("Hello", mail.body)
        self.assertIn("Second line", mail.body)
        self.assertNotIn("<p>", mail.body)

    def test_html_entities(self):
        text = html_to_text("<p>A &amp; B&nbsp;C</p><script>bad()</script>")
        self.assertIn("A & B C", text)
        self.assertNotIn("bad()", text)

    def test_keyword_matching_casefold_and_scope(self):
        cfg = make_config()
        mail = parse_mail(make_mail(subject="BUILD failed", body="please HELP me"))
        cfg.rule.keywords = ["help"]
        self.assertEqual(match_keyword(mail, cfg.rule), "help")
        cfg.rule.match_subject = True
        cfg.rule.match_body = False
        self.assertIsNone(match_keyword(mail, cfg.rule))
        cfg.rule.keywords = ["BUILD"]
        self.assertEqual(match_keyword(mail, cfg.rule), "BUILD")

    def test_ignore_own_and_auto_submitted(self):
        cfg = make_config()
        own = parse_mail(make_mail(sender="me@example.com"))
        self.assertIn("自己", ignore_reason(own, cfg))
        auto = parse_mail(make_mail(extra_headers={"Auto-Submitted": "auto-replied"}))
        self.assertIn("自动提交", ignore_reason(auto, cfg))
        test_mail = parse_mail(make_mail(extra_headers={"X-AI-Mail-Assistant-Test": "1"}))
        self.assertIsNotNone(ignore_reason(test_mail, cfg))
        normal = parse_mail(make_mail())
        self.assertIsNone(ignore_reason(normal, cfg))

    def test_allowlist(self):
        cfg = make_config()
        cfg.rule.allowed_senders = ["@example.com"]
        self.assertIsNone(ignore_reason(parse_mail(make_mail()), cfg))
        cfg.rule.allowed_senders = ["@other.com"]
        self.assertIn("不在", ignore_reason(parse_mail(make_mail()), cfg))

    def test_self_mail_can_be_enabled_via_whitelist(self):
        cfg = make_config()
        cfg.rule.allowed_senders = ["me@example.com"]
        self.assertIsNone(ignore_reason(parse_mail(make_mail(sender="me@example.com")), cfg))
        # Generated replies must never be processed, even with self whitelisted.
        generated = make_mail(
            sender="me@example.com",
            extra_headers={"X-AI-Mail-Assistant": "1"},
        )
        self.assertIn("生成", ignore_reason(parse_mail(generated), cfg))
        # Automatic submissions from self are still ignored when the user
        # turns off the generic auto-submitted filter.
        cfg.rule.ignore_auto_submitted = False
        auto = make_mail(
            sender="me@example.com",
            extra_headers={"Auto-Submitted": "auto-replied"},
        )
        self.assertIn("自动提交", ignore_reason(parse_mail(auto), cfg))

    def test_allowlist_blocks_untrusted_reply_to(self):
        cfg = make_config()
        cfg.rule.allowed_senders = ["@example.com"]
        raw = make_mail(extra_headers={"Reply-To": "victim@other.com"})
        self.assertIn("Reply-To", ignore_reason(parse_mail(raw), cfg))
        raw = make_mail(extra_headers={"Reply-To": "me@example.com"})
        self.assertIn("自己", ignore_reason(parse_mail(raw), cfg))

    def test_build_reply_headers(self):
        cfg = make_config()
        parsed = parse_mail(make_mail())
        reply = build_reply(parsed, cfg, "AI 的回答")
        self.assertEqual(reply["To"], "Alice <alice@example.com>")
        self.assertEqual(reply["Subject"], "Re: 请 发给我的AI 帮个忙")
        self.assertEqual(reply["In-Reply-To"], "<mail-1@example.com>")
        self.assertIn("<mail-1@example.com>", reply["References"])
        self.assertEqual(reply["Auto-Submitted"], "auto-replied")
        self.assertEqual(reply["X-Auto-Response-Suppress"], "All")
        self.assertIn("AI 的回答", reply.get_content())

    def test_reply_does_not_double_re_prefix(self):
        cfg = make_config()
        parsed = parse_mail(make_mail(subject="Re: hello"))
        self.assertEqual(build_reply(parsed, cfg, "x")["Subject"], "Re: hello")

    def test_ai_reply_email_format(self):
        body = format_ai_reply_body(
            "alice@example.com", "这是模型输出。", "qwen-test"
        )
        self.assertEqual(
            body,
            "Dear alice@example.com,\n\n这是模型输出。\n\nYours,\nqwen-test",
        )

    def test_references_chain(self):
        cfg = make_config()
        raw = make_mail(extra_headers={"References": "<root@example.com> <parent@example.com>"})
        reply = build_reply(parse_mail(raw), cfg, "x")
        self.assertEqual(
            reply["References"],
            "<root@example.com> <parent@example.com> <mail-1@example.com>",
        )


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 test.local ESMTP\r\n")
        data_mode = False
        lines: list[bytes] = []
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if data_mode:
                if line in (b".\r\n", b".\n"):
                    data_mode = False
                    self.server.messages.append(b"".join(lines))
                    lines = []
                    self.wfile.write(b"250 2.0.0 queued\r\n")
                else:
                    lines.append(line)
                continue
            command = line.strip().upper()
            if command.startswith(b"EHLO"):
                self.wfile.write(b"250-test.local\r\n250 HELP\r\n")
            elif command.startswith(b"HELO"):
                self.wfile.write(b"250 test.local\r\n")
            elif command.startswith(b"MAIL FROM"):
                self.wfile.write(b"250 2.1.0 ok\r\n")
            elif command.startswith(b"RCPT TO"):
                self.wfile.write(b"250 2.1.5 ok\r\n")
            elif command == b"DATA":
                data_mode = True
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
            elif command == b"RSET":
                self.wfile.write(b"250 ok\r\n")
            elif command == b"NOOP":
                self.wfile.write(b"250 ok\r\n")
            elif command == b"QUIT":
                self.wfile.write(b"221 bye\r\n")
                break
            else:
                self.wfile.write(b"250 ok\r\n")


class SMTPDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
        cls.server.daemon_threads = True
        cls.server.messages = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.messages.clear()

    def test_send_reply_over_smtp(self):
        cfg = make_config()
        cfg.smtp.host = "127.0.0.1"
        cfg.smtp.port = self.server.server_address[1]
        cfg.smtp.security = "none"
        cfg.smtp.use_auth = False
        parsed = parse_mail(make_mail())
        with SmtpSender(cfg.smtp, cfg.account_email, cfg.account_name) as sender:
            sender.send_text(parsed, cfg, "这是 SMTP 测试回复")
        self.assertEqual(len(self.server.messages), 1)
        delivered = email.message_from_bytes(self.server.messages[0], policy=email.policy.default)
        self.assertEqual(delivered["To"], "Alice <alice@example.com>")
        self.assertEqual(delivered["Subject"], "Re: 请 发给我的AI 帮个忙")
        body = delivered.get_payload(decode=True).decode("utf-8")
        self.assertIn("这是 SMTP 测试回复", body)
        self.assertEqual(delivered["Auto-Submitted"], "auto-replied")

    def test_send_test_message(self):
        cfg = make_config()
        cfg.smtp.host = "127.0.0.1"
        cfg.smtp.port = self.server.server_address[1]
        cfg.smtp.security = "none"
        cfg.smtp.use_auth = False
        with SmtpSender(cfg.smtp, cfg.account_email, cfg.account_name) as sender:
            sender.send_test()
        self.assertEqual(len(self.server.messages), 1)
        delivered = email.message_from_bytes(self.server.messages[0], policy=email.policy.default)
        self.assertEqual(delivered["Subject"], "[AI 邮件助手] 测试邮件")
        self.assertEqual(delivered["X-AI-Mail-Assistant-Test"], "1")


if __name__ == "__main__":
    unittest.main()
