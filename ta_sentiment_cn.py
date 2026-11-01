"""A 股 / 港股情绪数据源（东方财富），替代对 A 股 / 港股无效的美股社区。

问题：上游 Sentiment Analyst 预取三个源——Yahoo 新闻、StockTwits、Reddit。
后两个是美股散户社区，对 `600519.SS` / `0700.HK` 完全没有讨论量，分析师只能写
"数据不可用"，白白消耗上下文。

这里按市场替换：
- A 股：东方财富「千股千评」—— 综合得分、关注指数、机构参与度、人气排名，
  是 A 股散户 + 机构关注度的直接刻画。
- 港股：「港股通持股」日频序列 —— 南向资金的持仓变动，是港股最实在的情绪/资金流。
  港股没有可比的散户社区数据。

时点安全：千股千评只有最新快照（带「交易日」字段），分析日早于该快照时不使用；
港股通持股按「持股日期」裁到分析日，可以回溯。
"""
import logging

import pandas as pd

logger = logging.getLogger(__name__)

_MEM_TTL_SECONDS = 600
_MEM: dict[str, tuple[float, object]] = {}


def _cache_get(key: str):
    import time

    hit = _MEM.get(key)
    if hit and time.time() - hit[0] < _MEM_TTL_SECONDS:
        return hit[1]
    return None


def _cache_put(key: str, value) -> None:
    import time

    _MEM[key] = (time.time(), value)


def _numeric_code(code: str) -> str:
    return "".join(ch for ch in code if ch.isdigit()).zfill(5 if len(code) <= 5 else 6)


def eastmoney_comment(code: str) -> dict | None:
    """千股千评里该股的一行（全市场表，按代码定位）。"""
    import akshare as ak

    table = _cache_get("comment")
    if table is None:
        try:
            table = ak.stock_comment_em()
        except Exception as exc:
            logger.warning("东财千股千评拉取失败: %s", exc)
            return None
        _cache_put("comment", table)
    if table is None or table.empty:
        return None
    hit = table[table["代码"].astype(str).str.zfill(6) == code.zfill(6)]
    return None if hit.empty else hit.iloc[0].to_dict()


def southbound_series(code: str) -> pd.DataFrame | None:
    """港股通（南向资金）对该股的持股日频序列。"""
    import akshare as ak

    key = f"hsgt:{code}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        data = ak.stock_hsgt_individual_em(symbol=code)
    except Exception as exc:
        logger.warning("港股通持股拉取失败（%s）: %s", code, exc)
        return None
    if data is None or data.empty:
        return None
    data = data.copy()
    data["持股日期"] = pd.to_datetime(data["持股日期"], errors="coerce")
    data = data.dropna(subset=["持股日期"]).sort_values("持股日期")
    _cache_put(key, data)
    return data


def _is_a_share(ticker: str) -> str | None:
    text = (ticker or "").upper()
    return text[:-3] if text.endswith((".SS", ".SZ", ".BJ")) else None


def _is_hk(ticker: str) -> str | None:
    text = (ticker or "").upper()
    return text[:-3].zfill(5) if text.endswith(".HK") else None


def is_cn_hk(ticker: str) -> bool:
    """该标的是否属于 A 股 / 港股（决定要不要替换美股社区数据源）。"""
    return _is_a_share(ticker) is not None or _is_hk(ticker) is not None


