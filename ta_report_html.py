"""中文优先的报告版式（本地覆盖层，不改上游源码）。

上游 `tradingagents/report_html.py` 是一份英文衬线体文章模板，直接用在中国
A 股 / 港股报告上有三个硬伤：

1. 正文字族是 `Charter, Georgia, serif`，里面没有任何中文字体，中文会掉到
   系统默认（macOS 上是无衬线的 PingFang），衬线西文与无衬线中文混排，基线
   和字重都打架；行高 1.62 对中文偏紧（中文安全区 1.75–1.9）。
2. 配色沿用的是美股习惯（绿涨红跌），与 A 股「红涨绿跌」相反，读涨跌时会
   反向理解。
3. 整份报告最该被看见的三个数——评级、目标价、时间窗口——埋在正文第五章，
   首屏只有 ticker + 一个英文单词评级。

这里重排一版：首屏给决策卡，正文给中文排版系统，涨跌改成 A 股语义色。
由 `ta_run.py` 在 CLI 启动前替换 `tradingagents.report_html.render_report`，
上游文件一行不动，`git pull` 不会冲突。
"""

import re
from datetime import datetime
from html import escape

from markdown_it import MarkdownIt

from tradingagents import report_html as _upstream
from tradingagents.agents.rating import run_rating
from tradingagents.reporting import analyst_names

# 复用上游的 logo 与链接，避免资源重复；上游改名时降级为纯文字。
_LOGO = getattr(_upstream, "_LOGO", "")
_COMPANY = getattr(_upstream, "_COMPANY", "https://tauric.ai")
_ORG = getattr(_upstream, "_ORG", "https://github.com/TauricResearch")
_PROJECT = getattr(_upstream, "_PROJECT", f"{_ORG}/TradingAgents")

# 五档评级的中文名：报告正文是中文，首屏不该让读者先翻译一个英文单词。
_RATING_ZH = {
    "Buy": "买入", "Bullish": "看多", "Mildly Bullish": "温和偏多",
    "Overweight": "增持",
    "Hold": "持有", "Neutral": "中性",
    "Underweight": "减持",
    "Sell": "卖出", "Bearish": "看空", "Mildly Bearish": "温和偏空",
}
# 方向语义：买入/增持为多（A 股红），减持/卖空为空（A 股绿），持有中性。
_TONE = {
    "Buy": "up", "Overweight": "up",
    "Hold": "level",
    "Underweight": "down", "Sell": "down",
    "Bullish": "up", "Mildly Bullish": "up",
    "Bearish": "down", "Mildly Bearish": "down", "Neutral": "level",
}

# 章节与角色的中文名。框架是英文的，但读的人在看中文报告。
_SECTION_ZH = {
    "Analyst Team Reports": "分析师团队报告",
    "Research Team Decision": "研究团队决策",
    "Trading Team Plan": "交易团队方案",
    "Risk Management Team Decision": "风险管理团队决策",
    "Portfolio Manager Decision": "组合经理决策",
}
_AGENT_ZH = {
    "Market Analyst": "行情分析师",
    "Sentiment Analyst": "情绪分析师",
    "News Analyst": "新闻分析师",
    "Fundamentals Analyst": "基本面分析师",
    "Bull Researcher": "多头研究员",
    "Bear Researcher": "空头研究员",
    "Research Manager": "研究主管",
    "Trader": "交易员",
    "Aggressive Analyst": "激进风险分析师",
    "Conservative Analyst": "保守风险分析师",
    "Neutral Analyst": "中性风险分析师",
    "Portfolio Manager": "组合经理",
}
# 只有多空研究员带方向标签：风险分析师三种立场不代表涨跌，上色会是假语义。
_AGENT_ROLE = {
    "Bull Researcher": ("多方", "up"),
    "Bear Researcher": ("空方", "down"),
}

_MARKET = {
    ".SS": "沪市 A 股", ".SH": "沪市 A 股",
    ".SZ": "深市 A 股", ".BJ": "北交所",
    ".HK": "港股", ".T": "日股",
}

_CATEGORY_ZH = {
    "core_stock_apis": "行情", "technical_indicators": "指标",
    "fundamental_data": "基本面", "news_data": "新闻",
    "macro_data": "宏观", "prediction_markets": "预测市场",
}

# 行宽与字号的设定说明见 _CSS 里的 `--measure`：36rem ≈ 34 个中文字 / 行。



