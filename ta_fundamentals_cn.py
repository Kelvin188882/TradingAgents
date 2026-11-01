"""A 股 / 港股基本面数据源（按公告日期做时点过滤）。

问题：上游 yfinance 的 `Ticker.info` 是"当前快照"，没有历史版本，所以框架在分析
历史日期时会主动屏蔽基本面（`date_window.withhold_live_profile`）。结果是对
`600519.SS` 跑 2026-10-30 的分析，基本面分析师完全没有数据可用。

这里用东方财富的财报接口补上，关键差异是**每条报表都带公告日期**：

    600519 2026Q3  报告期 2026-09-30  公告日 2026-10-31

所以做 2026-10-30 的分析时，2026Q3 还没披露——自动退回到 2026H1（公告日
2026-08-15），不会读到未来数据。港股财报接口没有公告日期，改用法定披露期限做
保守回退（年报 +120 天、中报 +90 天），同样不会提前。

估值：A 股用"最新可见报告期的每股收益 / 每股净资产"配合分析日收盘价计算
PE、PB、总市值，避免用当前市值之类没有历史版本的数字。
"""
import logging

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

logger = logging.getLogger(__name__)

_KEY_COLUMNS = ("REPORT_DATE", "NOTICE_DATE")

# 利润表 / 资产负债表 / 现金流量表要保留的科目（东财英文字段名）
_INCOME_FIELDS = [
    ("TOTAL_OPERATE_INCOME", "营业总收入"),
    ("OPERATE_INCOME", "营业收入"),
    ("OPERATE_COST", "营业成本"),
    ("SALE_EXPENSE", "销售费用"),
    ("MANAGE_EXPENSE", "管理费用"),
    ("RESEARCH_EXPENSE", "研发费用"),
    ("FINANCE_EXPENSE", "财务费用"),
    ("OPERATE_PROFIT", "营业利润"),
    ("TOTAL_PROFIT", "利润总额"),
    ("NETPROFIT", "净利润"),
    ("PARENT_NETPROFIT", "归母净利润"),
    ("DEDUCT_PARENT_NETPROFIT", "扣非归母净利润"),
    ("BASIC_EPS", "基本每股收益"),
]
_BALANCE_FIELDS = [
    ("MONETARYFUNDS", "货币资金"),
    ("ACCOUNTS_RECE", "应收账款"),
    ("INVENTORY", "存货"),
    ("TOTAL_CURRENT_ASSETS", "流动资产合计"),
    ("FIXED_ASSET", "固定资产"),
    ("GOODWILL", "商誉"),
    ("TOTAL_ASSETS", "资产总计"),
    ("SHORT_LOAN", "短期借款"),
    ("LONG_LOAN", "长期借款"),
    ("TOTAL_CURRENT_LIAB", "流动负债合计"),
    ("TOTAL_LIABILITIES", "负债合计"),
    ("TOTAL_PARENT_EQUITY", "归母股东权益"),
    ("TOTAL_EQUITY", "股东权益合计"),
]
_CASHFLOW_FIELDS = [
    ("TOTAL_OPERATE_INFLOW", "经营活动现金流入"),
    ("TOTAL_OPERATE_OUTFLOW", "经营活动现金流出"),
    ("NETCASH_OPERATE", "经营活动现金流量净额"),
    ("NETCASH_INVEST", "投资活动现金流量净额"),
    ("NETCASH_FINANCE", "筹资活动现金流量净额"),
    ("END_CASH_EQUIVALENTS", "期末现金及等价物"),
]

# 港股报表的科目名是中文且与 A 股口径不同，单独列一份
_HK_FIELDS = {
    "利润表": ["营业额", "毛利", "经营溢利", "除税前溢利", "除税后溢利",
              "股东应占溢利", "融资成本", "加:折旧及摊销"],
    "资产负债表": ["流动资产合计", "总资产", "流动负债合计", "总负债", "净资产",
                  "股东权益", "总权益", "现金及等价物", "存货", "应收帐款",
                  "短期贷款", "长期贷款"],
    "现金流量表": ["经营业务现金净额", "投资业务现金净额", "融资业务现金净额",
                  "期末现金", "购建固定资产", "已付股息(融资)"],
}


def _a_symbol(ticker: str) -> str | None:
    """600519.SS -> SH600519（东财报表接口的代码格式）。"""
    text = (ticker or "").strip().upper()
    if text.endswith(".SS"):
        return f"SH{text[:-3]}"
    if text.endswith(".SZ"):
        return f"SZ{text[:-3]}"
    if text.endswith(".BJ"):
        return f"BJ{text[:-3]}"
    return None


