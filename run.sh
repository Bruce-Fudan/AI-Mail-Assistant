#!/usr/bin/env bash
# 启动 AI 邮件助手。用法： ./run.sh [--data-dir PATH]
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3" >&2
  exit 1
fi

exec python3 main.py "$@"