_CSS = """
:root {
  /* 纸张与外框 */
  --paper: #fbfaf7;
  --surface: #ffffff;
  --wash: #f5f3ee;
  --hairline: #e6e3dc;
  --edge: #d6d2c8;

  /* 文字三级 */
  --ink: #191d21;
  --ink-2: #3a434b;
  --muted: #5f6870;

  /* A 股语义色：红涨绿跌。链接避让红绿，走蓝，否则「可点」会被读成「涨/跌」 */
  --up: #c0362a;
  --down: #0d7350;
  --up-soft: #fbebe8;
  --down-soft: #e6f4ee;
  --level: #5f6870;
  --accent: #1a5cae;
  --accent-soft: #ebf1fa;

  --sans: system-ui, -apple-system, "Segoe UI", "PingFang SC",
          "HarmonyOS Sans SC", "Source Han Sans SC", "Noto Sans SC",
          "Microsoft YaHei", sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;

  /* 间距尺度：全部落在 4 的倍数上 */
  --sp-1: 4px;  --sp-2: 8px;  --sp-3: 12px; --sp-4: 16px;
  --sp-6: 24px; --sp-8: 32px; --sp-12: 48px; --sp-16: 64px; --sp-20: 80px;

  --rail: 13.5rem;
  --gutter: 4rem;
  /* 36rem ≈ 34 个中文字/行（17px × 34 ≈ 578px），落在中文 28–40 字的舒适区 */
  --measure: 36rem;
  color-scheme: light dark;
}

@media (prefers-color-scheme: dark) {
  :root {
    --paper: #12171b; --surface: #171e23; --wash: #1b242a;
    --hairline: #28323a; --edge: #3a464f;
    --ink: #e6eaee; --ink-2: #c4cdd5; --muted: #97a3ad;
    --up: #ef6d5e; --down: #3fc795;
    --up-soft: #3a1f1c; --down-soft: #14332a;
    --level: #97a3ad; --accent: #74aaf2; --accent-soft: #1a2735;
  }
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font: 17px/1.8 var(--sans);
  font-variant-numeric: tabular-nums;
  -webkit-font-smoothing: antialiased;
}

.page { max-width: 64rem; margin: 0 auto; padding: 0 var(--sp-6) var(--sp-20); }

/* 两栏整体居中，正文列宽度由 --measure 锁死：屏幕再宽，一行也不会超过 34 个中文字。 */
.grid {
  display: grid;
  grid-template-columns: var(--rail) minmax(0, var(--measure));
  column-gap: var(--gutter);
  justify-content: center;
  align-items: start;
}
.grid > .hero { grid-column: 2; grid-row: 1; }
.grid > .toc  { grid-column: 1; grid-row: 1 / span 2; }
.grid > main  { grid-column: 2; grid-row: 2; min-width: 0; }

a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 0.18em; }
a:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 2px; }
h1, h2, h3 { text-wrap: balance; }
main :is(h2, h3, h4) { scroll-margin-top: var(--sp-6); }

/* ── 首屏 ───────────────────────────────────────────────────────────── */
/* 首屏与正文共用同一条行宽：卡片比正文宽会造成左右边缘对不齐。 */
.hero { padding-top: var(--sp-12); max-width: var(--measure); }

.eyebrow {
  display: flex; align-items: center; gap: var(--sp-3);
  margin: 0 0 var(--sp-8); font-size: 13px; line-height: 1; color: var(--muted);
}
.eyebrow .brand { display: flex; align-items: center; }
.eyebrow .brand svg { display: block; height: 20px; width: auto; }
.eyebrow .divider { width: 1px; height: 14px; background: var(--edge); }
@media (prefers-color-scheme: dark) {
  .eyebrow .brand [stroke="#363636"] { stroke: var(--muted); }
}

.hero h1 {
  display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-3) var(--sp-4);
  margin: 0; font: 650 2.125rem/1.25 var(--sans); letter-spacing: -0.012em;
}
.hero .market {
  flex: none; font: 600 12px/1 var(--sans); letter-spacing: 0.06em;
  padding: 5px 8px 5px 9px; border-radius: 4px;
  color: var(--muted); background: var(--wash); border: 1px solid var(--hairline);
}
.hero .sub { margin: var(--sp-3) 0 0; font-size: 14px; line-height: 1.6; color: var(--muted); }
.nowrap { white-space: nowrap; }

/* 决策卡：整份报告唯一的「3 秒读懂」区域。
   最终结论 → 两行摘要 → 三项关键数据；不把同一决策拆成卡片外的第二段。 */
.verdict {
  --tone: var(--level);
  margin: var(--sp-8) 0 0;
  padding: var(--sp-6);
  background: var(--surface);
  border: 1px solid var(--hairline);
  border-radius: 12px;
  box-shadow: inset 3px 0 0 var(--tone), 0 1px 2px rgb(0 0 0 / 0.03);
}
.verdict.up { --tone: var(--up); }
.verdict.down { --tone: var(--down); }
.verdict.level { --tone: var(--level); }

.verdict-head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4); }
.verdict-kicker {
  margin: 0; font: 600 12px/1.4 var(--sans);
  letter-spacing: 0.12em; color: var(--muted);
}
.verdict-more { font-size: 13px; line-height: 1.5; white-space: nowrap; }
.verdict-rating {
  margin: var(--sp-1) 0 0; font: 700 1.875rem/1.15 var(--sans);
  letter-spacing: -0.01em; color: var(--tone);
}

.verdict-metrics {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--sp-4); margin: var(--sp-4) 0 0; padding-top: var(--sp-4);
  border-top: 1px solid var(--hairline);
}
.verdict-metrics > div { min-width: 0; }
.verdict-metrics dt {
  margin: 0 0 5px; font: 600 11.5px/1 var(--sans);
  letter-spacing: 0.1em; color: var(--muted);
}
.verdict-metrics dd {
  margin: 0; font: 600 1.0625rem/1.35 var(--sans);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}

.verdict-lead {
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  overflow: hidden; margin: var(--sp-3) 0 0;
  font-size: 15px; line-height: 1.75; color: var(--ink-2);
}
.verdict-points {
  display: grid; gap: var(--sp-2); margin: var(--sp-4) 0 0;
  padding-top: var(--sp-3); border-top: 1px solid var(--hairline);
}
.verdict-point { display: grid; grid-template-columns: 4rem minmax(0, 1fr); gap: var(--sp-3); }
.verdict-point dt { font: 600 12px/1.8 var(--sans); color: var(--muted); }
.verdict-point dd {
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  overflow: hidden; margin: 0; font-size: 13.5px; line-height: 1.65; color: var(--ink-2);
}

/* 分析角度汇总：首屏第二级信息，不与投资评级争夺主视觉。
   整卡点击、两行摘要，去掉额外表格线，让视线只停留在角度、评级和结论上。 */
.angles { margin: var(--sp-8) 0 0; }
.angles-head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--sp-4); }
.angles-head h2 { margin: 0; font: 650 1.0625rem/1.5 var(--sans); }
.angles-head span { font-size: 12px; color: var(--muted); white-space: nowrap; }
.angles-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-3); margin-top: var(--sp-4); }
.angle {
  min-width: 0; display: flex; flex-direction: column; padding: var(--sp-4);
  border: 1px solid var(--hairline); border-radius: 10px; background: var(--surface);
  color: inherit; text-decoration: none;
  transition: border-color 0.15s, background-color 0.15s;
}
.angle:hover { border-color: var(--edge); background: var(--wash); }
.angle:hover .angle-title { color: var(--accent); }
.angle-head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.angle-title { margin: 0; font: 650 15px/1.5 var(--sans); }
.angle-rating {
  flex: none; padding: 2px 7px; border-radius: 4px;
  font: 600 11.5px/1.5 var(--sans); color: var(--level); background: var(--wash);
}
.angle-rating.up { color: var(--up); background: var(--up-soft); }
.angle-rating.down { color: var(--down); background: var(--down-soft); }
.angle-text {
  display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
  overflow: hidden; min-height: 3.4em; margin: var(--sp-2) 0;
  font-size: 13.5px; line-height: 1.7; color: var(--ink-2);
}
.angle-more {
  display: flex; align-items: center; margin-top: auto;
  font-size: 12.5px; line-height: 1.5; color: var(--accent);
}
.angle-more::after { content: "→"; margin-left: var(--sp-2); }

/* 运行参数：辅助信息，默认收起 */
.run { margin: var(--sp-6) 0 0; font-size: 13px; line-height: 1.7; }
.run > summary {
  display: inline-flex; align-items: center; gap: var(--sp-2);
  padding: 6px 10px 6px 8px; border: 1px solid var(--hairline); border-radius: 6px;
  color: var(--muted); cursor: pointer; list-style: none; user-select: none;
}
.run > summary::-webkit-details-marker { display: none; }
.run > summary::before { content: "▸"; font-size: 10px; transition: transform 0.15s; }
.run[open] > summary::before { transform: rotate(90deg); }
.run > summary:hover { color: var(--ink-2); border-color: var(--edge); }
.run dl {
  display: grid; grid-template-columns: max-content minmax(0, 1fr);
  gap: var(--sp-2) var(--sp-6); margin: var(--sp-4) 0 0;
}
.run dl > div { display: contents; }
.run dt { color: var(--muted); }
.run dd { margin: 0; overflow-wrap: anywhere; }
.run .note dd { color: var(--down); }
.run .hint { color: var(--muted); font-style: italic; }

.field {
  display: inline-block; margin: 0 4px 4px 0; padding: 1px 6px;
  border: 1px solid var(--hairline); border-radius: 4px; background: var(--wash);
  font: 12px/1.6 var(--mono);
}
.run .group { display: inline-block; margin-right: var(--sp-4); white-space: nowrap; }
.run .group > .name { color: var(--muted); margin-right: 5px; }
.run .then { color: var(--muted); margin: 0 1px; }

/* ── 目录：宽屏常驻侧栏，窄屏折叠 ────────────────────────────────── */
.toc { position: sticky; top: var(--sp-12); font-size: 13.5px; line-height: 1.5; }
.toc-title {
  margin: 0 0 var(--sp-4); padding-bottom: var(--sp-3);
  border-bottom: 1px solid var(--hairline);
  font: 600 11.5px/1 var(--sans); letter-spacing: 0.14em; color: var(--muted);
}
.toc ol, .toc ul { margin: 0; padding: 0; list-style: none; }
.toc > ol > li { margin: 0 0 var(--sp-4); }
.toc > ol > li > a { font-weight: 600; color: var(--ink); }
.toc ul { margin: var(--sp-2) 0 0; padding-left: var(--sp-4); border-left: 1px solid var(--hairline); }
.toc ul li { margin: 0 0 2px; }
.toc ul a { color: var(--muted); font-weight: 400; }
.toc a { display: block; padding: 3px 0; text-decoration: none; }
.toc a:hover, .toc a:focus-visible { color: var(--accent); text-decoration: underline; }
.toc .num {
  display: inline-block; min-width: 1.6em; margin-right: 4px;
  font-variant-numeric: tabular-nums; color: var(--muted);
}

/* 窄屏目录：整行铺开，排在首屏与正文之间 */
.toc-mobile { display: none; grid-column: 1 / -1; margin: var(--sp-8) 0 0; }
.toc-mobile > summary {
  display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3);
  min-height: 44px; padding: 0 var(--sp-4);
  border: 1px solid var(--hairline); border-radius: 8px; background: var(--surface);
  font-size: 14px; color: var(--ink-2); cursor: pointer; list-style: none;
}
.toc-mobile > summary::-webkit-details-marker { display: none; }
.toc-mobile nav { padding: var(--sp-4) 0 0; }

.to-top {
  position: fixed; right: var(--sp-4); bottom: calc(var(--sp-4) + env(safe-area-inset-bottom));
  display: none; padding: 8px 12px;
  border: 1px solid var(--edge); border-radius: 8px; background: var(--surface);
  color: var(--muted); font: 13px/1.4 var(--sans); text-decoration: none;
  box-shadow: 0 2px 8px rgb(0 0 0 / 0.06);
}

/* ── 正文 ───────────────────────────────────────────────────────────── */
main { max-width: var(--measure); }

main h2 {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--sp-2) var(--sp-3);
  margin: var(--sp-20) 0 var(--sp-6);
  padding-top: var(--sp-6); border-top: 1px solid var(--edge);
  font: 650 1.5rem/1.35 var(--sans); letter-spacing: -0.008em;
}
main .hero + main h2, main section:first-of-type h2 { margin-top: var(--sp-12); }
main h2 .numeral {
  font: 600 12px/1 var(--sans); letter-spacing: 0.16em; color: var(--accent);
}
main h2 .en { font: 400 12.5px/1.4 var(--sans); color: var(--muted); }

main h3 {
  display: flex; align-items: center; gap: var(--sp-3);
  margin: var(--sp-12) 0 var(--sp-4); padding-top: var(--sp-6);
  border-top: 1px solid var(--hairline);
  font: 650 1.3125rem/1.45 var(--sans);
}
main h2 + h3 { margin-top: var(--sp-6); padding-top: 0; border-top: 0; }

.chip {
  flex: none; padding: 3px 7px; border-radius: 4px;
  font: 600 11.5px/1.35 var(--sans); letter-spacing: 0.06em;
}
.chip.up { color: var(--up); background: var(--up-soft); }
.chip.down { color: var(--down); background: var(--down-soft); }

main h4, main h5, main h6 { line-height: 1.65; }
/* Agent 自带的标题被下沉三级：h4 是它的文档名，h5 / h6 才是真正的小节。
   所以 h4 当作副标题处理（小、灰、紧跟 h3），h5 反而比它大——
   视觉层级跟着语义走，不跟着标签编号走，否则正文读起来是平的。 */
main h4 {
  margin: var(--sp-2) 0 var(--sp-4); font-weight: 600; font-size: 0.875rem;
  letter-spacing: 0.02em; color: var(--muted);
}
main h5 {
  margin: var(--sp-8) 0 var(--sp-3); font-weight: 650; font-size: 1.125rem;
  color: var(--ink); letter-spacing: 0.005em;
}
main h6 {
  margin: var(--sp-6) 0 var(--sp-2); font-weight: 650; font-size: 0.9375rem;
  color: var(--ink-2);
}

main p { margin: 0 0 1.15em; }
main ul, main ol { margin: 0 0 1.15em; padding-left: 1.4em; }
main li { margin: 0.3em 0; }
main li::marker { color: var(--muted); }
main strong { font-weight: 650; color: var(--ink); }

main blockquote {
  margin: var(--sp-6) 0; padding: var(--sp-2) 0 var(--sp-2) var(--sp-4);
  border-left: 2px solid var(--edge); color: var(--ink-2);
}
main code {
  font: 0.86em/1.5 var(--mono); background: var(--wash);
  padding: 0.12em 0.34em; border-radius: 4px;
}
main pre {
  margin: 0 0 1.15em; padding: var(--sp-4); border-radius: 8px;
  background: var(--wash); overflow-x: auto;
}
main pre code { background: none; padding: 0; }
main hr { width: var(--sp-12); margin: var(--sp-8) auto; border: 0; border-top: 1px solid var(--edge); }

/* 表格：长报告里最容易读崩的元素 */
.table {
  margin: 0 0 1.35em; overflow-x: auto; overscroll-behavior-x: contain;
  border: 1px solid var(--hairline); border-radius: 10px;
  /* 宽度不够时可横向滚动；这两层阴影只在真的溢出时露在边缘，不给假提示。 */
  background:
    linear-gradient(to right, var(--surface), transparent) left center / 24px 100% no-repeat local,
    linear-gradient(to left, var(--surface), transparent) right center / 24px 100% no-repeat local,
    radial-gradient(farthest-side at 0 50%, rgb(0 0 0 / 0.10), transparent) left center / 14px 100% no-repeat scroll,
    radial-gradient(farthest-side at 100% 50%, rgb(0 0 0 / 0.10), transparent) right center / 14px 100% no-repeat scroll,
    var(--surface);
}
table {
  border-collapse: collapse; width: 100%;
  font-size: 14px; line-height: 1.65; font-variant-numeric: tabular-nums;
}
th, td { padding: 10px; text-align: left; vertical-align: top; }
td { overflow-wrap: break-word; }
.cell { display: block; min-width: 4.5rem; }
th {
  position: sticky; top: 0;
  font-weight: 600; font-size: 12.5px; letter-spacing: 0.04em; color: var(--ink-2);
  background: var(--wash); border-bottom: 1px solid var(--edge); white-space: nowrap;
}
td { border-bottom: 1px solid var(--hairline); }
tbody tr:last-child td { border-bottom: 0; }

.credit {
  margin-top: var(--sp-20); padding-top: var(--sp-4);
  border-top: 1px solid var(--hairline);
  font-size: 12.5px; line-height: 1.6; color: var(--muted);
}
.credit a { color: inherit; text-decoration: none; }
.credit a.brand { color: var(--accent); }
.credit a:hover, .credit a:focus-visible { text-decoration: underline; }

/* ── 断点：1280 / 1024 / 768 / 640 ─────────────────────────────────── */
@media (max-width: 80rem) { .page { max-width: 60rem; } }

@media (max-width: 64rem) {
  .page { max-width: 44rem; padding: 0 var(--sp-4) var(--sp-16); }
  .grid { grid-template-columns: minmax(0, 1fr); }
  .grid > .hero { grid-column: 1; grid-row: 1; }
  .grid > .toc-mobile { grid-row: 2; }
  .grid > main { grid-column: 1; grid-row: 3; }
  .grid > .toc { display: none; }
  .toc-mobile { display: block; }
  .to-top { display: block; }
  .hero { padding-top: var(--sp-8); }
  main h2 { margin-top: var(--sp-16); }
}

@media (max-width: 48rem) {
  .verdict-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .verdict-rating { font-size: 1.75rem; }
}

@media (max-width: 40rem) {
  body { font-size: 16px; line-height: 1.8; }
  .angles-grid { grid-template-columns: minmax(0, 1fr); }
  .page { padding: 0 var(--sp-4) var(--sp-12); }
  .hero h1 { font-size: 1.625rem; }
  .hero .sub { font-size: 13px; }
  .verdict { padding: var(--sp-4) var(--sp-4) var(--sp-4) var(--sp-6); }
  .run dl { grid-template-columns: minmax(0, 1fr); gap: var(--sp-1); }
  .run dd { margin-bottom: var(--sp-3); }
  .run .group { white-space: normal; }
  main h2 { font-size: 1.3125rem; }
  main h3 { font-size: 1.0625rem; }
  th, td { padding: 8px 10px; }
}

@media print {
  :root {
    --paper: #fff; --surface: #fff; --wash: #f4f4f4;
    --ink: #000; --ink-2: #222; --muted: #444;
    --hairline: #ccc; --edge: #999; --accent: #000;
    --up: #b02a1e; --down: #0a5c40; --level: #444;
  }
  body { font-size: 10.5pt; line-height: 1.7; }
  .page { max-width: none; padding: 0; }
  .grid { display: block; }
  .toc, .toc-mobile, .to-top, .run { display: none; }
  .verdict, .angle { break-inside: avoid; box-shadow: none; }
  .angles-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .angle-more { display: none; }
  main { max-width: none; }
  main h2 { break-before: page; margin-top: 0; border-top: 0; padding-top: 0; }
  main section:first-of-type h2 { break-before: auto; }
  main h3, main h4 { break-after: avoid; }
  .table { overflow: visible; break-inside: avoid; }
  a { color: inherit; text-decoration: none; }
}
"""