def _hk_code(ticker: str) -> str | None:
    """0700.HK -> 00700。"""
    text = (ticker or "").strip().upper()
    return text[:-3].zfill(5) if text.endswith(".HK") else None


def _report_date(value) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.normalize()


def _hk_notice_date(report_date: pd.Timestamp) -> pd.Timestamp:
    """港股财报没有公告日期，按法定披露期限保守估计"最早可见日"。

    港股主板：年报须在财年结束后 4 个月内、中报 3 个月内、季报 45 天内刊发。
    用期限上限而不是实际上限，保证分析日看不到尚未公开的数字。
    """
    month_day = (report_date.month, report_date.day)
    if month_day == (12, 31):
        days = 120
    elif month_day == (6, 30):
        days = 90
    else:
        days = 45
    return report_date + pd.Timedelta(days=days)


def _visible(df: pd.DataFrame, as_of_date: str) -> pd.DataFrame:
    """按公告日期裁到分析日为止，按报告期倒序。"""
    if df is None or df.empty:
        return pd.DataFrame()
    data = df.copy()
    data["_report"] = data["REPORT_DATE"].map(_report_date)
    if "NOTICE_DATE" in data.columns:
        data["_notice"] = data["NOTICE_DATE"].map(_report_date)
    else:
        data["_notice"] = data["_report"].map(_hk_notice_date)
    data = data.dropna(subset=["_report", "_notice"])
    as_of = pd.to_datetime(as_of_date).normalize()
    data = data[data["_notice"] <= as_of]
    return data.sort_values("_report", ascending=False)


