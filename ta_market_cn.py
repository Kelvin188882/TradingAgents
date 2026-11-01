"""A 股 / 港股行情数据源（akshare，双后端级联）。

问题一：上游行情与指标全部来自 yfinance，Yahoo 对同一 IP 限流极敏感，连续跑两三次
就会整条链路瘫痪。

问题二（实测才发现）：只补一家兜底源不够。东方财富 push2 同样按 IP 限流，压测几轮
之后 yfinance 和东财会同时不可用。所以这里接两家互相独立的源，按顺序级联：

    东方财富 push2（akshare stock_zh_a_hist / stock_hk_hist）
       ↓ 失败
    新浪财经（akshare stock_zh_a_daily / stock_hk_daily）

接在两个位置：
1. `get_stock_data` 工具 —— 注册为 vendor `akshare_cn`，链路 `yfinance,akshare_cn`；
2. `load_ohlcv` —— 技术指标与快照都靠它取数，这里包一层：yfinance 抛错时改用
   上面两家取数，再复用上游的清洗、时点过滤、缺口填充、陈旧校验逻辑。

代价与差异：两家都用前复权（qfq），yfinance 用 auto_adjust，在复权基准日附近
可能有个位数差异；成交量单位都是股。
"""
import logging
import time

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

logger = logging.getLogger(__name__)

# akshare 一次拉全量比较慢，同一次运行里指标工具会反复问同一个标的，缓存 10 分钟
_MEM_TTL_SECONDS = 600
_MEM: dict[str, tuple[float, pd.DataFrame]] = {}

_EM_COLUMN_MAP = {
    "日期": "Date",
    "开盘": "Open",
    "收盘": "Close",
    "最高": "High",
    "最低": "Low",
    "成交量": "Volume",
    "成交额": "Amount",
}

# 新浪日线用小写英文列名
_SINA_COLUMN_MAP = {
    "date": "Date",
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
    "amount": "Amount",
    "outstanding_share": "OutstandingShare",
}

# 5 年，与上游 load_ohlcv 的默认回看窗口一致（指标要算 200 日均线）
_LOOKBACK_YEARS = 5


def parse_symbol(symbol: str) -> tuple[str, str] | None:
    """把 600519.SS / 0700.HK 这类符号拆成 (市场, akshare 代码)。不支持的返回 None。"""
    text = (symbol or "").strip().upper()
    if text.endswith((".SS", ".SZ", ".BJ")):
        return "zh_a", text[:-3]
    if text.endswith(".HK"):
        # akshare 港股代码是 5 位，0700.HK -> 00700
        return "hk", text[:-3].zfill(5)
    return None


