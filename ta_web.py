#!/usr/bin/env python3
"""TradingAgents 本地报告入口：输入 A 股 / 港股代码并生成新报告。

仅监听 127.0.0.1，不暴露到局域网；复用现有 run.sh，分析逻辑不变。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

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
        return f"{digits.zfill(4)}.HK"
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

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json({"status": "ok"})
            return
        if path == "/api/active":
            self._json({"job": MANAGER.active()})
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
        if urlparse(self.path).path != "/api/analyze":
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
    parser = argparse.ArgumentParser(description="TradingAgents 本地报告页面")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口应在 1–65535 之间")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"TradingAgents 报告入口：http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
