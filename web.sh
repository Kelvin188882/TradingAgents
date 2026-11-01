#!/usr/bin/env bash
# 启动免密码报告入口：./web.sh；终端会显示电脑/手机地址。
set -e
cd "$(dirname "$0")"
exec .venv/bin/python ta_web.py "$@"