def _pct(value, digits: int = 2) -> str:
    try:
        return f"{float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def get_social_block(ticker: str, start_date: str, end_date: str) -> str:
    """返回替代 StockTwits / Reddit 的情绪数据块（已是给模型读的纯文本）。"""
    end_dt = pd.to_datetime(end_date).normalize()

    a_code = _is_a_share(ticker)
    if a_code:
        row = eastmoney_comment(a_code)
        if row is None:
            return (
                f"## 社交情绪 · 东方财富千股千评\n\n"
                f"<{ticker} 的千股千评数据暂不可用；A 股没有对应的美股式散户社区数据源>"
            )
        snapshot_day = pd.to_datetime(row.get("交易日"), errors="coerce")
        if pd.notna(snapshot_day) and snapshot_day > end_dt:
            return (
                f"## 社交情绪 · 东方财富千股千评\n\n"
                f"<千股千评只有最新快照（{snapshot_day:%Y-%m-%d}），无法还原 {end_date} 的"
                f"市场情绪，为避免未来信息已跳过>"
            )
        lines = [
            "## 社交情绪 · 东方财富千股千评（替代 StockTwits / Reddit）",
            "",
            f"快照日期：{snapshot_day:%Y-%m-%d}" if pd.notna(snapshot_day) else "快照日期：—",
            f"- 综合得分：{_num(row.get('综合得分'))}",
            f"- 关注指数：{_num(row.get('关注指数'), 1)}",
            f"- 机构参与度：{_num(row.get('机构参与度'), 3)}",
            f"- 人气排名：{row.get('目前排名', '—')}（较上期 {'上升' if row.get('上升') else '下降'}）",
            f"- 主力成本：{_num(row.get('主力成本'))}",
            f"- 换手率：{_pct(row.get('换手率'))}　涨跌幅：{_pct(row.get('涨跌幅'))}",
            "",
            "说明：千股千评是东方财富对 A 股的全市场关注度/资金参与度打分，"
            "关注指数越高说明散户关注度越高，机构参与度反映机构资金介入程度。",
        ]
        return "\n".join(lines)

    hk_code = _is_hk(ticker)
    if hk_code:
        data = southbound_series(hk_code)
        if data is None or data.empty:
            return (
                f"## 社交情绪 · 港股通持股\n\n<{ticker} 的港股通持股数据不可用>"
            )
        window = data[data["持股日期"] <= end_dt]
        if window.empty:
            return (
                f"## 社交情绪 · 港股通持股\n\n"
                f"<港股通持股序列早于 {end_date} 没有记录>"
            )
        recent = window.tail(10)
        lines = [
            "## 社交情绪 · 港股通（南向资金）持股（替代 StockTwits / Reddit）",
            "",
            "港股没有可比的散户社区数据，这里用南向资金的持仓变动刻画资金情绪。",
            "",
            "| 持股日期 | 收盘价 | 涨跌幅 | 持股数量(股) | 持股市值(港元) | 市值变化1日 | 市值变化5日 |",
            "|---|---|---|---|---|---|---|",
        ]
        for _, r in recent.iterrows():
            lines.append(
                f"| {r['持股日期']:%Y-%m-%d} | {r.get('当日收盘价', '—')} | "
                f"{_pct(r.get('当日涨跌幅'))} | {r.get('持股数量', 0):,.0f} | "
                f"{r.get('持股市值', 0):,.0f} | {r.get('持股市值变化-1日', 0):,.0f} | "
                f"{r.get('持股市值变化-5日', 0):,.0f} |"
            )
        return "\n".join(lines)

    return ""


def get_social_block_for_analyst(ticker, limit=30, start_date=None, end_date=None, screen=None) -> str:
    """`fetch_stocktwits_messages` 的替代实现，签名保持兼容。"""
    block = get_social_block(ticker, start_date or "", end_date or "")
    return block or f"<{ticker} 不在 A 股 / 港股情绪数据覆盖范围内>"


def get_community_block_for_analyst(ticker, subreddits=(), start_date=None, end_date=None, screen=None) -> str:
    """`fetch_reddit_posts` 的替代实现：A 股 / 港股没有对应的社区讨论源。"""
    if not (_is_a_share(ticker) or _is_hk(ticker)):
        return ""
    return (
        "## 社区讨论\n\n"
        f"<Reddit 是美股社区，对 {ticker} 没有讨论量。A 股散户讨论集中在东方财富股吧、"
        "雪球，港股集中在雪球与 AASTOCKS，这些源没有可靠的接口可取；"
        "情绪判断请以上一节的国内数据为准，不要假设存在社区热度。>"
    )
