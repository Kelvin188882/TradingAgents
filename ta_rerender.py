#!/usr/bin/env python3
"""把已生成的报告重新渲染成新版式，不必重跑分析。

分析一次要十几分钟和真金白银的 API 费用，但换版式只是渲染层的事。
这个脚本从已有的报告目录里把各章节 markdown 读回来，重建 `final_state`，
再走一遍写报告树，产出新的 `complete_report.html`。

用法：
    .venv/bin/python ta_rerender.py data/logs/reports/600519.SS_20261101_163249
    .venv/bin/python ta_rerender.py --all          # 重渲 data/logs/reports 下全部
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import ta_run
from tradingagents import reporting as rp

# 章节文件 → 状态键。与 reporting._SECTIONS 的 (agent, filename, path) 一一对应。
_ANALYST_KEYS = {
    "market.md": "market",
    "sentiment.md": "social",
    "news.md": "news",
    "fundamentals.md": "fundamentals",
}


def _set_path(state: dict, path: tuple, value: str) -> None:
    node = state
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def _ticker_of(folder: Path) -> str:
    return re.sub(r"_\d{8}_\d{6}$", "", folder.name)


def _trade_date_of(folder: Path) -> str:
    """分析日期：先看报告头，再从行情报告正文里捞（重渲会重写报告头）。"""
    md = folder / "complete_report.md"
    if md.exists():
        m = re.search(r"^-\s*Analysis date:\s*(\d{4}-\d{2}-\d{2})", md.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    market = folder / "1_analysts" / "market.md"
    if market.exists():
        m = re.search(r"截至\s*(\d{4}-\d{2}-\d{2})", market.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    return ""


def _settings_for(state: dict) -> dict:
    """按当前配置重建运行参数，供报告的「运行参数」面板使用。"""
    from tradingagents.dataflows.config import get_config
    import tradingagents

    cfg = get_config()
    analysts = [
        _ANALYST_KEYS[filename]
        for _agent, filename, _path in rp._SECTIONS[0][2]
        if state.get(_path[0])
    ]
    return {
        "version": tradingagents.__version__,
        "llm_provider": cfg.get("llm_provider"),
        "deep_think_provider": cfg.get("deep_think_provider") or cfg.get("llm_provider"),
        "deep_think_llm": cfg.get("deep_think_llm"),
        "quick_think_provider": cfg.get("quick_think_provider") or cfg.get("llm_provider"),
        "quick_think_llm": cfg.get("quick_think_llm"),
        "analysts": analysts,
        "max_debate_rounds": cfg.get("max_debate_rounds"),
        "max_risk_discuss_rounds": cfg.get("max_risk_discuss_rounds"),
        "data_vendors": dict(cfg.get("data_vendors") or {}),
        "tool_vendors": dict(cfg.get("tool_vendors") or {}),
    }


def _patch_mkdir() -> None:
    """容错版的 `Path.mkdir`。

    本机 Python 带了一层文件系统代理（sitecustomize），对已存在的目录调用
    `mkdir(exist_ok=True)` 仍会抛 FileExistsError。重渲时目录都已经在了，
    必须兜住，否则一进 write_report_tree 就炸。
    """
    original = Path.mkdir

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        try:
            original(self, mode=mode, parents=parents, exist_ok=exist_ok)
        except FileExistsError:
            if not (exist_ok and self.is_dir()):
                raise

    Path.mkdir = mkdir


def rerender(folder: Path) -> Path | None:
    state: dict = {}
    for _heading, sub, agents in rp._SECTIONS:
        for _agent, filename, path in agents:
            candidate = folder / sub / filename
            if candidate.exists():
                _set_path(state, path, candidate.read_text(encoding="utf-8"))

    if not state.get("final_trade_decision"):
        print(f"  跳过 {folder.name}：没有组合经理决策文件", file=sys.stderr)
        return None

    state["trade_date"] = _trade_date_of(folder)
    parts = rp._report_parts(state)

    # write_report_tree 会连带重写 complete_report.md，而它的报告头记的是这次
    # 分析的真实参数（数据源、模型）。重渲用的配置不一定和当时一样，所以把
    # 原文件存下来再放回去，只留 HTML 是新版式。
    md_file = folder / "complete_report.md"
    original_md = md_file.read_bytes() if md_file.exists() else None

    settings = _settings_for(state)
    settings["_reconstructed"] = True
    provenance_file = folder / "data_provenance.json"
    if provenance_file.exists():
        settings["_data_provenance"] = json.loads(provenance_file.read_text(encoding="utf-8"))
    out = rp.write_report_tree(state, _ticker_of(folder), folder, settings=settings, html=True)
    if original_md is not None:
        md_file.write_bytes(original_md)
    return Path(out).with_suffix(".html")


def main() -> None:
    parser = argparse.ArgumentParser(description="把已生成的报告重新渲染成新版式")
    parser.add_argument("folders", nargs="*", help="报告目录，省略则配合 --all 使用")
    parser.add_argument("--all", action="store_true", help="重渲 reports 下全部目录")
    args = parser.parse_args()

    # 与正式运行一致地恢复本地数据源链，供「数据来源与时效」面板使用。
    # 这些补丁只改进程内配置，不重跑分析，也不发起数据请求。
    for env_name, apply in (
        ("TA_MACRO_VENDOR", ta_run._apply_macro_vendor),
        ("TA_MARKET_FALLBACK", ta_run._apply_market_fallback),
        ("TA_NEWS_VENDOR", ta_run._apply_news_vendor),
        ("TA_FUNDAMENTALS_VENDOR", ta_run._apply_fundamentals_vendor),
    ):
        default = "on" if env_name == "TA_MARKET_FALLBACK" else "akshare"
        if os.environ.get(env_name, default).strip().lower() != "off":
            apply()
    ta_run._apply_report_theme()
    _patch_mkdir()

    targets = [Path(f) for f in args.folders]
    if args.all:
        from tradingagents.dataflows.config import get_config

        root = Path(get_config()["results_dir"]) / "reports"
        targets.extend(sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else [])

    if not targets:
        parser.error("请给出报告目录，或用 --all")

    for folder in targets:
        if not folder.is_dir():
            print(f"  跳过 {folder}：不是目录", file=sys.stderr)
            continue
        html = rerender(folder)
        if html:
            print(f"  已重渲 {folder.name} → {html}  ({html.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