# 日期在正文里被连字符折断（"2026-" / "10-30"）是中文字号下最刺眼的一处断行。
# 只处理 text token，不碰属性、不碰行内代码。
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# 展示层中文化：只翻译框架字段、角色和投资术语，不改原始 Markdown，
# 也不硬译 RSI / MACD / PE 等技术缩写。多字术语先于单字术语匹配。
_I18N = {
    "FINAL TRANSACTION PROPOSAL": "最终交易建议",
    "Overall Sentiment": "整体情绪", "Executive Summary": "核心摘要",
    "Investment Thesis": "投资逻辑", "Strategic Actions": "策略行动",
    "Position Sizing": "仓位建议", "Price Target": "目标价",
    "Time Horizon": "时间窗口", "Entry Price": "参考入场价",
    "Stop Loss": "止损价", "Recommendation": "综合建议",
    "Rationale": "决策依据", "Reasoning": "决策依据",
    "Confidence": "置信度", "Score": "评分", "Rating": "评级",
    "Action": "操作", "Not provided": "未提供",
    "Aggressive Analyst": "激进风险分析师",
    "Conservative Analyst": "保守风险分析师", "Neutral Analyst": "中性风险分析师",
    "Bull Analyst": "多头研究员", "Bear Analyst": "空头研究员",
    "Mildly Bullish": "温和偏多", "Mildly Bearish": "温和偏空",
    "Underweight": "减持", "Overweight": "增持",
    "Bullish": "看多", "Bearish": "看空", "Neutral": "中性",
    "Hold": "持有", "Buy": "买入", "Sell": "卖出",
    "Bull": "多头", "Bear": "空头", "Low": "低", "High": "高",
    "months": "个月", "month": "个月", "weeks": "周", "week": "周",
    "days": "天", "day": "天", "years": "年", "year": "年",
}
_I18N_LOWER = {key.lower(): value for key, value in _I18N.items()}
_I18N_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(k) for k in sorted(_I18N, key=len, reverse=True)) + r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _localize_text(text: str) -> str:
    return _I18N_RE.sub(lambda match: _I18N_LOWER[match.group(1).lower()], text)


