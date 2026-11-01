"""A 股 / 港股个股与宏观新闻数据源（东方财富搜索接口）。

问题：上游新闻只接 yfinance 与 Alpha Vantage。Yahoo 不给非美股标的打新闻标签，
`0700.HK` / `600519.SS` 的 News Analyst 因此长期空转，报告里只能写"数据不可用"。

这里直接调东方财富的站内搜索接口（`search-api-web.eastmoney.com`）补上这块：
- 单只标的：按证券代码检索，一次可翻页取上百条；
- 宏观新闻：用中文关键词检索，比 Yahoo 英文索引对国内题材的召回好得多。

与 akshare `stock_news_em` 的差别：那个封装把 `pageSize` 写死成 10、
`pageIndex` 写死成 1，只能拿到最新 10 条，做 7 天窗口回看经常不够；这里可配页数。

时点安全：所有返回都按 `[start_date, end_date]` 过滤，避免回测读到未来新闻。
"""
import json
import logging
import re
import time
from urllib.parse import quote

import requests

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

# 同一次运行里 News Analyst 会对同一标的多次取数，缓存 10 分钟
_MEM_TTL_SECONDS = 600
_MEM: dict[str, tuple[float, list[dict]]] = {}

# 模型摘要可能省略新闻时分，这里独立保留实际送入模型的新闻中最晚发布时间。
# 只记录真实源字段，不为日线或无时刻的数据补 00:00。
_NEWS_LATEST_AT = None

_PAGE_SIZE = 50
_MAX_PAGES = 3

_TAG_RE = re.compile(r"</?em>")


def _strip_tags(text: str) -> str:
    """东财用 <em> 高亮命中词，去掉标签后才是正文。"""
    return _TAG_RE.sub("", str(text or "")).strip()


