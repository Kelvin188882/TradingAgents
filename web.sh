#!/usr/bin/env bash
# 启动本地报告入口：./web.sh，然后打开 http://127.0.0.1:8765
set -e
cd "$(dirname "$0")"
exec .venv/bin/python ta_web.py "$@"
