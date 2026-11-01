#!/usr/bin/env bash
# TradingAgents 便捷启动脚本（港股 / A 股配置）
#
# 用法：
#   ./run.sh                                  交互式启动（默认跑全四类分析师、分析当天）
#   ./run.sh --ticker 0700.HK --date 2026-10-30 --save --html --no-show
#
# 可覆盖的环境变量：
#   TA_ANALYSTS    默认 market,social,news,fundamentals
#   TA_ROUNDS      默认 1（辩论 / 风险讨论轮数）
#   TA_DATE        默认当天
#   TA_NEWS_PRESET cnhk（默认）| us
#   TA_MACRO_VENDOR        akshare（默认）| off
#   TA_MARKET_FALLBACK     on（默认）| off
#   TA_NEWS_VENDOR         akshare（默认）| off
#   TA_FUNDAMENTALS_VENDOR akshare（默认）| off
#   TA_SENTIMENT_VENDOR    akshare（默认）| off
#   TA_PORTFOLIO   持仓文件路径，默认自动用项目根目录的 portfolio.json（存在时）
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "未找到 .venv，先创建环境：python3.13 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

# 海外数据源（yfinance / SEC / 部分 LLM）需走本机代理
export HTTP_PROXY="${HTTP_PROXY:-http://127.0.0.1:15078}"
export HTTPS_PROXY="${HTTPS_PROXY:-http://127.0.0.1:15078}"

# 非交互运行必需的两个轮数配置，给默认值免得每次手输
export TRADINGAGENTS_MAX_DEBATE_ROUNDS="${TRADINGAGENTS_MAX_DEBATE_ROUNDS:-${TA_ROUNDS:-1}}"
export TRADINGAGENTS_MAX_RISK_ROUNDS="${TRADINGAGENTS_MAX_RISK_ROUNDS:-${TA_ROUNDS:-1}}"

has_arg() {
  local needle="$1"; shift
  for arg in "$@"; do
    [ "$arg" = "$needle" ] && return 0
  done
  return 1
}

# 默认分析师组合：用户显式传了 --analysts 就以用户的为准
ANALYSTS="${TA_ANALYSTS:-market,social,news,fundamentals}"
if ! has_arg --analysts "$@"; then
  set -- --analysts "$ANALYSTS" "$@"
fi

# 默认分析日期取当天。A 股/港股的基本面已改为按公告日期时点还原，
# 历史日期同样有数据；这里默认当天只是符合日常盯盘习惯。
if ! has_arg --date "$@"; then
  set -- --date "${TA_DATE:-$(date +%Y-%m-%d)}" "$@"
fi

# 有持仓文件就带上：交易员才知道是加仓、减仓还是开新仓
PORTFOLIO="${TA_PORTFOLIO:-portfolio.json}"
if [ -f "$PORTFOLIO" ] && ! has_arg --portfolio "$@"; then
  set -- --portfolio "$PORTFOLIO" "$@"
fi

exec .venv/bin/python ta_run.py "$@"
