"""Small filesystem and formatting helpers used across the app."""

from __future__ import annotations

import json
import os
import platform
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_DIR_NAME = "ai-mail-assistant"


@dataclass(frozen=True)
class AppPaths:
    """Where settings, the encryption key, runtime state and logs live."""

    home: Path

    @property
    def settings(self) -> Path:
        return self.home / "settings.json"

    @property
    def secret_key(self) -> Path:
        return self.home / "secret.key"

    @property
    def state(self) -> Path:
        return self.home / "state.json"

    @property
    def log(self) -> Path:
        return self.home / "app.log"

    @classmethod
    def resolve(cls, home: str | os.PathLike[str] | None = None) -> "AppPaths":
        if home is None:
            env_home = os.environ.get("AI_MAIL_HOME")
            if env_home:
                home = env_home
        if home is None:
            system = platform.system()
            if system == "Windows":
                base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
                home = Path(base) / "AIMailAssistant"
            elif system == "Darwin":
                home = Path.home() / "Library" / "Application Support" / "AIMailAssistant"
            else:
                base = os.environ.get("XDG_CONFIG_HOME")
                home = (Path(base) if base else Path.home() / ".config") / APP_DIR_NAME
        return cls(Path(home).expanduser())

    def ensure(self) -> None:
        """Create the data directory with private permissions."""
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            try:
                os.chmod(self.home, 0o700)
            except OSError:
                pass


def secure_chmod(path: Path, mode: int = 0o600) -> None:
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write text atomically, never leaving a half-written settings file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        if os.name == "posix":
            try:
                os.fchmod(fd, mode)
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        secure_chmod(path, mode)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        mode=mode,
    )


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 的内容不是 JSON 对象")
    return value


def human_relative_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 5:
        return "刚刚"
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时前"