def _fetch_page(keyword: str, page: int) -> list[dict]:
    inner = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": page,
                "pageSize": _PAGE_SIZE,
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    params = {"cb": "jQuery1", "param": json.dumps(inner, ensure_ascii=False), "_": str(int(time.time()))}
    headers = {
        "user-agent": _UA,
        # HTTP 头只能是 latin-1，中文关键词必须百分号编码，否则 requests 直接抛编码错误
        "referer": f"https://so.eastmoney.com/news/s?keyword={quote(keyword)}",
        "accept": "*/*",
    }
    try:
        resp = requests.get(_SEARCH_URL, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
    except Exception as exc:
        raise VendorUnavailableError(f"东财新闻搜索不可达（{keyword}）: {exc}") from exc

    text = resp.text.strip()
    try:
        payload = json.loads(text[text.index("(") + 1 : text.rindex(")")])
        return payload["result"]["cmsArticleWebOld"] or []
    except Exception as exc:
        raise VendorUnavailableError(f"东财新闻搜索返回格式异常（{keyword}）: {exc}") from exc


def search_news(keyword: str, pages: int = _MAX_PAGES) -> list[dict]:
    """按关键词检索新闻，返回去重后的文章列表（含发布时间 datetime）。"""
    key = f"{keyword}:{pages}"
    cached = _MEM.get(key)
    if cached and time.time() - cached[0] < _MEM_TTL_SECONDS:
        return cached[1]

    from datetime import datetime

    articles: list[dict] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        batch = _fetch_page(keyword, page)
        if not batch:
            break
        for item in batch:
            title = _strip_tags(item.get("title"))
            if not title or title in seen:
                continue
            seen.add(title)
            published = None
            try:
                published = datetime.strptime(str(item.get("date"))[:19], "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                published = None
            articles.append(
                {
                    "title": title,
                    "summary": _strip_tags(item.get("content")),
                    "publisher": _strip_tags(item.get("mediaName")) or "东方财富",
                    "link": item.get("url") or "",
                    "pub_date": published,
                }
            )

    _MEM[key] = (time.time(), articles)
    return articles


def _format_articles(articles: list[dict], summary_limit: int = 400) -> str:
    out = ""
    for item in articles:
        when = f"（{item['pub_date']:%Y-%m-%d %H:%M}）" if item.get("pub_date") else ""
        out += f"### {item['title']} (source: {item['publisher']}){when}\n"
        summary = item.get("summary") or ""
        if summary:
            out += f"{summary[:summary_limit]}\n"
        if item.get("link"):
            out += f"Link: {item['link']}\n"
        out += "\n"
    return out


def _news_keyword(ticker: str) -> str | None:
    """把 600519.SS / 0700.HK 转成东财搜索用的纯代码；非 A 股港股返回 None。"""
    from ta_market_cn import parse_symbol

    parsed = parse_symbol(ticker)
    if parsed is None:
        return None
    return parsed[1]


def _record_news_time(articles: list[dict]) -> None:
    global _NEWS_LATEST_AT
    timestamps = [a["pub_date"] for a in articles if a.get("pub_date") is not None]
    if timestamps:
        latest = max(timestamps)
        if _NEWS_LATEST_AT is None or latest > _NEWS_LATEST_AT:
            _NEWS_LATEST_AT = latest


def get_provenance() -> dict:
    """供报告层读取的可序列化数据口径；没有分钟信息时返回空字典。"""
    return {"news_latest_at": _NEWS_LATEST_AT.strftime("%Y-%m-%d %H:%M")} if _NEWS_LATEST_AT else {}


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    """`get_news` 的东财实现，输出格式与 yfinance vendor 对齐。

    契约上必须返回字符串：本 vendor 通常排在链路末位，抛异常会让整条分析中止。
    只有"接口完全不可达"才抛 `VendorUnavailableError`（路由会转成安全哨兵）。
    """
    from datetime import datetime

    keyword = _news_keyword(ticker)
    if keyword is None:
        raise VendorUnavailableError(f"东财新闻仅覆盖 A 股 / 港股: {ticker!r}")

    limit = get_config().get("news_article_limit", 20)
    articles = search_news(keyword)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    in_range = [a for a in articles if in_window(a["pub_date"], start_dt, end_dt)]
    # 东财搜索不保证按时间倒序，自己排一遍
    in_range.sort(key=lambda a: a["pub_date"] or datetime.min, reverse=True)

    if in_range:
        selected = in_range[:limit]
        _record_news_time(selected)
        return (
            f"## {ticker} News, from {start_date} to {end_date}（数据源：东方财富）:\n\n"
            f"{_format_articles(selected)}"
        )

    gap = coverage_gap(
        (a["pub_date"] for a in articles),
        start_date,
        end_date,
        "东方财富个股新闻",
        f"news about {ticker}",
    )
    return gap or f"No news found for {ticker} between {start_date} and {end_date}"


# 宏观 / 大盘新闻的检索词。中文命中率远高于 Yahoo 的英文索引。
GLOBAL_KEYWORDS = [
    "恒生指数",
    "港股 南向资金",
    "A股 上证指数",
    "央行 公开市场操作 LPR",
    "中国 CPI PPI 经济数据",
]


def get_global_news(as_of_date: str, look_back_days: int | None = None, limit: int | None = None) -> str:
    """`get_global_news` 的东财实现：中文宏观 / 大盘新闻。"""
    from datetime import datetime, timedelta

    config = get_config()
    if look_back_days is None:
        look_back_days = config.get("global_news_lookback_days", 7)
    if limit is None:
        limit = config.get("global_news_article_limit", 10)

    curr_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    collected: list[dict] = []
    seen: set[str] = set()
    failures = 0
    for keyword in GLOBAL_KEYWORDS:
        try:
            batch = search_news(keyword, pages=1)
        except VendorUnavailableError as exc:
            failures += 1
            logger.warning("东财宏观新闻检索失败（%s）: %s", keyword, exc)
            continue
        for item in batch:
            if not in_window(item["pub_date"], start_dt, curr_dt):
                continue
            if item["title"] in seen:
                continue
            seen.add(item["title"])
            collected.append(item)

    if failures == len(GLOBAL_KEYWORDS):
        raise VendorUnavailableError("东财宏观新闻全部检索失败")

    collected.sort(key=lambda a: a["pub_date"] or datetime.min, reverse=True)
    if not collected:
        # 本 vendor 排在宏观新闻链路前面，空结果交给 yfinance 兜底
        raise VendorUnavailableError(
            f"东财在 {start_date}~{as_of_date} 窗口内没有检索到宏观新闻"
        )

    selected = collected[:limit]
    _record_news_time(selected)
    return (
        f"## Global Market News, from {start_date} to {as_of_date}（数据源：东方财富）:\n\n"
        f"{_format_articles(selected)}"
    )
