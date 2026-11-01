#!/usr/bin/env python3
"""TradingAgents 启动封装。

作用：在 CLI 启动前，把「宏观新闻查询词」替换成港股 / A 股视角，
不改上游源码（`tradingagents/default_config.py` 保持原样，`git pull` 不会冲突）。

用法与本机 `tradingagents` 命令完全一致：
    .venv/bin/python ta_run.py --ticker 0700.HK --date 2026-10-30 \
        --analysts market,news --save --html --no-show

可用环境变量：
    TA_NEWS_PRESET = cnhk（默认，港股/A股） | us（上游原版美股视角）
"""
import os
import sys

# ---- 必须在导入 cli 之前完成注入 ----
import tradingagents.default_config as default_config

# 港股 / A 股视角的宏观新闻查询词。
# 用英文表述是为了命中 Yahoo 新闻索引（中文关键词在 Yahoo 上召回很差）；
# 保留一条美联储查询，因为港元与美元挂钩，美联储政策对港股是硬约束。
CNHK_QUERIES = [
    "Federal Reserve interest rates inflation",
    "Hang Seng Index Hong Kong stocks southbound connect flows",
    "PBOC RRR LPR China monetary policy stimulus",
    "China CPI PPI credit growth M2 economic data",
    "China A-shares CSI 300 Shanghai Composite market turnover",
    "US China tariffs export controls semiconductor sanctions",
    "oil commodities supply chain energy",
]

US_QUERIES = list(default_config.DEFAULT_CONFIG["global_news_queries"])

# 宏观指标工具的说明补丁：告诉分析师 akshare 侧有哪些中国/香港指标可用
MACRO_TOOL_DESC = """获取中国 / 香港宏观经济指标时间序列，用于给宏观判断提供实际数据支撑。

参数：
    indicator: 指标名。支持：
        china_cpi（中国 CPI 年率）、china_ppi（PPI 年率）、china_m2（M2 年率）、
        china_pmi（制造业 PMI）、china_gdp（GDP 年率）、china_fx_reserves（外汇储备）、
        lpr_1y / lpr_5y（1 年期 / 5 年期以上 LPR）、shibor_on（SHIBOR 隔夜）、
        hk_unemployment（中国香港失业率）。
        也接受别名：cpi / ppi / m2 / gdp / real_gdp / pmi / lpr / policy_rate / unemployment / shibor。
        美国与欧元区指标（fed_funds_rate、10y_treasury、core_pce、euro_hicp 等）由 FRED 提供，需配 FRED_API_KEY。
    curr_date: 当前日期 yyyy-mm-dd，作为窗口终点
    look_back_days: 回看窗口天数，省略则默认 3 年

返回序列标题、单位、频率、最新值、窗口内变化和近期观测表。"""


def _apply_queries(queries: list[str]) -> None:
    """同时写入默认配置与已初始化的运行期配置。

    `tradingagents.default_config.DEFAULT_CONFIG` 是图构建时读取的源头，
    而 `tradingagents.dataflows.config._config` 是导入时就 copy 好的一份快照，
    两处都要改，否则数据工具读到的还是旧值。
    """
    default_config.DEFAULT_CONFIG["global_news_queries"] = list(queries)

    from tradingagents.dataflows import config as df_config

    if df_config._config is not None:
        df_config._config["global_news_queries"] = list(queries)