def _quarters_back(as_of: pd.Timestamp, count: int) -> list[str]:
    """分析日往前数 count 个季末（yyyyMMdd），东财业绩报表按季末取数。"""
    quarter = pd.Timestamp(year=as_of.year, month=((as_of.month - 1) // 3) * 3 + 3, day=1) + pd.offsets.MonthEnd(0)
    if quarter > as_of:
        quarter = quarter - pd.offsets.QuarterEnd(1)
    return [f"{(quarter - pd.offsets.QuarterEnd(i)):%Y%m%d}" for i in range(count)]


def _latest_visible_yjbb(code: str, as_of_date: str) -> tuple[dict, str] | None:
    """取分析日当时已披露的最新一期业绩报表（含每股收益、每股净资产）。

    `code` 是纯 6 位代码：东财业绩报表的「股票代码」列不带交易所前缀，
    而三张报表接口要的是 SH600519 这种格式，两者不能混用。
    """
    import akshare as ak

    as_of = pd.to_datetime(as_of_date).normalize()
    for quarter in _quarters_back(as_of, 6):
        try:
            table = ak.stock_yjbb_em(date=quarter)
        except Exception as exc:
            logger.warning("东财业绩报表 %s 拉取失败: %s", quarter, exc)
            continue
        if table is None or table.empty:
            continue
        row = table[table["股票代码"] == code]
        if row.empty:
            continue
        record = row.iloc[0].to_dict()
        notice = _report_date(record.get("最新公告日期"))
        if notice is None or notice > as_of:
            continue  # 该期在分析日尚未公告
        return record, quarter
    return None


def _a_statements(code: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """三张报表原始数据（不裁时间）。"""
    import akshare as ak

    try:
        income = ak.stock_profit_sheet_by_report_em(symbol=code)
        balance = ak.stock_balance_sheet_by_report_em(symbol=code)
        cashflow = ak.stock_cash_flow_sheet_by_report_em(symbol=code)
    except Exception as exc:
        raise VendorUnavailableError(f"东财财报接口不可达（{code}）: {exc}") from exc
    return income, balance, cashflow


def _hk_statement(code: str, symbol: str) -> pd.DataFrame:
    """港股报表：东财返回长表（科目 × 报告期），这里透成宽表。"""
    import akshare as ak

    try:
        raw = ak.stock_financial_hk_report_em(stock=code, symbol=symbol, indicator="报告期")
    except Exception as exc:
        raise VendorUnavailableError(f"东财港股财报不可达（{code}/{symbol}）: {exc}") from exc
    if raw is None or raw.empty:
        return pd.DataFrame()
    wide = raw.pivot_table(
        index="REPORT_DATE", columns="STD_ITEM_NAME", values="AMOUNT", aggfunc="first"
    ).reset_index()
    wide["NOTICE_DATE"] = wide["REPORT_DATE"].map(_report_date).map(_hk_notice_date)
    return wide


def _fmt_money(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    for unit, scale in (("亿", 1e8), ("万", 1e4)):
        if abs(number) >= scale:
            return f"{number / scale:,.2f} {unit}"
    return f"{number:,.2f}"


def _fmt_pct(value) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _latest_close(ticker: str, as_of_date: str) -> float | None:
    """分析日收盘价，用于估值。取不到就跳过估值，不让基本面整体失败。"""
    try:
        from ta_market_cn import fetch_ohlcv

        start = (pd.to_datetime(as_of_date) - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
        data = fetch_ohlcv(ticker, start, as_of_date)
        return None if data.empty else float(data.iloc[-1]["Close"])
    except Exception as exc:
        logger.info("估值用收盘价取不到（%s）: %s", ticker, exc)
        return None


def _a_fundamentals(ticker: str, code: str, as_of_date: str) -> str:
    hit = _latest_visible_yjbb(code[2:], as_of_date)
    if hit is None:
        raise VendorUnavailableError(
            f"东财在 {as_of_date} 之前没有 {ticker} 的业绩报表记录"
        )
    record, quarter = hit
    quarter_dt = pd.to_datetime(quarter)
    notice = _report_date(record.get("最新公告日期"))

    lines = [
        f"# Company Fundamentals for {ticker}",
        f"# Point-in-time as of: {as_of_date}",
        "# 数据源：东方财富财报接口（按公告日期过滤，仅含该日已披露的数据）",
        "",
        f"- 最新可用报告期：{quarter_dt:%Y-%m-%d}（公告日 {notice:%Y-%m-%d}）",
        f"- 股票简称：{record.get('股票简称', '—')}",
    ]
    if record.get("所处行业"):
        lines.append(f"- 所处行业：{record.get('所处行业')}")

    lines += [
        "",
        "## 经营数据（报告期累计）",
        f"- 营业总收入：{_fmt_money(record.get('营业总收入-营业总收入'))}"
        f"（同比 {_fmt_pct(record.get('营业总收入-同比增长'))}）",
        f"- 净利润：{_fmt_money(record.get('净利润-净利润'))}"
        f"（同比 {_fmt_pct(record.get('净利润-同比增长'))}）",
        f"- 每股收益：{_fmt_num(record.get('每股收益'))}",
        f"- 每股净资产：{_fmt_num(record.get('每股净资产'))}",
        f"- 净资产收益率 ROE：{_fmt_pct(record.get('净资产收益率'))}",
        f"- 销售毛利率：{_fmt_pct(record.get('销售毛利率'))}",
        f"- 每股经营现金流：{_fmt_num(record.get('每股经营现金流量'))}",
    ]

    price = _latest_close(ticker, as_of_date)
    eps, bps = record.get("每股收益"), record.get("每股净资产")
    if price:
        lines += ["", f"## 估值（收盘价 {price:.2f}，{as_of_date}）"]
        try:
            eps_f, bps_f = float(eps), float(bps)
            months = quarter_dt.month
            annualised = eps_f * 12 / months if eps_f else None
            if annualised:
                lines.append(f"- 市盈率 PE（按报告期年化）：{price / annualised:.2f} 倍")
            if bps_f:
                lines.append(f"- 市净率 PB：{price / bps_f:.2f} 倍")
        except (TypeError, ValueError):
            lines.append("- 估值倍数因数据缺失无法计算")
    else:
        lines += ["", "## 估值", "- 分析日收盘价取不到，估值倍数未计算"]

    lines += [
        "",
        "## 说明",
        "- 以上仅为分析日之前已公告的最近一期报告，季度数据为年初至今累计口径。",
        "- 需要完整科目明细可调用 get_income_statement / get_balance_sheet / get_cashflow。",
    ]
    return "\n".join(lines)


def _hk_fundamentals(ticker: str, code: str, as_of_date: str) -> str:
    income = _visible(_hk_statement(code, "利润表"), as_of_date)
    balance = _visible(_hk_statement(code, "资产负债表"), as_of_date)
    if income.empty:
        raise VendorUnavailableError(f"东财在 {as_of_date} 之前没有 {ticker} 的港股利润表")

    latest = income.iloc[0]
    report_dt = latest["_report"]
    notice_dt = latest["_notice"]

    lines = [
        f"# Company Fundamentals for {ticker}",
        f"# Point-in-time as of: {as_of_date}",
        "# 数据源：东方财富港股财报接口（港股无公告日期，按法定披露期限估算可见日）",
        "",
        f"- 最新可用报告期：{report_dt:%Y-%m-%d}（估算可见日 {notice_dt:%Y-%m-%d}）",
    ]
    for field in ("营业额", "毛利", "经营溢利", "股东应占溢利", "除税后溢利"):
        if field in latest.index:
            lines.append(f"- {field}：{_fmt_money(latest[field])}")

    # 同比：同一科目上一年同期
    prior = income[income["_report"] == report_dt - pd.DateOffset(years=1)]
    if not prior.empty and "营业额" in latest.index and "营业额" in prior.iloc[0].index:
        try:
            now_v, prev_v = float(latest["营业额"]), float(prior.iloc[0]["营业额"])
            if prev_v:
                lines.append(f"- 营业额同比：{_fmt_pct((now_v / prev_v - 1) * 100)}")
        except (TypeError, ValueError):
            pass

    if not balance.empty:
        row = balance.iloc[0]
        lines += ["", f"## 资产负债（{row['_report']:%Y-%m-%d}）"]
        for field in ("总资产", "总负债", "净资产", "股东权益", "现金及等价物"):
            if field in row.index:
                lines.append(f"- {field}：{_fmt_money(row[field])}")

    lines += [
        "",
        "## 说明",
        "- 港股财报披露无固定公告日字段，可见日按主板法定披露期限上限估算，不会提前。",
        "- 需要明细可调用 get_income_statement / get_balance_sheet / get_cashflow。",
    ]
    return "\n".join(lines)


def get_fundamentals(ticker: str, as_of_date: str = None) -> str:
    """`get_fundamentals` 的东财实现，支持历史日期（按公告日期时点过滤）。"""
    as_of_date = as_of_date or pd.Timestamp.today().strftime("%Y-%m-%d")
    code = _a_symbol(ticker)
    if code:
        return _a_fundamentals(ticker, code, as_of_date)
    hk = _hk_code(ticker)
    if hk:
        return _hk_fundamentals(ticker, hk, as_of_date)
    raise VendorUnavailableError(f"东财基本面仅覆盖 A 股 / 港股: {ticker!r}")


def _statement_csv(ticker: str, freq: str, as_of_date: str, fields, sheet_name: str, limit: int = 8) -> str:
    """把报表转成"报告期为行、科目为列"的 CSV，只保留分析日已公告的期次。"""
    as_of_date = as_of_date or pd.Timestamp.today().strftime("%Y-%m-%d")
    code = _a_symbol(ticker)
    if code:
        income, balance, cashflow = _a_statements(code)
        source = {"利润表": income, "资产负债表": balance, "现金流量表": cashflow}[sheet_name]
        data = _visible(source, as_of_date)
        if data.empty:
            return f"No {sheet_name} data for {ticker} as of {as_of_date}"
        yearly = freq.lower().startswith("annual")
        if yearly:
            data = data[data["_report"].dt.strftime("%m-%d") == "12-31"]
        rows = []
        for _, row in data.head(limit).iterrows():
            item = {"报告期": f"{row['_report']:%Y-%m-%d}", "公告日": f"{row['_notice']:%Y-%m-%d}"}
            for col, label in fields:
                if col in row.index:
                    item[label] = row[col]
            rows.append(item)
        frame = pd.DataFrame(rows)
        return (
            f"# {sheet_name} data for {ticker}（数据源：东方财富，按公告日期过滤）\n"
            f"# 单位：元；季度/中报为年初至今累计口径\n" + frame.to_csv(index=False)
        )

    hk = _hk_code(ticker)
    if hk:
        data = _visible(_hk_statement(hk, sheet_name), as_of_date)
        if data.empty:
            return f"No {sheet_name} data for {ticker} as of {as_of_date}"
        if freq.lower().startswith("annual"):
            data = data[data["_report"].dt.strftime("%m-%d") == "12-31"]
        keep = [c for c in _HK_FIELDS.get(sheet_name, []) if c in data.columns]
        out = data[["_report", "_notice"] + keep].head(limit).copy()
        out["_report"] = out["_report"].dt.strftime("%Y-%m-%d")
        out["_notice"] = out["_notice"].dt.strftime("%Y-%m-%d")
        out = out.rename(columns={"_report": "报告期", "_notice": "估算可见日"})
        return (
            f"# {sheet_name} data for {ticker}（数据源：东方财富港股，单位：元）\n"
            + out.to_csv(index=False)
        )

    raise VendorUnavailableError(f"东财财报仅覆盖 A 股 / 港股: {ticker!r}")


def get_income_statement(ticker: str, freq: str = "quarterly", as_of_date: str = None) -> str:
    return _statement_csv(ticker, freq, as_of_date, _INCOME_FIELDS, "利润表")


def get_balance_sheet(ticker: str, freq: str = "quarterly", as_of_date: str = None) -> str:
    return _statement_csv(ticker, freq, as_of_date, _BALANCE_FIELDS, "资产负债表")


def get_cashflow(ticker: str, freq: str = "quarterly", as_of_date: str = None) -> str:
    return _statement_csv(ticker, freq, as_of_date, _CASHFLOW_FIELDS, "现金流量表")
