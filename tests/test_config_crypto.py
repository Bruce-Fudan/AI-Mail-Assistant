from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from aimail.config import AppConfig, SettingsStore, is_valid_email, split_keywords
from aimail.secure_store import PREFIX, SecretBox
from aimail.util import AppPaths


class ConfigCryptoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.paths = AppPaths(Path(self.tmp.name) / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_secret_box_roundtrip(self):
        box = SecretBox(self.paths.secret_key)
        token = box.encrypt("sample-value-中文")
        self.assertTrue(token.startswith(PREFIX))
        self.assertNotIn("sample-value", token)
        self.assertEqual(box.decrypt(token), "sample-value-中文")
        self.assertEqual(box.encrypt(""), "")
        self.assertEqual(box.decrypt(""), "")

    def test_key_file_is_private_and_stable(self):
        box = SecretBox(self.paths.secret_key)
        token = box.encrypt("x")
        key_bytes = self.paths.secret_key.read_bytes()
        self.assertTrue(key_bytes)
        if os.name == "posix":
            self.assertEqual(self.paths.secret_key.stat().st_mode & 0o777, 0o600)
        box2 = SecretBox(self.paths.secret_key)
        self.assertEqual(box2.decrypt(token), "x")

    def test_settings_roundtrip_and_encryption_at_rest(self):
        store = SettingsStore(self.paths)
        cfg = AppConfig()
        cfg.account_email = "me@example.com"
        cfg.account_name = "测试助手"
        cfg.imap.host = "imap.example.com"
        cfg.imap.username = "me@example.com"
        cfg.imap.password = "test-imap-password"
        cfg.smtp.host = "smtp.example.com"
        cfg.smtp.password = "test-smtp-password"
        cfg.ai.api_key = "test-api-key"
        cfg.ai.ignore_output_limit = True
        cfg.ai.ignore_timeout = True
        cfg.ai.use_backend_defaults = True
        cfg.rule.keywords = ["发给我的AI", "help me"]
        store.save(cfg)

        raw = self.paths.settings.read_text(encoding="utf-8")
        for secret in ("test-imap-password", "test-smtp-password", "test-api-key"):
            self.assertNotIn(secret, raw)
        data = json.loads(raw)
        self.assertTrue(data["imap"]["password"].startswith(PREFIX))
        self.assertTrue(data["smtp"]["password"].startswith(PREFIX))
        self.assertTrue(data["ai"]["api_key"].startswith(PREFIX))
        if os.name == "posix":
            self.assertEqual(self.paths.settings.stat().st_mode & 0o777, 0o600)

        loaded = store.load()
        self.assertEqual(loaded.account_email, "me@example.com")
        self.assertEqual(loaded.imap.password, "test-imap-password")
        self.assertEqual(loaded.smtp.password, "test-smtp-password")
        self.assertEqual(loaded.ai.api_key, "test-api-key")
        self.assertTrue(loaded.ai.ignore_output_limit)
        self.assertTrue(loaded.ai.ignore_timeout)
        self.assertTrue(loaded.ai.use_backend_defaults)
        self.assertEqual(loaded.rule.keywords, ["发给我的AI", "help me"])
        self.assertTrue(loaded.is_configured())

    def test_bad_token_yields_warning_and_empty_secret(self):
        store = SettingsStore(self.paths)
        cfg = AppConfig()
        cfg.account_email = "me@example.com"
        cfg.imap.password = "test-password"
        store.save(cfg)
        data = json.loads(self.paths.settings.read_text(encoding="utf-8"))
        data["imap"]["password"] = PREFIX + "not-a-valid-token"
        self.paths.settings.write_text(json.dumps(data), encoding="utf-8")

        loaded = store.load()
        self.assertEqual(loaded.imap.password, "")
        self.assertTrue(any("IMAP 密码" in warning for warning in store.warnings))

    def test_malformed_nested_settings_structure_does_not_crash(self):
        store = SettingsStore(self.paths)
        self.paths.settings.write_text(
            json.dumps(
                {
                    "account_email": ["not", "a", "string"],
                    "imap": 5,
                    "smtp": [],
                    "rule": "oops",
                    "ai": None,
                    "behavior": 42,
                }
            ),
            encoding="utf-8",
        )
        cfg = store.load()
        self.assertIsInstance(cfg, AppConfig)
        self.assertEqual(cfg.smtp.host, "")
        self.assertEqual(cfg.rule.keywords, ["发给我的AI"])
        self.assertEqual(cfg.ai.base_url, "http://127.0.0.1:8000")
        self.assertTrue(any("格式异常" in warning for warning in store.warnings))

    def test_empty_keyword_list_is_not_silently_replaced(self):
        store = SettingsStore(self.paths)
        cfg = AppConfig()
        cfg.account_email = "me@example.com"
        cfg.rule.keywords = []
        store.save(cfg)
        loaded = store.load()
        self.assertEqual(loaded.rule.keywords, [])
        self.assertIn("触发关键词", loaded.invalid_fields())

    def test_reset_deletes_settings_and_state(self):
        store = SettingsStore(self.paths)
        store.save(AppConfig(account_email="a@b.com"))
        self.paths.state.write_text("{}", encoding="utf-8")
        self.assertTrue(self.paths.settings.exists())
        store.reset()
        self.assertFalse(self.paths.settings.exists())
        self.assertFalse(self.paths.state.exists())

    def test_password_that_looks_encrypted_is_still_encrypted(self):
        store = SettingsStore(self.paths)
        cfg = AppConfig()
        cfg.account_email = "me@example.com"
        cfg.imap.password = "enc:v1:placeholder-token"
        store.save(cfg)
        raw = json.loads(self.paths.settings.read_text(encoding="utf-8"))
        self.assertTrue(raw["imap"]["password"].startswith(PREFIX))
        self.assertEqual(store.load().imap.password, "enc:v1:placeholder-token")

    def test_invalid_key_reports_config_error_on_save(self):
        store = SettingsStore(self.paths)
        self.paths.secret_key.write_text("not-a-fernet-key\n", encoding="utf-8")
        cfg = AppConfig()
        cfg.account_email = "me@example.com"
        cfg.imap.password = "test-password"
        with self.assertRaises(Exception) as ctx:
            store.save(cfg)
        self.assertIn("加密", str(ctx.exception))

    def test_validation(self):
        cfg = AppConfig()
        self.assertFalse(cfg.is_configured())
        self.assertIn("IMAP 服务器地址", cfg.invalid_fields())
        cfg.account_email = "me@example.com"
        cfg.imap.host = "imap.example.com"
        cfg.imap.username = "me@example.com"
        cfg.imap.password = "x"
        cfg.smtp.host = "smtp.example.com"
        cfg.smtp.username = "me@example.com"
        cfg.smtp.password = "x"
        cfg.ai.base_url = "http://127.0.0.1:8000"
        cfg.rule.keywords = ["发给我的AI"]
        self.assertTrue(cfg.is_configured())
        cfg.smtp.use_auth = False
        cfg.smtp.username = ""
        cfg.smtp.password = ""
        self.assertTrue(cfg.is_configured())

    def test_helpers(self):
        self.assertTrue(is_valid_email("a@b.co"))
        self.assertFalse(is_valid_email("a@b"))
        self.assertEqual(split_keywords("a，b, c；d\n e"), ["a", "b", "c", "d", "e"])


if __name__ == "__main__":
    unittest.main()
