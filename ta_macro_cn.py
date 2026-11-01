"""中国 / 香港宏观数据源（akshare，免费、无需 API Key）。

作为 FRED 的补充 vendor 注册到 `get_macro_indicators`。上游的 FRED 以美国序列
为主，对港股 / A 股标的的宏观环境覆盖很弱；这里用 akshare 抓取国内公开数据，
覆盖货币、通胀、增长、景气、利率几类核心指标。

为避免修改上游源码，本模块由 `ta_run.py` 在启动时 monkey-patch 注册进
`tradingagents.dataflows.router.VENDOR_METHODS`，并把 `data_vendors.macro_data`
设为 `akshare_cn,fred`（akshare 优先，未覆盖的美国指标回落到 FRED）。

选源注意：akshare 里同一指标常有多个接口，数据新鲜度差别很大。本模块统一用
东方财富系接口（`macro_china_cpi` 等），实测能到 2026-09/10；而"金十"系接口
（`macro_china_cpi_yearly` 等）停在 2025-09，不可用。

时点一致性：akshare 不提供 FRED 那样的 vintage 修订机制。本模块给每个指标配
`lag_days` 近似"发布滞后"，只有「观测期结束 + lag_days <= as_of_date」的数据
才会出现在结果里，避免回测看到尚未公布的数字。这是近似，不是精确公告日。
"""
import logging
import re
from datetime import datetime, timedelta

from tradingagents.dataflows.errors import VendorUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 1095  # 3 年：月度指标约 36 个观测，够看趋势
MAX_ROWS = 24                 # 渲染表格的行数上限，避免刷爆上下文

# 每个指标：akshare 接口、日期列、取值列、展示名、单位、频率、发布滞后天数
SPECS: dict[str, dict] = {
    "china_cpi": {
        "fn": "macro_china_cpi", "date": "月份", "value": "全国-同比增长",
        "title": "中国 CPI 同比", "unit": "%", "freq": "月度", "lag_days": 10,
    },
    "china_ppi": {
        "fn": "macro_china_ppi", "date": "月份", "value": "当月同比增长",
        "title": "中国 PPI 同比", "unit": "%", "freq": "月度", "lag_days": 10,
    },
    "china_m2": {
        "fn": "macro_china_money_supply", "date": "月份", "value": "货币和准货币(M2)-同比增长",
        "title": "中国 M2 同比", "unit": "%", "freq": "月度", "lag_days": 15,
    },
    "china_m1": {
        "fn": "macro_china_money_supply", "date": "月份", "value": "货币(M1)-同比增长",
        "title": "中国 M1 同比（企业活期资金，领先性更强）", "unit": "%", "freq": "月度", "lag_days": 15,
    },
    "china_pmi": {
        "fn": "macro_china_pmi", "date": "月份", "value": "制造业-指数",
        "title": "中国官方制造业 PMI", "unit": "指数", "freq": "月度", "lag_days": 2,
    },
    "china_nonmanufacturing_pmi": {
        "fn": "macro_china_pmi", "date": "月份", "value": "非制造业-指数",
        "title": "中国非制造业商务活动指数", "unit": "指数", "freq": "月度", "lag_days": 2,
    },
    "china_gdp": {
        "fn": "macro_china_gdp", "date": "季度", "value": "国内生产总值-同比增长",
        "title": "中国 GDP 同比", "unit": "%", "freq": "季度", "lag_days": 20,
    },
    "lpr_1y": {
        "fn": "macro_china_lpr", "date": "TRADE_DATE", "value": "LPR1Y",
        "title": "中国 1 年期 LPR（贷款市场报价利率）", "unit": "%", "freq": "月度", "lag_days": 0,
    },
    "lpr_5y": {
        "fn": "macro_china_lpr", "date": "TRADE_DATE", "value": "LPR5Y",
        "title": "中国 5 年期以上 LPR（房贷定价基准）", "unit": "%", "freq": "月度", "lag_days": 0,
    },
    "hk_unemployment": {
        "fn": "macro_china_hk_rate_of_unemployment", "date": "发布日期", "value": "现值",
        "title": "中国香港失业率", "unit": "%", "freq": "月度", "lag_days": 0,
    },
    "shibor_on": {
        "fn": "macro_china_shibor_all", "date": "日期", "value": "O/N-定价",
        "title": "SHIBOR 隔夜（银行间流动性）", "unit": "%", "freq": "日度", "lag_days": 0,
    },
}

# 别名 → 规范名。FRED 风格的写法也一并映射，让分析师按原提示词提问时仍能命中。
ALIASES: dict[str, str] = {
    "lpr": "lpr_1y",
    "policy_rate": "lpr_1y",
    "china_lpr": "lpr_1y",
    "china_policy_rate": "lpr_1y",
    "cpi": "china_cpi",
    "cpi_yoy": "china_cpi",
    "inflation": "china_cpi",
    "chr_cpi": "china_cpi",
    "ppi": "china_ppi",
    "m2": "china_m2",
    "gdp": "china_gdp",
    "real_gdp": "china_gdp",
    "pmi": "china_pmi",
    "manufacturing_pmi": "china_pmi",
    "nonmanufacturing_pmi": "china_nonmanufacturing_pmi",
    "unemployment": "hk_unemployment",
    "hk_rate_of_unemployment": "hk_unemployment",
    "shibor": "shibor_on",
    "interbank": "shibor_on",
    "on_rate": "shibor_on",
}