def _fetch_eastmoney(market: str, code: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak

    if market == "hk":
        raw = ak.stock_hk_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq")
    else:
        raw = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq")
    return raw.rename(columns=_EM_COLUMN_MAP) if raw is not None else pd.DataFrame()


def _fetch_sina(market: str, code: str, start: str, end: str) -> pd.DataFrame:
    """新浪财经。港股接口不支持日期区间，只能拉全量后自行截取。"""
    import akshare as ak

    if market == "hk":
        raw = ak.stock_hk_daily(symbol=code, adjust="qfq")
    else:
        # 新浪用 sh/sz/bj 前缀；akshare 的港股接口不带前缀，A 股要自己按号段判断
        prefix = "sh" if code.startswith(("6", "9")) else ("bj" if code.startswith(("4", "8")) else "sz")
        raw = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", start_date=start, end_date=end, adjust="qfq")
    return raw.rename(columns=_SINA_COLUMN_MAP) if raw is not None else pd.DataFrame()


def fetch_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """取日线 OHLCV，返回列 Date/Open/High/Low/Close/Volume，按日期升序。

    先东方财富、再新浪：两家域名与限流策略都不同，同时挂掉的概率低得多。
    """
    parsed = parse_symbol(symbol)
    if parsed is None:
        raise VendorUnavailableError(f"akshare 行情不支持该符号格式: {symbol!r}（仅 .SS/.SZ/.BJ/.HK）")

    market, code = parsed
    key = f"{market}:{code}:{start_date}:{end_date}"
    cached = _MEM.get(key)
    if cached and time.time() - cached[0] < _MEM_TTL_SECONDS:
        return cached[1].copy()

    start = start_date.replace("-", "")
    end = end_date.replace("-", "")

    errors: list[str] = []
    data = pd.DataFrame()
    for name, fetcher in (("东方财富", _fetch_eastmoney), ("新浪财经", _fetch_sina)):
        try:
            raw = fetcher(market, code, start, end)
        except Exception as exc:  # 限流 / 网络 / 接口改版
            errors.append(f"{name}: {exc}")
            continue
        if raw is None or raw.empty:
            errors.append(f"{name}: 返回空数据")
            continue
        raw = raw.rename(columns=lambda c: _SINA_COLUMN_MAP.get(c, c))
        keep = [c for c in ("Date", "Open", "High", "Low", "Close", "Volume", "OutstandingShare") if c in raw.columns]
        missing = {"Date", "Open", "High", "Low", "Close"} - set(keep)
        if missing:
            errors.append(f"{name}: 缺少列 {missing}")
            continue
        candidate = raw[keep].copy()
        candidate["Date"] = pd.to_datetime(candidate["Date"], errors="coerce")
        for col in ("Open", "High", "Low", "Close", "Volume", "OutstandingShare"):
            if col in candidate.columns:
                candidate[col] = pd.to_numeric(candidate[col], errors="coerce")
        candidate = candidate.dropna(subset=["Date", "Close"]).sort_values("Date").reset_index(drop=True)
        if candidate.empty:
            errors.append(f"{name}: 清洗后无有效行")
            continue
        data = candidate
        break

    if data.empty:
        raise VendorUnavailableError(f"akshare 行情抓取失败（{symbol}）: " + "；".join(errors))

    _MEM[key] = (time.time(), data.copy())
    return data


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """`get_stock_data` 的 akshare 实现，返回格式与 yfinance vendor 保持一致。"""
    data = fetch_ohlcv(symbol, start_date, end_date)

    end_dt = pd.to_datetime(end_date).normalize()
    data = data[data["Date"] <= end_dt].copy()
    if data.empty:
        raise NoMarketDataError(symbol, symbol, f"区间 {start_date}~{end_date} 内没有数据")

    out = data.copy()
    out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
    for col in ("Open", "High", "Low", "Close"):
        if col in out.columns:
            out[col] = out[col].map(lambda v: f"{v:.2f}", na_action="ignore")

    header = f"# Stock data for {symbol} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(out)}（数据源：akshare 东方财富，前复权）\n\n"
    return header + out.to_csv(index=False)


def make_load_ohlcv_fallback(original, ohlcv_module):
    """给 `load_ohlcv` 包一层：yfinance 失败时改用 akshare，其余逻辑复用上游。"""

    def load_ohlcv(symbol, as_of_date, fill_gaps=True):
        try:
            return original(symbol, as_of_date, fill_gaps=fill_gaps)
        except Exception as exc:
            parsed = parse_symbol(symbol)
            if parsed is None:
                raise
            logger.warning("yfinance 取数失败（%s），改用 akshare 兜底: %s", symbol, exc)

            as_of_dt = pd.to_datetime(as_of_date).normalize()
            start_dt = as_of_dt - pd.DateOffset(years=_LOOKBACK_YEARS)
            try:
                data = fetch_ohlcv(
                    symbol,
                    start_dt.strftime("%Y-%m-%d"),
                    as_of_dt.strftime("%Y-%m-%d"),
                )
            except Exception:
                raise exc  # akshare 也不行，抛出原始错误

            # 复用上游的清洗 / 时点过滤 / 缺口填充 / 陈旧校验
            data = ohlcv_module._clean_dataframe(data)
            data = data[data["Date"] <= as_of_dt]
            if data.empty:
                raise NoMarketDataError(symbol, symbol, f"{as_of_date} 之前没有数据")

            data = (
                ohlcv_module._fill_price_gaps(data)
                if fill_gaps
                else data.dropna(subset=["Close"]).copy()
            )
            ohlcv_module._assert_ohlcv_not_stale(data, as_of_date, symbol, symbol)
            return data

    return load_ohlcv