def _apply_macro_vendor() -> None:
    """把 akshare 宏观数据源注册进路由，并置于 FRED 之前。

    上游把 vendor 表写成静态 dict，这里直接往 `VENDOR_METHODS` 里插一项，
    不需要改源码。FRED 保留在后面做回落（未配 Key 时会自动跳过）。
    """
    from tradingagents.dataflows import config as df_config
    from tradingagents.dataflows import router as df_router

    import ta_macro_cn

    df_router.VENDOR_METHODS["get_macro_indicators"]["akshare_cn"] = ta_macro_cn.get_macro_data

    chain = "akshare_cn,fred"
    default_config.DEFAULT_CONFIG.setdefault("data_vendors", {})["macro_data"] = chain
    default_config.DEFAULT_CONFIG.setdefault("tool_vendors", {}).pop("get_macro_indicators", None)
    if df_config._config is not None:
        df_config._config.setdefault("data_vendors", {})["macro_data"] = chain
        df_config._config.setdefault("tool_vendors", {}).pop("get_macro_indicators", None)

    # 让分析师知道有哪些中国指标可问
    try:
        from tradingagents.agents import tools as ta_tools

        ta_tools.get_macro_indicators.description = MACRO_TOOL_DESC
    except Exception:  # 描述补丁失败不影响主流程
        pass


def _apply_market_fallback() -> None:
    """把 akshare 行情接成 yfinance 的兜底。

    Yahoo 对同一 IP 限流很敏感，被限时整条分析链（行情 / 指标 / 快照）都会失败。
    这里做两处补丁：
    1. `get_stock_data` 工具：注册 vendor `akshare_cn`，链路 `yfinance,akshare_cn`；
    2. `load_ohlcv`：指标与快照的取数入口，包一层失败回退。
       上游 `market.py` / `snapshot.py` 是 `from ... import load_ohlcv` 绑定的，
       所以三个模块的同名引用都要替换。
    """
    from tradingagents.dataflows import config as df_config
    from tradingagents.dataflows import router as df_router
    from tradingagents.dataflows.vendors.yahoo import ohlcv as y_ohlcv

    import ta_market_cn

    df_router.VENDOR_METHODS["get_stock_data"]["akshare_cn"] = ta_market_cn.get_stock_data

    chain = "yfinance,akshare_cn"
    for cfg in (default_config.DEFAULT_CONFIG, df_config._config):
        if cfg is None:
            continue
        cfg.setdefault("data_vendors", {})["core_stock_apis"] = chain
        cfg.setdefault("tool_vendors", {}).pop("get_stock_data", None)

    fallback = ta_market_cn.make_load_ohlcv_fallback(y_ohlcv.load_ohlcv, y_ohlcv)
    y_ohlcv.load_ohlcv = fallback
    try:
        from tradingagents.dataflows.vendors.yahoo import market as y_market
        from tradingagents.dataflows.vendors.yahoo import snapshot as y_snapshot

        y_market.load_ohlcv = fallback
        y_snapshot.load_ohlcv = fallback
    except Exception:  # 模块改名等情况下不阻断主流程
        pass


def _apply_news_vendor() -> None:
    """把东财新闻接进链路，解决港股 / A 股新闻为空。

    Yahoo 不给非美股标的打新闻标签，`0700.HK` / `600519.SS` 的个股新闻长期为空。
    这里分两个工具单独配：
    - `get_news`：`yfinance,akshare_cn` —— Yahoo 没有标签新闻时自动落到东财；
    - `get_global_news`：`akshare_cn,yfinance` —— 中文检索对国内题材召回更好，
      东财查不到时再由 Yahoo 兜底。
    `get_insider_transactions` 不在此列（东财没有对应的高管持股变动接口）。
    """
    from tradingagents.dataflows import config as df_config
    from tradingagents.dataflows import router as df_router

    import ta_news_cn

    df_router.VENDOR_METHODS["get_news"]["akshare_cn"] = ta_news_cn.get_news
    df_router.VENDOR_METHODS["get_global_news"]["akshare_cn"] = ta_news_cn.get_global_news

    chains = {"get_news": "yfinance,akshare_cn", "get_global_news": "akshare_cn,yfinance"}
    for cfg in (default_config.DEFAULT_CONFIG, df_config._config):
        if cfg is None:
            continue
        tool_vendors = cfg.setdefault("tool_vendors", {})
        tool_vendors.update(chains)