# akshare 的抓取是网络调用，同一次运行里同一序列会被问多次，缓存一下
_CACHE: dict[str, object] = {}

_MONTH_RE = re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月(?:份)?$")
_QUARTER_RE = re.compile(r"^(\d{4})\s*年\s*第\s*(\d)\s*(?:[-~至]\s*(\d))?\s*季度$")


def _period_to_date(raw: str):
    """把「2026年09月份」「2026年第1-3季度」这类中文期间解析成期末日期。"""
    import pandas as pd

    text = str(raw).strip()

    m = _MONTH_RE.match(text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)

    m = _QUARTER_RE.match(text)
    if m:
        year, start_q, end_q = int(m.group(1)), int(m.group(2)), m.group(3)
        last_q = int(end_q) if end_q else start_q
        month = min(last_q * 3, 12)
        return pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)

    return pd.to_datetime(text, errors="coerce")


def _normalize(indicator: str) -> str | None:
    key = (indicator or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in SPECS:
        return key
    return ALIASES.get(key)


def _fetch(fn_name: str):
    """按函数名抓取 DataFrame，带进程内缓存。"""
    if fn_name in _CACHE:
        return _CACHE[fn_name]

    import akshare as ak

    fn = getattr(ak, fn_name, None)
    if fn is None:
        raise VendorUnavailableError(f"akshare 缺少接口 {fn_name}")

    try:
        df = fn()
    except Exception as exc:  # 网络 / 反爬 / 上游改版
        raise VendorUnavailableError(f"akshare {fn_name} 抓取失败: {exc}") from exc

    _CACHE[fn_name] = df
    return df


def _series(spec: dict):
    """取出 (date, value) 序列，按日期升序，剔除空值。"""
    import pandas as pd

    df = _fetch(spec["fn"])
    date_col, value_col = spec["date"], spec["value"]
    if date_col not in df.columns or value_col not in df.columns:
        raise VendorUnavailableError(
            f"{spec['fn']} 缺少列 {date_col!r}/{value_col!r}，可用列: {list(df.columns)}"
        )

    out = df[[date_col, value_col]].copy()
    out.columns = ["date", "value"]
    out["date"] = out["date"].map(_period_to_date)
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out.dropna(subset=["date", "value"]).sort_values("date")
    return out


def _guidance(indicator: str) -> str:
    listing = "\n".join(
        f"- `{name}` — {spec['title']}（{spec['freq']}，{spec['unit']}）"
        for name, spec in SPECS.items()
    )
    return (
        f"未知的宏观指标 {indicator!r}。当前 akshare 数据源支持的指标：\n{listing}\n\n"
        "也可用别名：cpi / ppi / m2 / gdp / real_gdp / pmi / lpr / policy_rate / "
        "unemployment / shibor。"
    )


def get_macro_data(indicator: str, as_of_date: str, look_back_days: int | None = None) -> str:
    """Fetch a China / Hong Kong macro series as a formatted markdown report.

    Args:
        indicator: 指标名或别名，见 ``SPECS`` / ``ALIASES``。
        as_of_date: 截止日期（yyyy-mm-dd）。只返回该日之前已公布的数据。
        look_back_days: 回看窗口天数，``None`` 用 DEFAULT_LOOKBACK_DAYS。

    Returns:
        包含序列标题、单位、频率、最新值、窗口内变化与近期观测表的 markdown。
    """
    key = _normalize(indicator)
    if key is None:
        return _guidance(indicator)

    spec = SPECS[key]
    look_back_days = DEFAULT_LOOKBACK_DAYS if look_back_days is None else look_back_days

    end_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=look_back_days)
    # 发布滞后：观测期结束 + lag_days 之后才算"已公布"
    available_before = end_dt - timedelta(days=spec.get("lag_days", 0))

    series = _series(spec)
    window = series[(series["date"] <= available_before) & (series["date"] >= start_dt)]

    if window.empty:
        # 窗口内没有观测时退化为「截止日之前的最近值」，总比空表有用
        window = series[series["date"] <= available_before].tail(1)
    if window.empty:
        raise VendorUnavailableError(f"{spec['title']} 在 {as_of_date} 之前没有可用观测")

    latest_date = window["date"].iloc[-1]
    latest_value = window["value"].iloc[-1]
    first_value = window["value"].iloc[0]
    change = latest_value - first_value
    stale_days = (available_before - latest_date).days

    lines = [
        f"### {spec['title']}",
        "",
        "- 数据源：akshare（中国公开数据，东方财富系接口）",
        f"- 单位：{spec['unit']}　频率：{spec['freq']}",
        f"- 最新观测：{latest_date:%Y-%m} = **{latest_value:g}**",
        f"- 窗口内变化（{first_value:g} → {latest_value:g}）：{change:+g}",
    ]
    if stale_days > 45:
        lines.append(f"- ⚠ 数据已滞后 {stale_days} 天，引用时请注明期间")
    lines += [
        "",
        "| 期间 | 数值 |",
        "| --- | --- |",
    ]
    for _, row in window.tail(MAX_ROWS).iloc[::-1].iterrows():
        lines.append(f"| {row['date']:%Y-%m} | {row['value']:g} |")

    return "\n".join(lines)
