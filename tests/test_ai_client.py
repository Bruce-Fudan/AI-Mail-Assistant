from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from aimail.ai_client import AIError, AIClient, build_endpoint, normalize_base_url
from aimail.config import AIConfig


class _AIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        self.server.requests.append(
            {"path": self.path, "headers": dict(self.headers), "payload": payload}
        )
        status = self.server.status
        if status == 200:
            body = {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": payload.get("model", "mock-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.server.answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        else:
            body = {"error": {"message": "模拟错误", "type": "invalid_request_error"}}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AIClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _AIHandler)
        cls.server.daemon_threads = True
        cls.server.requests = []
        cls.server.status = 200
        cls.server.answer = "你好，这是 AI 回复。"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.requests.clear()
        self.server.status = 200
        self.server.answer = "你好，这是 AI 回复。"
        self.config = AIConfig(
            base_url=f"127.0.0.1:{self.port}",
            endpoint_path="/v1/chat/completions",
            model="test-model",
            api_key="test-api-key",
            system_prompt="你是一个测试助手。",
            max_tokens=100,
        )

    def test_url_helpers(self):
        self.assertEqual(
            normalize_base_url("127.0.0.1:8000"), "http://127.0.0.1:8000"
        )
        self.assertEqual(
            build_endpoint("http://127.0.0.1:8000"),
            "http://127.0.0.1:8000/v1/chat/completions",
        )
        self.assertEqual(
            build_endpoint("http://127.0.0.1:8000/v1"),
            "http://127.0.0.1:8000/v1/chat/completions",
        )
        self.assertEqual(
            build_endpoint("http://127.0.0.1:8000/v1/chat/completions"),
            "http://127.0.0.1:8000/v1/chat/completions",
        )

    def test_generate_success(self):
        result = AIClient(self.config).generate("请回答")
        self.assertEqual(result.text, "你好，这是 AI 回复。")
        self.assertEqual(result.model, "test-model")
        self.assertEqual(len(self.server.requests), 1)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["headers"].get("Authorization"), "Bearer test-api-key")
        payload = request["payload"]
        self.assertEqual(payload["model"], "test-model")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][0]["content"], "你是一个测试助手。")
        self.assertEqual(payload["messages"][-1]["content"], "请回答")
        self.assertEqual(payload["max_tokens"], 100)

    def test_generate_without_optional_fields(self):
        config = AIConfig(base_url=f"http://127.0.0.1:{self.port}", max_tokens=64)
        AIClient(config).generate("hi")
        payload = self.server.requests[0]["payload"]
        self.assertNotIn("model", payload)
        self.assertNotIn("Authorization", self.server.requests[0]["headers"])

    def test_system_prompt_is_sent_exactly_as_entered(self):
        self.config.system_prompt = "  保留前导空格和换行\n"
        AIClient(self.config).generate("hi")
        messages = self.server.requests[0]["payload"]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "  保留前导空格和换行\n")

    def test_empty_system_prompt_is_omitted(self):
        self.config.system_prompt = ""
        AIClient(self.config).generate("hi")
        messages = self.server.requests[0]["payload"]["messages"]
        self.assertFalse(any(item["role"] == "system" for item in messages))
        self.assertEqual(messages[-1]["content"], "hi")

    def test_unlimited_output_omits_max_tokens(self):
        self.config.ignore_output_limit = True
        AIClient(self.config).generate("hi")
        payload = self.server.requests[0]["payload"]
        self.assertNotIn("max_tokens", payload)

    def test_backend_defaults_omit_sampling_parameters(self):
        self.config.use_backend_defaults = True
        AIClient(self.config).generate("hi")
        payload = self.server.requests[0]["payload"]
        self.assertNotIn("temperature", payload)
        self.assertNotIn("top_p", payload)
        self.assertNotIn("top_k", payload)
        self.assertNotIn("thinking", payload)
        self.assertEqual(payload["messages"][-1]["content"], "hi")

    def test_ignore_timeout_passes_none_to_requests(self):
        captured = {}

        class FakeResponse:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "ok"}}]}

        def fake_post(_session, url, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return FakeResponse()

        self.config.ignore_timeout = True
        with patch("aimail.ai_client.requests.Session.post", new=fake_post):
            AIClient(self.config).generate("hi")
        self.assertIsNone(captured["timeout"])

        captured.clear()
        self.config.ignore_timeout = False
        with patch("aimail.ai_client.requests.Session.post", new=fake_post):
            AIClient(self.config).generate("hi")
        self.assertEqual(captured["timeout"], (min(15, 120), 120))

    def test_http_error(self):
        self.server.status = 401
        with self.assertRaises(AIError) as ctx:
            AIClient(self.config).generate("hi")
        self.assertIn("401", str(ctx.exception))
        self.assertIn("API Key", str(ctx.exception))
        self.assertEqual(len(self.server.requests), 1)

    def test_retry_on_server_error(self):
        self.server.status = 500
        with self.assertRaises(AIError):
            AIClient(self.config).generate("hi")
        self.assertEqual(len(self.server.requests), 2)

    def test_test_connection(self):
        result = AIClient(self.config).test_connection()
        self.assertEqual(result, "你好，这是 AI 回复。")
        payload = self.server.requests[0]["payload"]
        self.assertLessEqual(payload["max_tokens"], 64)

    def test_empty_answer(self):
        self.server.answer = ""
        with self.assertRaises(AIError):
            AIClient(self.config).generate("hi")


if __name__ == "__main__":
    unittest.main()
