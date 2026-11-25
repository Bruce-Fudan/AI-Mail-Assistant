from __future__ import annotations

import socketserver
import threading
import unittest
from http.server import ThreadingHTTPServer
from email import message_from_bytes, policy
from pathlib import Path
from tempfile import TemporaryDirectory

from aimail.config import AppConfig
from aimail.monitor import MailMonitor, MonitorState, StateStore
from aimail.util import AppPaths
from tests.test_ai_client import _AIHandler
from tests.test_imap_client import _IMAPHandler
from tests.test_mailer import _SMTPHandler, make_mail


class FullPipelineTests(unittest.TestCase):
    """Real ImapClient + SmtpSender + AIClient against local fake servers."""

    @classmethod
    def setUpClass(cls):
        cls.imap = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _IMAPHandler)
        cls.imap.daemon_threads = True
        cls.imap.uidvalidity = 42
        cls.imap.seen = set()
        cls.imap.messages = {1: make_mail(body="请 发给我的AI 帮我写一句问候语")}
        threading.Thread(target=cls.imap.serve_forever, daemon=True).start()

        cls.smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
        cls.smtp.daemon_threads = True
        cls.smtp.messages = []
        threading.Thread(target=cls.smtp.serve_forever, daemon=True).start()

        cls.ai_server = ThreadingHTTPServer(("127.0.0.1", 0), _AIHandler)
        cls.ai_server.daemon_threads = True
        cls.ai_server.requests = []
        cls.ai_server.status = 200
        cls.ai_server.answer = "这是集成测试的 AI 回答。"
        threading.Thread(target=cls.ai_server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.imap, cls.smtp, cls.ai_server):
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.smtp.messages.clear()
        self.ai_server.requests.clear()
        self.imap.seen.clear()

    def test_matched_mail_gets_ack_and_ai_reply(self):
        with TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp) / "data")
            paths.ensure()
            state_store = StateStore(paths.state)
            state_store.save(MonitorState(uidvalidity=42, last_uid=1))

            config = AppConfig()
            config.account_email = "me@example.com"
            config.account_name = "AI 助手"
            config.imap.host = "127.0.0.1"
            config.imap.port = self.imap.server_address[1]
            config.imap.security = "none"
            config.imap.username = "me@example.com"
            config.imap.password = "test-password"
            config.smtp.host = "127.0.0.1"
            config.smtp.port = self.smtp.server_address[1]
            config.smtp.security = "none"
            config.smtp.use_auth = False
            config.rule.keywords = ["发给我的AI"]
            config.ai.base_url = f"http://127.0.0.1:{self.ai_server.server_address[1]}"
            config.ai.model = "mock"
            config.ai.max_tokens = 128

            events = []
            monitor = MailMonitor(config, paths, events.append)
            detail = monitor._check_once()
            self.assertIn("匹配 1", detail)
            self.assertEqual(len(self.smtp.messages), 2)

            ack = message_from_bytes(self.smtp.messages[0], policy=policy.default)
            reply = message_from_bytes(self.smtp.messages[1], policy=policy.default)
            self.assertEqual(ack["Subject"], "Re: 请 发给我的AI 帮个忙")
            self.assertEqual(ack.get_content().strip(), "收到")
            self.assertEqual(reply["Subject"], ack["Subject"])
            self.assertIn("这是集成测试的 AI 回答。", reply.get_content())
            self.assertEqual(reply["To"], "Alice <alice@example.com>")
            self.assertEqual(reply["Auto-Submitted"], "auto-replied")
            self.assertEqual(self.imap.seen, {1})
            self.assertEqual(state_store.load().last_uid, 2)
            self.assertEqual(monitor.stats["replied"], 1)

            # The AI request actually reached the mock server.
            self.assertEqual(len(self.ai_server.requests), 1)
            self.assertEqual(self.ai_server.requests[0]["path"], "/v1/chat/completions")
            self.assertEqual(monitor.stats["errors"], 0)


if __name__ == "__main__":
    unittest.main()
