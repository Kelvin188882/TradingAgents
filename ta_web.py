#!/usr/bin/env python3
"""TradingAgents 报告入口：手机 / 电脑输入 A 股、港股代码并生成报告。

默认监听局域网，报告与分析接口均免密码访问；复用现有 run.sh，分析逻辑不变。
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def _results_dir() -> Path:
    """读取项目 .env 中的结果目录，不加载或打印任何凭据。"""
    configured = os.environ.get("TRADINGAGENTS_RESULTS_DIR")
    if not configured:
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("TRADINGAGENTS_RESULTS_DIR="):
                    configured = line.split("=", 1)[1].strip().strip("\"'")
                    break
    path = Path(configured) if configured else ROOT / "data" / "logs"
    return path.expanduser().resolve() if path.is_absolute() else (ROOT / path).resolve()


RESULTS_DIR = _results_dir()
REPORTS_DIR = RESULTS_DIR / "reports"
_TICKER_RE = re.compile(r"^(\d{1,6})(?:\.(SS|SH|SZ|BJ|HK))?$")


def _lan_ip() -> str | None:
    """取得当前出口网卡地址；UDP 探测不会发送实际数据。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        address = sock.getsockname()[0]
        return None if address.startswith("127.") else address
    except OSError:
        return None
    finally:
        sock.close()


def normalize_ticker(raw: str) -> str:
    """接受常见 A 股简写与港股代码，返回分析链使用的标准代码。"""
    value = str(raw or "").strip().upper()
    match = _TICKER_RE.fullmatch(value)
    if not match:
        raise ValueError("请输入 A 股代码（如 600519）或港股代码（如 0700.HK）")
    digits, suffix = match.groups()
    if suffix == "HK" or (not suffix and len(digits) <= 5 and digits.startswith("0")):
        if len(digits) > 5:
            raise ValueError("港股代码应为 1–5 位数字，例如 0700.HK")
        return f"{str(int(digits)).zfill(4)}.HK"
    if len(digits) != 6:
        raise ValueError("A 股代码应为 6 位数字，例如 600519 或 000001.SZ")
    if suffix in {"SS", "SH"}:
        return f"{digits}.SS"
    if suffix:
        return f"{digits}.{suffix}"
    if digits[0] in {"6", "9"}:
        return f"{digits}.SS"
    if digits[0] in {"4", "8"}:
        return f"{digits}.BJ"
    return f"{digits}.SZ"


_NAME_CACHE: dict[str, tuple[float, str | None]] = {}
_NAME_LOCK = threading.Lock()


def resolve_stock_name(raw: str) -> tuple[str, str | None]:
    """用新浪实时行情返回的名称识别 A 股 / 港股；失败不影响按代码分析。"""
    ticker = normalize_ticker(raw)
    now = time.time()
    with _NAME_LOCK:
        cached = _NAME_CACHE.get(ticker)
    if cached and cached[0] > now:
        return ticker, cached[1]

    digits, market = ticker.split(".")
    prefix = {"SS": "sh", "SZ": "sz", "BJ": "bj"}.get(market, "")
    symbol = f"hk{digits.zfill(5)}" if market == "HK" else f"{prefix}{digits}"
    name = None
    try:
        request = Request(
            f"https://hq.sinajs.cn/list={symbol}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
        )
        with urlopen(request, timeout=4) as response:
            text = response.read().decode("gb18030")
        match = re.search(r'=\"([^\"]*)\"', text)
        fields = match.group(1).split(",") if match else []
        candidate = (fields[1] if market == "HK" and len(fields) > 1 else fields[0] if fields else "").strip()
        if candidate and len(candidate) <= 80 and not any(ord(char) < 32 for char in candidate):
            name = candidate
    except (OSError, UnicodeError, ValueError, IndexError):
        pass
    with _NAME_LOCK:
        # 名称缓存一天；识别失败只缓存五分钟，避免源站抖动造成长期缺失。
        _NAME_CACHE[ticker] = (now + (86400 if name else 300), name)
    return ticker, name


_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}


