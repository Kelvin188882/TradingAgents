#!/usr/bin/env bash
# 启动报告入口：./web.sh；终端会显示电脑/手机地址和访问密码。
set -e
cd "$(dirname "$0")"
exec .venv/bin/python ta_web.py "$@"
