#!/usr/bin/env python3
"""A tiny OpenAI-compatible mock AI server for trying the app without a model.

Run:
    python3 tools/mock_ai_server.py --port 8000

Then use http://127.0.0.1:8000 as the AI base URL in the app.
It is intentionally simple and produces template answers, not real intelligence.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mock-local-ai"


def _summarize(prompt: str) -> str:
    # The app's prompt is Chinese; pull out the requested parts if present.
    subject_match = re.search(r"主题：(.+)", prompt)
    body_match = re.search(r"邮件正文：\n(.+)", prompt, re.DOTALL)
    subject = subject_match.group(1).strip() if subject_match else ""
    body = body_match.group(1).strip() if body_match else prompt.strip()
    body = re.sub(r"\n\n要求：.*$", "", body, flags=re.DOTALL).strip()
    snippet = body[:240].strip()
    if len(body) > 240:
        snippet += "……"
    lines = ["你好！", "", "这是一条来自本地 Mock AI 的演示回复，说明邮件链路已经打通。"]
    if subject:
        lines.append(f"我看到的主题是：{subject}")
    if snippet:
        lines.extend(["", f"你邮件中的内容（节选）：{snippet}"])
    lines.extend(
        [
            "",
            "（这是 Mock AI 的固定格式回复。把 AI 接口地址换成你自己的模型服务后，"
            "这里就会是真实模型的输出。）",
        ]
    )
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    server_version = "MockAI/1.0"

    def log_message(self, fmt, *args):
        print(f"[mock-ai] {self.address_string()} {fmt % args}", flush=True)

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") in {"", "/health"}:
            self._send_json(200, {"status": "ok", "model": MODEL})
        elif self.path.rstrip("/") == "/v1/models":
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": MODEL, "object": "model", "created": int(time.time())}],
                },
            )
        elif self.path == "/":
            self._send_json(
                200,
                {
                    "message": "Mock AI server. POST /v1/chat/completions",
                    "model": MODEL,
                },
            )
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": f"unknown path: {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": {"message": f"invalid JSON: {exc}"}})
            return
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._send_json(400, {"error": {"message": "messages is required"}})
            return
        prompt = "\n".join(
            str(item.get("content", ""))
            for item in messages
            if isinstance(item, dict) and item.get("role") == "user"
        )
        if "连接测试" in prompt:
            answer = "连接成功"
        else:
            answer = _summarize(prompt)
        response = {
            "id": f"chatcmpl-mock-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": len(prompt),
                "completion_tokens": len(answer),
                "total_tokens": len(prompt) + len(answer),
            },
        }
        self._send_json(200, response)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local OpenAI-compatible mock AI server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"Mock AI listening on http://{args.host}:{args.port}", flush=True)
    print("Base URL for the app: http://%s:%s" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