def search_symbols(raw: str, limit: int = 8) -> list[dict]:
    """按名称或代码搜索 A 股 / 港股，过滤基金、期货、ADR 等不可分析标的。"""
    query = str(raw or "").strip()
    if not query or len(query) > 40 or any(ord(char) < 32 for char in query):
        raise ValueError("请输入股票名称或代码")
    now = time.time()
    with _NAME_LOCK:
        cached = _SEARCH_CACHE.get(query.casefold())
    if cached and cached[0] > now:
        return cached[1][:limit]

    params = urlencode({
        "input": query, "type": 14,
        "token": "D43BF722C8E33BD8A4A3B49E7C4A9C1C", "count": 20,
    })
    request = Request(
        f"https://searchapi.eastmoney.com/api/suggest/get?{params}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
        records = (payload.get("QuotationCodeTable") or {}).get("Data") or []
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("名称搜索暂时不可用，请改用股票代码") from exc

    markets = {"沪A": "SS", "深A": "SZ", "京A": "BJ"}
    results = []
    seen = set()
    for item in records:
        if not isinstance(item, dict):
            continue
        classify = item.get("Classify")
        security_type = item.get("SecurityTypeName", "")
        code = str(item.get("Code", "")).strip()
        name = str(item.get("Name", "")).strip()
        if not code.isdigit() or not name or len(name) > 80:
            continue
        try:
            if classify == "AStock" and security_type in markets:
                ticker = normalize_ticker(f"{code}.{markets[security_type]}")
            elif classify == "HK" and security_type == "港股" and str(item.get("TypeUS")) == "3":
                ticker = normalize_ticker(f"{code}.HK")
            else:
                continue
        except ValueError:
            continue
        if ticker in seen:
            continue
        seen.add(ticker)
        results.append({"ticker": ticker, "name": name, "market": security_type})
    results.sort(key=lambda item: (
        item["name"].casefold() != query.casefold(),
        item["ticker"].split(".", 1)[0].lstrip("0") != query.lstrip("0"),
    ))
    with _NAME_LOCK:
        _SEARCH_CACHE[query.casefold()] = (now + 300, results)
    return results[:limit]


def list_reports(ticker: str | None = None) -> list[Path]:
    if not REPORTS_DIR.exists():
        return []
    prefix = f"{ticker}_" if ticker else ""
    reports = [
        path for path in REPORTS_DIR.iterdir()
        if path.is_dir() and path.name.startswith(prefix)
        and (path / "complete_report.html").is_file()
    ]
    return sorted(reports, key=lambda path: path.name, reverse=True)


def report_url(report_dir: Path) -> str:
    return f"/reports/{quote(report_dir.name)}/complete_report.html"


def _report_dir(folder: str) -> Path | None:
    """仅允许现有报告目录名，阻止下载路径逃逸。"""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", folder) or folder in {".", ".."}:
        return None
    report = REPORTS_DIR / folder
    if not (report / "complete_report.html").is_file():
        return None
    resolved = report.resolve()
    return resolved if resolved.is_relative_to(REPORTS_DIR) else None


def _report_metadata(report: Path) -> dict:
    match = re.fullmatch(r"([A-Za-z0-9.]+)_(\d{8}_\d{6})", report.name)
    ticker, stamp = match.groups() if match else (report.name, "")
    try:
        analyzed_at = datetime.strptime(stamp, "%Y%m%d_%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        analyzed_at = stamp
    try:
        source = (report / "complete_report.html").read_text(encoding="utf-8")
        found = re.search(r'<span class="stock-name">(.*?)</span>', source, re.DOTALL)
        name = html.unescape(re.sub(r"<[^>]+>", "", found.group(1))).strip() if found else ""
    except (OSError, UnicodeError):
        name = ""
    return {"ticker": ticker, "name": name or ticker, "analyzed_at": analyzed_at}


def _archive_label(report: Path, metadata: dict) -> str:
    label = f"{metadata['name']}_{report.name}"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label)


def _history_index(reports: list[Path]) -> tuple[bytes, bytes, list[tuple[Path, dict, str]]]:
    """生成 ZIP 内的离线 HTML 目录和 Excel 可读的 UTF-8 CSV 目录。"""
    entries = [(path, _report_metadata(path)) for path in reports]
    entries = [(path, meta, _archive_label(path, meta)) for path, meta in entries]
    rows = []
    cards = []
    for index, (_path, meta, label) in enumerate(entries, 1):
        relative = f"reports/{quote(label)}/complete_report.html"
        rows.append([index, meta["name"], meta["ticker"], meta["analyzed_at"], relative])
        cards.append(
            "<tr>"
            f"<td>{index}</td><td>{html.escape(meta['name'])}</td>"
            f"<td>{html.escape(meta['ticker'])}</td><td>{html.escape(meta['analyzed_at'])}</td>"
            f"<td><a href=\"{relative}\">打开 HTML 报告</a></td></tr>"
        )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["序号", "股票名称", "个股代码", "分析时间", "报告文件"])
    writer.writerows(rows)
    catalog_csv = ("\ufeff" + output.getvalue()).encode("utf-8")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    catalog_html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradingAgents 历史报告目录</title><style>