def _text_rule(self, tokens, idx, options, env):
    from markdown_it.common.utils import escapeHtml

    content = _localize_text(tokens[idx].content)
    return _DATE_RE.sub(r'<span class="nowrap">\1</span>', escapeHtml(content))


def _cell_open(tag: str):
    """在单元格里包一层 `.cell`。

    `min-width` 直接写在 td 上，浏览器在 table 自动布局里不保证生效；中文的
    min-content 又只有 1 个字，长描述列会把标签列挤成竖排单字。包一层带
    min-width 的块，才能稳定地把每列撑到可读宽度。
    """

    def rule(self, tokens, idx, options, env):
        return f'<{tag}{self.renderAttrs(tokens[idx])}><span class="cell">'

    return rule


def _cell_close(tag: str):
    def rule(self, tokens, idx, options, env):
        return f"</span></{tag}>"

    return rule


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    md.add_render_rule("table_open", lambda *a: '<div class="table"><table>\n')
    md.add_render_rule("table_close", lambda *a: "</table></div>\n")
    md.add_render_rule("text", _text_rule)
    for tag in ("th", "td"):
        md.add_render_rule(f"{tag}_open", _cell_open(tag))
        md.add_render_rule(f"{tag}_close", _cell_close(tag))
    return md


def _render_agent_text(md: MarkdownIt, text: str) -> str:
    """Agent 自带的标题下沉三级，不跟章节标题抢层级。"""
    tokens = md.parse(text)
    for token in tokens:
        if token.type in ("heading_open", "heading_close"):
            token.tag = f"h{min(int(token.tag[1]) + 3, 6)}"
    return md.renderer.render(tokens, md.options, {})