def _apply_fundamentals_vendor() -> None:
    """把东财财报接成 A 股 / 港股的基本面数据源。

    上游 `withhold_live_profile` 会在分析历史日期时屏蔽基本面（yfinance 的
    `Ticker.info` 是当前快照，没有历史版本，防未来信息泄露）。东财财报每条都带
    公告日期，可以做到真正的时点还原，所以放在链路最前：
    非 A 股 / 港股的符号会抛 `VendorUnavailableError` 自动落到 yfinance。
    """
    from tradingagents.dataflows import config as df_config
    from tradingagents.dataflows import router as df_router

    import ta_fundamentals_cn as ta_fin

    methods = ("get_fundamentals", "get_income_statement", "get_balance_sheet", "get_cashflow")
    for method in methods:
        df_router.VENDOR_METHODS[method]["akshare_cn"] = getattr(ta_fin, method)

    chain = "akshare_cn,yfinance"
    for cfg in (default_config.DEFAULT_CONFIG, df_config._config):
        if cfg is None:
            continue
        tool_vendors = cfg.setdefault("tool_vendors", {})
        for method in methods:
            tool_vendors[method] = chain


def _apply_sentiment_vendor() -> None:
    """把情绪分析师的 StockTwits / Reddit 预取替换成国内数据源。

    Sentiment Analyst 不调工具，而是在节点里直接 `fetch_stocktwits_messages` /
    `fetch_reddit_posts` 预取后塞进 prompt。这两个源是美股散户社区，对 A 股 / 港股
    没有讨论量，分析师只能写"数据不可用"。这里替换模块级函数名：
    A 股走东财千股千评，港股走港股通持股，美股标的仍走原实现。
    """
    from tradingagents.agents.analysts import sentiment_analyst as sa

    import ta_sentiment_cn as ta_sent

    if getattr(sa, "_ta_sentiment_patched", False):
        return

    original_twits = sa.fetch_stocktwits_messages
    original_reddit = sa.fetch_reddit_posts

    def fetch_stocktwits_messages(ticker, limit=30, start_date=None, end_date=None, screen=None):
        if ta_sent.is_cn_hk(ticker):
            return ta_sent.get_social_block_for_analyst(ticker, limit, start_date, end_date, screen)
        return original_twits(
            ticker, limit=limit, start_date=start_date, end_date=end_date, screen=screen
        )

    def fetch_reddit_posts(ticker, subreddits=(), start_date=None, end_date=None, screen=None):
        if ta_sent.is_cn_hk(ticker):
            return ta_sent.get_community_block_for_analyst(
                ticker, subreddits, start_date, end_date, screen
            )
        return original_reddit(
            ticker, subreddits, start_date=start_date, end_date=end_date, screen=screen
        )

    sa.fetch_stocktwits_messages = fetch_stocktwits_messages
    sa.fetch_reddit_posts = fetch_reddit_posts
    sa._ta_sentiment_patched = True


def main() -> None:
    preset = os.environ.get("TA_NEWS_PRESET", "cnhk").strip().lower()
    if preset == "us":
        _apply_queries(US_QUERIES)
    else:
        _apply_queries(CNHK_QUERIES)

    if os.environ.get("TA_MACRO_VENDOR", "akshare").strip().lower() != "off":
        _apply_macro_vendor()

    if os.environ.get("TA_MARKET_FALLBACK", "on").strip().lower() != "off":
        _apply_market_fallback()

    if os.environ.get("TA_NEWS_VENDOR", "akshare").strip().lower() != "off":
        _apply_news_vendor()

    if os.environ.get("TA_FUNDAMENTALS_VENDOR", "akshare").strip().lower() != "off":
        _apply_fundamentals_vendor()

    if os.environ.get("TA_SENTIMENT_VENDOR", "akshare").strip().lower() != "off":
        _apply_sentiment_vendor()

    from cli.main import app

    app()


if __name__ == "__main__":
    main()