body{{max-width:1100px;margin:0 auto;padding:40px 24px;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#252525;background:#f7f7f5}}
h1{{margin:0;font-size:28px}}p{{color:#666}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:12px;border-bottom:1px solid #e5e5e0;text-align:left}}a{{color:#175cd3}}@media(max-width:650px){{th:nth-child(1),td:nth-child(1){{display:none}}th,td{{padding:9px 6px;font-size:12px}}}}
</style></head><body><h1>历史分析报告</h1><p>共 {len(entries)} 份 · 导出于 {generated_at} · 报告可完全离线打开</p>
<table><thead><tr><th>序号</th><th>股票名称</th><th>个股代码</th><th>分析时间</th><th>报告</th></tr></thead>
<tbody>{''.join(cards)}</tbody></table></body></html>"""
    return catalog_html.encode("utf-8"), catalog_csv, entries


def build_history_archive() -> tuple[tempfile.SpooledTemporaryFile, int, int]:
    """在临时文件中打包，避免历史报告增多后占用大量内存。"""
    reports = list_reports()
    if not reports:
        raise ValueError("暂无可导出的历史报告")
    catalog_html, catalog_csv, entries = _history_index(reports)
    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("index.html", catalog_html)
            bundle.writestr("报告目录.csv", catalog_csv)
            for report, _meta, label in entries:
                for filename in ("complete_report.html", "complete_report.md", "data_provenance.json"):
                    source = report / filename
                    if source.is_file():
                        bundle.write(source, f"reports/{label}/{filename}")
        size = archive.seek(0, os.SEEK_END)
        archive.seek(0)
        return archive, size, len(entries)
    except Exception:
        archive.close()
        raise


def run_analysis(ticker: str, job_id: str) -> Path:
    """同步执行一次分析；只返回本次新生成的报告，避免误开旧文件。"""
    started = time.time()
    log_dir = RESULTS_DIR / "web"
    log_dir.mkdir(parents=True, exist_ok=True)
    command = [str(ROOT / "run.sh"), "--ticker", ticker, "--save", "--html", "--no-show"]
    with (log_dir / f"{job_id}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        return_code = process.wait()
    if return_code != 0:
        raise RuntimeError("分析未完成，请检查个股代码、数据源或模型配置后重试")
    fresh = [
        path for path in list_reports(ticker)
        if (path / "complete_report.html").stat().st_mtime >= started - 2
    ]
    if not fresh:
        raise RuntimeError("分析已结束，但没有找到新生成的报告")
    return fresh[0]


class JobBusy(RuntimeError):
    def __init__(self, job: dict):
        super().__init__("已有分析正在运行")
        self.job = job


class JobManager:
    """单任务执行，避免重复提交造成模型费用和源站并发压力。"""

    def __init__(self, runner=run_analysis):
        self.runner = runner
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def _public(self, job: dict) -> dict:
        result = {key: value for key, value in job.items() if not key.startswith("_")}
        if job["status"] == "running":
            result["elapsed_seconds"] = max(0, int(time.time() - job["_started"]))
        return result

    def active(self) -> dict | None:
        with self.lock:
            active = [job for job in self.jobs.values() if job["status"] == "running"]
            return self._public(active[-1]) if active else None

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self._public(job) if job else None

    def start(self, ticker: str) -> dict:
        with self.lock:
            active = [job for job in self.jobs.values() if job["status"] == "running"]
            if active:
                raise JobBusy(self._public(active[-1]))
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id, "ticker": ticker, "status": "running",
                "message": "分析正在进行，通常需要数分钟", "report_url": None,
                "_started": time.time(),
            }
            self.jobs[job_id] = job
            # 只保留最近 20 条本地状态。
            for old_id in list(self.jobs)[:-20]:
                del self.jobs[old_id]
        threading.Thread(target=self._execute, args=(job_id,), daemon=True).start()
        return self._public(job)

    def _execute(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
        try:
            report = self.runner(job["ticker"], job_id)
            with self.lock:
                job.update(
                    status="completed", message="报告已生成",
                    report_url=report_url(report), completed_at=datetime.now().isoformat(timespec="seconds"),
                )
        except Exception as exc:
            with self.lock:
                job.update(status="failed", message=str(exc), report_url=None)


MANAGER = JobManager()


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsLocal/1.0"

    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _report(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _download(self, data: bytes, filename: str, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"report.html\"; filename*=UTF-8''{quote(filename, safe='')}",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _export_history(self) -> None:
        try:
            archive, size, count = build_history_archive()
        except ValueError as exc:
            self._json({"error": str(exc)}, 404)
            return
        except OSError:
            self._json({"error": "历史报告打包失败，请稍后重试"}, 500)
            return
        filename = f"TradingAgents_全部历史报告_{datetime.now():%Y%m%d_%H%M%S}.zip"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("X-Report-Count", str(count))
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"reports.zip\"; filename*=UTF-8''{quote(filename, safe='')}",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            shutil.copyfileobj(archive, self.wfile)
        finally:
            archive.close()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json({"status": "ok"})
            return
        if path == "/login":
            # 兼容已收藏的旧登录地址。
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if path == "/api/symbols":
            raw = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            try:
                results = search_symbols(raw)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 503)
                return
            self._json({"results": results})
            return
        if path == "/api/stock-name":
            raw = parse_qs(urlparse(self.path).query).get("ticker", [""])[0]
            try:
                ticker, name = resolve_stock_name(raw)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json({"ticker": ticker, "name": name})
            return
        if path == "/api/active":
            self._json({"job": MANAGER.active()})
            return
        if path == "/api/export/all":
            self._export_history()
            return
        if path.startswith("/api/jobs/"):
            job = MANAGER.get(path.rsplit("/", 1)[-1])
            self._json({"job": job} if job else {"error": "任务不存在"}, 200 if job else 404)
            return
        if path == "/":
            reports = list_reports()
            if reports:
                self.send_response(302)
                self.send_header("Location", report_url(reports[0]))
                self.end_headers()
            else:
                self._json({"message": "暂无报告，请先通过命令行生成一份报告"}, 404)
            return
        match = re.fullmatch(r"/reports/([^/]+)/download", path)
        if match:
            report = _report_dir(unquote(match.group(1)))
            if report:
                meta = _report_metadata(report)
                filename = f"{meta['name']}_{meta['ticker']}_{report.name.rsplit('_', 2)[-2]}_分析报告.html"
                filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
                try:
                    data = (report / "complete_report.html").read_bytes()
                except OSError:
                    self._json({"error": "报告读取失败，请稍后重试"}, 500)
                    return
                self._download(data, filename, "text/html; charset=utf-8")
                return
        match = re.fullmatch(r"/reports/([^/]+)/complete_report\.html", path)
        if match:
            folder = unquote(match.group(1))
            if re.fullmatch(r"[A-Za-z0-9._-]+", folder) and folder not in {".", ".."}:
                report = REPORTS_DIR / folder / "complete_report.html"
                if report.is_file() and report.resolve().is_relative_to(REPORTS_DIR):
                    self._report(report)
                    return
        self._json({"error": "页面不存在"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path != "/api/analyze":
            self._json({"error": "接口不存在"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2048:
                raise ValueError("请求内容无效")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            ticker = normalize_ticker(payload.get("ticker", ""))
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json({"error": str(exc) or "请求格式无效"}, 400)
            return
        try:
            job = MANAGER.start(ticker)
            self._json({"job": job}, 202)
        except JobBusy as exc:
            self._json({"error": str(exc), "job": exc.job}, 409)

    def log_message(self, fmt: str, *args) -> None:
        # 不记录请求体；访问日志只保留本机 stdout。
        super().log_message(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingAgents 手机 / 电脑报告页面")
    parser.add_argument("--host", default="0.0.0.0", help="默认 0.0.0.0，允许同一 Wi-Fi 的手机访问")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口应在 1–65535 之间")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"电脑入口：http://127.0.0.1:{args.port}", flush=True)
    lan_ip = _lan_ip()
    if lan_ip:
        print(f"手机入口：http://{lan_ip}:{args.port}（需连接同一 Wi-Fi）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