def _field(text: str, label: str) -> str:
    """从决策正文里取一个 `**Label**: value` 字段，容忍全角冒号与加粗。"""
    pattern = rf"^\s*\*\*{label}\*\*\s*[:\uff1a]\s*(.+)$"
    match = re.search(pattern, text or "", re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip().strip("*").strip() if match else ""


def _plain(text: str) -> str:
    """把单行 Markdown 收成可安全放入摘要卡的纯文本。"""
    text = re.sub(r"!?\[([^]]+)\]\([^)]+\)", r"\1", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[*_`~]", "", text).strip()


def _summary_text(md: MarkdownIt, text: str) -> str:
    """优先摘取结论段的正文；结论写成列表时退到首个列表项。

    这是原文摘录，不调用模型二次总结，避免头部汇总与正文结论互相打架。
    """
    tokens = md.parse(text or "")
    sections = []
    active = False
    level = 0
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            active = i + 1 < len(tokens) and "结论" in tokens[i + 1].content
            level = int(token.tag[1]) if active else 0
        if active and token.type == "inline":
            content = _plain(token.content)
            if len(content) < 32 or re.match(r"(?i)(rating|final transaction)", content):
                continue
            visible = i > 0 and tokens[i - 1].type == "paragraph_open" and not tokens[i - 1].hidden
            sections.append((visible, content))
    if not sections:
        for i, token in enumerate(tokens):
            if token.type != "inline":
                continue
            content = _plain(token.content)
            visible = i > 0 and tokens[i - 1].type == "paragraph_open" and not tokens[i - 1].hidden
            if visible and len(content) >= 32 and not re.match(r"(?i)(rating|final transaction)", content):
                sections.append((True, content))
    if not sections:
        return ""
    chosen = next((content for visible, content in sections if visible), sections[0][1])
    return _lead(chosen, 88)


def _angle_rating(text: str) -> str:
    for line in (text or "").splitlines():
        match = re.match(r"(?i)(?:Overall Sentiment|Rating)\s*[:：]\s*(.+)", _plain(line))
        if match:
            return match.group(1).split("(", 1)[0].strip()
    return ""


def _analysis_angles(md: MarkdownIt, parts) -> str:
    names = {
        "Market Analyst": "行情与技术面",
        "Fundamentals Analyst": "基本面与估值",
        "News Analyst": "新闻与宏观",
        "Sentiment Analyst": "市场与情绪",
    }
    cards = []
    for n, (_heading, _folder, written) in enumerate(parts, 1):
        for m, (agent, _filename, text) in enumerate(written, 1):
            if agent not in names:
                continue
            rating = _angle_rating(text)
            tone = _TONE.get(rating, "level")
            rating_zh = _RATING_ZH.get(rating, rating or "详见正文")
            excerpt = _summary_text(md, text) or "该角度已完成分析，请查看完整报告。"
            cards.append(
                f'<a class="angle" href="#s{n}-{m}" aria-label="查看{names[agent]}完整分析">'
                f'<div class="angle-head"><h3 class="angle-title">{names[agent]}</h3>'
                f'<span class="angle-rating {tone}">{escape(rating_zh)}</span></div>'
                f'<p class="angle-text">{escape(excerpt)}</p>'
                f'<span class="angle-more">查看详情</span></a>'
            )
    if not cards:
        return ""
    return (
        '<section class="angles" aria-labelledby="angles-title">'
        '<div class="angles-head"><h2 id="angles-title">综合汇总</h2>'
        f'<span>{len(cards)} 个独立视角</span></div>'
        f'<div class="angles-grid">{"".join(cards)}</div></section>'
    )


def _lead(text: str, limit: int = 120) -> str:
    """摘要首句：优先在句末标点收口，其次在分句处加省略号，绝不切半句。"""
    text = (text or "").strip()
    if not text or len(text) <= limit:
        return text
    head = text[:limit]
    floor = limit * 0.45
    for mark in ("。", "！", "？"):
        cut = head.rfind(mark)
        if cut >= floor:
            return text[: cut + 1]
    for mark in ("；", "，"):
        cut = head.rfind(mark)
        if cut >= floor:
            return text[:cut] + "…"
    return head + "…"


def _sentences(text: str) -> list[str]:
    """按中文句末标点拆句；小数点与英文缩写不参与拆分。"""
    return [s.strip() for s in re.split(r"(?<=[。！？])", _plain(text)) if len(s.strip()) >= 20]


def _best_sentence(sentences, keywords, exclude="") -> str:
    """选包含最多指定证据词的原文句；同分取前，避免模型式自由改写。"""
    candidates = []
    for index, sentence in enumerate(sentences):
        if sentence == exclude:
            continue
        score = sum(sentence.count(word) for word in keywords)
        if score:
            candidates.append((score, -index, sentence))
    return max(candidates)[2] if candidates else ""


def _decision_points(parts, decision: str, rating: str, rating_zh: str) -> str:
    """在决策卡内回答三件事：依据是什么、分歧在哪里、最大的风险是什么。"""
    thesis = _field(decision, "Investment Thesis") or _field(decision, "Rationale")
    thesis_sentences = _sentences(thesis)
    tone = _TONE.get(rating, "level")
    evidence_words = {
        "up": ["多头", "增长", "改善", "支撑", "上行", "低估", "增持", "买入", "优势"],
        "down": ["空头", "风险收益", "减持", "卖出", "下行", "不佳", "偏弱", "谨慎", "尚未", "死叉", "高估"],
        "level": ["中性", "持有", "平衡", "等待", "分歧", "震荡"],
    }[tone]
    core = _best_sentence(thesis_sentences, evidence_words + ["但", "综合看"])
    if not core and thesis_sentences:
        core = thesis_sentences[0]
    # 单独放进卡片时删去句首连接词，避免读者找不到被转折的前句。
    core_source = core
    core = re.sub(r"^(?:但|然而|不过)", "", core)

    risk_texts = [thesis]
    for heading, _folder, written in parts:
        if "Risk Management" in heading:
            risk_texts.extend(text for _agent, _filename, text in written)
    risk_sentences = [s for text in risk_texts for s in _sentences(text)]
    risk = _best_sentence(
        risk_sentences,
        ["风险", "止损", "跌破", "下行", "不确定", "缺失", "尚未", "未见", "回撤", "跳空"],
        exclude=core_source,
    )

    names = {
        "Market Analyst": "行情", "Fundamentals Analyst": "基本面",
        "News Analyst": "新闻宏观", "Sentiment Analyst": "情绪",
    }
    views = []
    for _heading, _folder, written in parts:
        for agent, _filename, text in written:
            if agent in names:
                view_rating = _angle_rating(text)
                if view_rating:
                    views.append((names[agent], _RATING_ZH.get(view_rating, view_rating), _TONE.get(view_rating, "level")))
    disagreement = ""
    opposite = "down" if tone == "up" else "up" if tone == "down" else ""
    conflicting = [f"{name}{view_rating}" for name, view_rating, view_tone in views if view_tone == opposite]
    if conflicting and rating:
        disagreement = f"{'、'.join(conflicting)}，最终结论为{rating_zh}，存在方向分歧。"
    elif {"up", "down"}.issubset({view_tone for _n, _r, view_tone in views}):
        disagreement = f"分析师视角存在多空分歧，最终结论为{rating_zh}。" if rating else "分析师视角存在多空分歧。"
    elif len(views) >= 2 and rating:
        disagreement = "各视角与最终结论未见明显方向冲突。"

    rows = [("核心依据", core), ("主要分歧", disagreement), ("首要风险", risk)]
    visible = []
    for label, value in rows:
        value = _lead(_localize_text(value), 76)
        if value:
            visible.append(f'<div class="verdict-point"><dt>{label}</dt><dd>{escape(value)}</dd></div>')
    return f'<dl class="verdict-points">{"".join(visible)}</dl>' if visible else ""


# 模型写的时间窗口有英文有中文，统一成本地读法。
_UNIT_ZH = {
    "day": "天", "days": "天", "week": "周", "weeks": "周",
    "month": "个月", "months": "个月", "quarter": "个季度", "quarters": "个季度",
    "year": "年", "years": "年",
}


def _localize_span(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    unit = r"([A-Za-z]+)"
    pair = re.fullmatch(rf"(\d+)\s*[-–—~至]\s*(\d+)\s*{unit}", value)
    if pair and pair.group(3).lower() in _UNIT_ZH:
        return f"{pair.group(1)}–{pair.group(2)} {_UNIT_ZH[pair.group(3).lower()]}"
    single = re.fullmatch(rf"(\d+)\s*{unit}", value)
    if single and single.group(2).lower() in _UNIT_ZH:
        return f"{single.group(1)} {_UNIT_ZH[single.group(2).lower()]}"
    return value


def _market_label(ticker: str) -> str:
    upper = (ticker or "").upper()
    for suffix, name in _MARKET.items():
        if upper.endswith(suffix):
            return name
    return "美股"


def _fields(values) -> str:
    return " ".join(f'<span class="field">{escape(str(v))}</span>' for v in values)


def _chain(vendors: str) -> str:
    names = [v.strip() for v in str(vendors).split(",") if v.strip()]
    return ' <span class="then">→</span> '.join(_fields([n]) for n in names)


def _group(name: str, values_html: str) -> str:
    return f'<span class="group"><span class="name">{escape(name)}</span>{values_html}</span>'


def _run_details(final_state: dict, settings: dict | None) -> list[tuple[str, str]]:
    details = [("生成时间", escape(datetime.now().strftime("%Y-%m-%d %H:%M")))]
    if settings:
        s = settings.get
        for tier, label in (("deep", "深度模型"), ("quick", "快速模型")):
            provider = s(f"{tier}_think_provider") or s("llm_provider", "?")
            details.append((label, _fields([provider, s(f"{tier}_think_llm", "?")])))
        details.append(("分析师", _fields(analyst_names(s("analysts"))) or "—"))
        details.append(("辩论轮数", " ".join((
            _group("研究", _fields([s("max_debate_rounds", "?")])),
            _group("风控", _fields([s("max_risk_discuss_rounds", "?")])),
        ))))
        vendors = {**(s("data_vendors") or {}), **(s("tool_vendors") or {})}
        if vendors:
            details.append(("数据源", " ".join(
                _group(_CATEGORY_ZH.get(k, k), _chain(v)) for k, v in vendors.items()
            )))
        details.append(("版本", escape(f"TradingAgents {s('version', '?')}")))
    if final_state.get("memory_note"):
        details.append(("记忆日志", escape(final_state["memory_note"])))
    if settings and settings.get("_reconstructed"):
        details.append(("说明", '<span class="hint">重排版时按当前配置重建，可能与该次运行的实际参数不同</span>'))
    return details


def _toc(parts) -> str:
    items = []
    for n, (heading, _folder, written) in enumerate(parts, 1):
        numeral, _, name = heading.partition(". ")
        title = _SECTION_ZH.get(name.strip(), name.strip())
        children = "".join(
            f'<li><a href="#s{n}-{m}">{escape(_AGENT_ZH.get(agent, agent))}</a></li>'
            for m, (agent, _f, _t) in enumerate(written, 1)
        )
        items.append(
            f'<li><a href="#s{n}"><span class="num">{int(numeral) if numeral.isdigit() else n:02d}</span>'
            f"{escape(title)}</a><ul>{children}</ul></li>"
        )
    return f'<ol>{"".join(items)}</ol>'


def render_report(ticker: str, final_state: dict, settings: dict | None, parts) -> str:
    """一页式报告：首屏决策卡 + 中文排版正文 + 常驻目录。"""
    md = _markdown()
    decision = final_state.get("final_trade_decision") or ""
    rating = run_rating(final_state) if decision else ""
    tone = _TONE.get(rating, "level")
    rating_zh = _RATING_ZH.get(rating, "待复核" if rating else "未评级")
    date = final_state.get("trade_date", "")

    target = _field(decision, "Price Target")
    horizon = _localize_span(_field(decision, "Time Horizon"))
    summary = _field(decision, "Executive Summary")
    if target.strip().lower() in ("not provided", "n/a", "na", "—", "-", "none"):
        target = ""
    if horizon.strip().lower() in ("not provided", "n/a", "na", "—", "-", "none"):
        horizon = ""

    # 「查看完整决策」指向组合经理那一章，不假定它一定排在最后。
    decision_anchor = f"#s{len(parts)}"
    for n, (heading, _f, _w) in enumerate(parts, 1):
        if "Portfolio Manager" in heading:
            decision_anchor = f"#s{n}"
            break

    metrics = [
        ("目标价", target or "未给出"),
        ("时间窗口", horizon or "未给出"),
        ("分析日期", date or "—"),
    ]
    metrics_html = "".join(
        f"<div><dt>{label}</dt><dd>{escape(value)}</dd></div>" for label, value in metrics
    )

    # 首屏已有 ticker 与中文评级，摘要不再重复「对 XXX 给予 Underweight」。
    lead_source = re.sub(
        r"^\s*对\s*\S+\s*给予\s*[A-Za-z]+\s*[:：]\s*", "", summary, count=1, flags=re.IGNORECASE
    )
    lead = _lead(lead_source, 92)
    lead_html = f'<p class="verdict-lead">{escape(lead)}</p>' if lead else ""
    decision_link = (
        f'<a class="verdict-more" href="{decision_anchor}">完整决策 →</a>' if decision else ""
    )
    points_html = _decision_points(parts, decision, rating, rating_zh) if decision else ""

    angles_html = _analysis_angles(md, parts)

    details = "".join(
        f'<div class="{"note" if label == "记忆日志" else "detail"}">'
        f"<dt>{escape(label)}</dt><dd>{value}</dd></div>"
        for label, value in _run_details(final_state, settings)
    )

    body = []
    for n, (heading, _folder, written) in enumerate(parts, 1):
        numeral, _, name = heading.partition(". ")
        title = _SECTION_ZH.get(name.strip(), name.strip())
        num = f"{int(numeral):02d}" if numeral.isdigit() else f"{n:02d}"
        body.append(
            f'<section id="s{n}"><h2><span class="numeral">{num}</span>'
            f'<span>{escape(title)}</span></h2>'
        )
        for m, (agent, _filename, text) in enumerate(written, 1):
            label = _AGENT_ZH.get(agent, agent)
            role = _AGENT_ROLE.get(agent)
            chip = f'<span class="chip {role[1]}">{escape(role[0])}</span>' if role else ""
            body.append(f'<h3 id="s{n}-{m}">{chip}{escape(label)}</h3>\n{_render_agent_text(md, text)}')
        body.append("</section>")

    toc = _toc(parts)
    logo = f'<a class="brand" href="{_COMPANY}" rel="noopener" aria-label="Tauric Research">{_LOGO}</a>' if _LOGO else ""
    page_title = " ".join(x for x in (ticker, date, rating_zh) if x)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{escape(page_title)} · 分析报告</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page" id="top">
<div class="grid">

<header class="hero">
  <p class="eyebrow">{logo}<span class="divider"></span><span>TradingAgents 深度分析报告</span></p>
  <h1><span class="ticker">{escape(ticker)}</span><span class="market">{escape(_market_label(ticker))}</span></h1>
  <p class="sub">分析日期 <span class="nowrap">{escape(date) if date else "—"}</span>　·　多智能体研判</p>

  <section class="verdict {tone}" aria-label="最终结论">
    <div class="verdict-head">
      <p class="verdict-kicker">最终结论</p>
      {decision_link}
    </div>
    <p class="verdict-rating">{escape(rating_zh)}</p>
    {lead_html}
    {points_html}
    <dl class="verdict-metrics">{metrics_html}</dl>
  </section>
  {angles_html}

  <details class="run">
    <summary>运行参数</summary>
    <dl>{details}</dl>
  </details>
</header>

<nav class="toc" aria-label="目录">
  <p class="toc-title">目录</p>
  {toc}
</nav>

<details class="toc-mobile">
  <summary>目录<span>{len(parts)} 个章节</span></summary>
  <nav aria-label="目录">{toc}</nav>
</details>

<main>
{"".join(body)}
</main>

</div>

<footer class="credit">
  本报告由 <a href="{_PROJECT}" rel="noopener">TradingAgents</a> 生成　·　
  框架 <a class="brand" href="{_ORG}" rel="noopener">Tauric Research</a>　·　
  本地适配：A 股 / 港股数据源（akshare）
</footer>
</div>
<a class="to-top" href="#top">回到顶部</a>
</body>
</html>
"""


def apply() -> None:
    """把上游的 render_report 换成这一版。"""
    _upstream.render_report = render_report
