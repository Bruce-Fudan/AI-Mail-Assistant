from __future__ import annotations

import re
import socketserver
import threading
import unittest

from aimail.config import ImapConfig
from aimail.mailer import ImapClient


class _IMAPHandler(socketserver.StreamRequestHandler):
    """Tiny IMAP4rev1 subset used to exercise the real imaplib client."""

    def handle(self):
        server = self.server
        self.wfile.write(b"* OK [CAPABILITY IMAP4rev1 UIDPLUS] test IMAP ready\r\n")
        selected = False
        while True:
            line = self.rfile.readline()
            if not line:
                break
            parts = line.rstrip(b"\r\n").split(b" ", 2)
            if len(parts) < 2:
                continue
            tag = parts[0]
            command = parts[1].upper()
            rest = parts[2] if len(parts) > 2 else b""

            if command == b"CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1 UIDPLUS\r\n")
                self.wfile.write(tag + b" OK CAPABILITY completed\r\n")
            elif command == b"LOGIN":
                self.wfile.write(tag + b" OK LOGIN completed\r\n")
            elif command == b"SELECT":
                selected = True
                self.wfile.write(f"* {len(server.messages)} EXISTS\r\n".encode())
                self.wfile.write(b"* 0 RECENT\r\n")
                self.wfile.write(f"* OK [UIDVALIDITY {server.uidvalidity}] UIDs valid\r\n".encode())
                self.wfile.write(f"* OK [UIDNEXT {max(server.messages, default=0) + 1}] next\r\n".encode())
                self.wfile.write(tag + b" OK [READ-WRITE] SELECT completed\r\n")
            elif command == b"UID" and selected:
                if not self._handle_uid(tag, rest):
                    break
            elif command == b"NOOP":
                self.wfile.write(tag + b" OK NOOP completed\r\n")
            elif command == b"LOGOUT":
                self.wfile.write(b"* BYE logging out\r\n")
                self.wfile.write(tag + b" OK LOGOUT completed\r\n")
                break
            elif command == b"CLOSE":
                selected = False
                self.wfile.write(tag + b" OK CLOSE completed\r\n")
            else:
                self.wfile.write(tag + b" BAD unsupported command\r\n")

    def _handle_uid(self, tag: bytes, rest: bytes) -> bool:
        server = self.server
        if rest.upper().startswith(b"SEARCH"):
            query = rest[6:].strip().upper()
            if query == b"ALL":
                uids = sorted(server.messages)
            elif query == b"UNSEEN":
                uids = sorted(uid for uid in server.messages if uid not in server.seen)
            else:
                match = re.match(rb"UID (\d+):\*", query)
                if match:
                    start = int(match.group(1))
                    uids = sorted(uid for uid in server.messages if uid >= start)
                    # Real IMAP returns the last message when start > max.
                    if not uids and server.messages:
                        uids = [max(server.messages)]
                else:
                    uids = sorted(server.messages)
            self.wfile.write(b"* SEARCH" + b"".join(f" {uid}".encode() for uid in uids) + b"\r\n")
            self.wfile.write(tag + b" OK SEARCH completed\r\n")
            return True
        if rest.upper().startswith(b"FETCH"):
            match = re.match(rb"FETCH (\d+)", rest, re.IGNORECASE)
            if not match:
                self.wfile.write(tag + b" BAD FETCH argument\r\n")
                return True
            uid = int(match.group(1))
            raw = server.messages.get(uid, b"")
            flags = b"\\Seen" if uid in server.seen else b""
            self.wfile.write(f"* 1 FETCH (UID {uid} FLAGS ({flags.decode()}) BODY[] {{{len(raw)}}}\r\n".encode())
            self.wfile.write(raw)
            self.wfile.write(b")\r\n")
            self.wfile.write(tag + b" OK FETCH completed\r\n")
            return True
        if rest.upper().startswith(b"STORE"):
            match = re.match(rb"STORE (\d+) \+FLAGS", rest, re.IGNORECASE)
            if match:
                uid = int(match.group(1))
                server.seen.add(uid)
                self.wfile.write(f"* 1 FETCH (UID {uid} FLAGS (\\Seen))\r\n".encode())
                self.wfile.write(tag + b" OK STORE completed\r\n")
            else:
                self.wfile.write(tag + b" BAD STORE argument\r\n")
            return True
        self.wfile.write(tag + b" BAD unsupported UID command\r\n")
        return True


class IMAPClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _IMAPHandler)
        cls.server.daemon_threads = True
        cls.server.uidvalidity = 42
        cls.server.seen = set()
        cls.server.messages = {
            1: b"From: a@example.com\r\nSubject: hello\r\n\r\nbody one\r\n",
            2: b"From: b@example.com\r\nSubject: second\r\n\r\nbody two\r\n",
        }
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.config = ImapConfig(
            host="127.0.0.1",
            port=cls.server.server_address[1],
            security="none",
            username="me@example.com",
            password="test-password",
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_select_and_uid_operations(self):
        with ImapClient(self.config) as client:
            self.assertEqual(client.uidvalidity, 42)
            self.assertEqual(client.exists, 2)
            self.assertEqual(client.max_uid(), 2)
            self.assertEqual(client.uids_since(1), [1, 2])
            self.assertEqual(client.unseen_uids(), [1, 2])
            flags, raw = client.fetch(1)
            self.assertIn("hello", raw.decode())
            self.assertNotIn("\\Seen", flags)
            client.mark_seen(1)
            self.assertEqual(client.unseen_uids(), [2])

    def test_test_connection(self):
        self.assertEqual(ImapClient(self.config).test_connection(), 2)


if __name__ == "__main__":
    unittest.main()
