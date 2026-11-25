"""OpenAI-compatible chat-completions client for a local AI service."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

from .config import AIConfig

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


class AIError(Exception):
    """A user-presentable AI request failure."""


@dataclass
class AIResult:
    text: str
    model: str = ""
    usage: dict | None = None


def normalize_base_url(value: str) -> str:
    """Paste-friendly URL normalization: 127.0.0.1:8000 -> http://127.0.0.1:8000."""
    value = (value or "").strip()
    if not value:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        value = "http://" + value
    return value.rstrip("/")


def build_endpoint(base_url: str, endpoint_path: str = "/v1/chat/completions") -> str:
    """Join base URL and path without duplicating /v1 segments."""
    base = normalize_base_url(base_url)
    path = (endpoint_path or "").strip()
    if not base:
        return path
    if re.match(r"^https?://", path, re.IGNORECASE):
        return path
    if not path:
        path = "/v1/chat/completions"
    path = "/" + path.lstrip("/")
    if base.endswith(path):
        return base
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path


def _extract_message_content(choice: dict) -> str:
    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    text = choice.get("text")
    if isinstance(text, str):
        return text
    return ""


def _short(text: str, limit: int = 320) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


class AIClient:
    def __init__(self, config: AIConfig):
        self.config = config

    @property
    def endpoint(self) -> str:
        return build_endpoint(self.config.base_url, self.config.endpoint_path)

    def test_connection(self) -> str:
        """Send a tiny prompt and return the model's answer (for the UI test button)."""
        result = self.generate(
            "这是一次连接测试。请只回复四个字：连接成功。",
            max_tokens=min(64, max(16, self.config.max_tokens)),
        )
        return result.text.strip() or "连接成功"

    def generate(self, user_prompt: str, max_tokens: int | None = None) -> AIResult:
        if not (user_prompt or "").strip():
            raise AIError("AI 输入为空")
        endpoint = self.endpoint
        if not re.match(r"^https?://", endpoint, re.IGNORECASE):
            raise AIError("AI 接口地址无效，请填写形如 http://127.0.0.1:8000 的地址")

        messages: list[dict[str, str]] = []
        # The user's system prompt is sent exactly as entered.  The app does not
        # prepend, append, trim, or substitute any prompt of its own.
        if self.config.system_prompt != "":
            messages.append({"role": "system", "content": self.config.system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload: dict = {
            "messages": messages,
            "stream": False,
        }
        if not self.config.use_backend_defaults:
            # Let the backend choose temperature / thinking strength / top_p /
            # top_k when the user enables "使用后端 AI 默认选项".  The app never
            # sends top_p, top_k or thinking parameters.
            payload["temperature"] = float(self.config.temperature)
        if (self.config.model or "").strip():
            payload["model"] = self.config.model.strip()
        tokens = self.config.max_tokens if max_tokens is None else max_tokens
        if not self.config.ignore_output_limit and tokens and tokens > 0:
            payload["max_tokens"] = int(tokens)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AI-Mail-Assistant/1.0",
        }
        if (self.config.api_key or "").strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"

        session = requests.Session()
        host = (urlsplit(endpoint).hostname or "").lower()
        if host in LOOPBACK_HOSTS:
            # Never send local AI requests through a system HTTP proxy.
            session.trust_env = False

        timeout = (
            None
            if self.config.ignore_timeout
            else (min(15, self.config.timeout_seconds), self.config.timeout_seconds)
        )
        try:
            return self._request_with_retries(session, endpoint, payload, headers, timeout)
        finally:
            session.close()

    def _request_with_retries(
        self,
        session: requests.Session,
        endpoint: str,
        payload: dict,
        headers: dict[str, str],
        timeout: tuple[int, int] | None,
    ) -> AIResult:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = session.post(endpoint, json=payload, headers=headers, timeout=timeout)
            except requests.exceptions.Timeout as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise AIError(
                    f"AI 接口请求超时（{self.config.timeout_seconds} 秒）。"
                    "如果模型较慢，可在设置里增大超时时间。"
                ) from exc
            except requests.exceptions.RequestException as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise AIError(
                    "无法连接 AI 接口："
                    f"{exc}。请确认本地 AI 服务已启动，并检查地址与端口。"
                ) from exc

            if response.status_code >= 500 and attempt == 0:
                last_error = AIError(f"AI 服务返回 HTTP {response.status_code}")
                time.sleep(1.0)
                continue

            if response.status_code >= 400:
                detail = self._error_detail(response)
                hint = ""
                if response.status_code in (401, 403):
                    hint = "请检查 API Key 是否正确。"
                elif response.status_code == 404:
                    hint = (
                        "请检查接口路径是否为 /v1/chat/completions，"
                        "或去掉基础地址中重复的 /v1。"
                    )
                raise AIError(
                    f"AI 接口返回 HTTP {response.status_code}：{detail} {hint}".strip()
                )

            try:
                data = response.json()
            except ValueError as exc:
                body = _short(response.text)
                raise AIError(f"AI 接口返回的内容不是 JSON：{body or '（空响应）'}") from exc

            if not isinstance(data, dict):
                raise AIError("AI 接口返回了无法识别的 JSON 结构")
            if data.get("error"):
                err = data["error"]
                if isinstance(err, dict):
                    err = err.get("message") or json.dumps(err, ensure_ascii=False)
                raise AIError(f"AI 接口返回错误：{_short(str(err))}")

            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                # Be forgiving with small self-written services that return a
                # top-level text field instead of the OpenAI envelope.
                for key in ("response", "output", "text", "answer"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return AIResult(
                            text=value.strip(),
                            model=str(data.get("model") or self.config.model or ""),
                        )
                raise AIError("AI 接口响应中没有 choices 字段，无法获取输出")
            content = _extract_message_content(choices[0]).strip()
            if not content:
                raise AIError("AI 返回了空内容")
            model = str(data.get("model") or self.config.model or "")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            return AIResult(text=content, model=model, usage=usage)

        raise AIError(f"AI 接口请求失败：{last_error}")

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    return _short(str(err.get("message") or err))
                if err:
                    return _short(str(err))
                if data.get("message"):
                    return _short(str(data["message"]))
        except ValueError:
            pass
        return _short(response.text)
