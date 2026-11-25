"""Command line entry point for `python -m aimail` and `main.py`."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="AI 邮件助手 — 本地 AI 邮件自动回复")
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help="覆盖数据目录（设置、密钥、日志），默认使用 ~/.config/ai-mail-assistant",
    )
    parser.add_argument("--version", action="store_true", help="显示版本并退出")
    args = parser.parse_args(argv)

    from aimail import __version__

    if args.version:
        print(f"AI 邮件助手 {__version__}")
        return 0

    try:
        from aimail.ui.app import run
    except ModuleNotFoundError as exc:
        missing = exc.name or "依赖"
        print(
            f"缺少 Python 依赖：{missing}\n\n"
            "本项目需要：\n"
            "  • Python 3.10+\n"
            "  • PyGObject / GTK 4 / libadwaita（系统包）\n"
            "  • cryptography, requests（pip）\n\n"
            "Ubuntu/Debian 安装系统依赖：\n"
            "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1\n"
            "然后运行：\n"
            "  python3 -m pip install -r requirements.txt\n",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # e.g. missing GTK typelib, no display
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    # Gtk.Application parses its own command line; our options have been consumed.
    return run([sys.argv[0]], home=args.data_dir)
