#!/usr/bin/env python3
"""TradingAgents 报告入口：手机 / 电脑输入 A 股、港股代码并生成报告。

默认监听局域网，报告与分析接口均需要密码登录；复用现有 run.sh，分析逻辑不变。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

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
_SESSION_COOKIE = "ta_web_session"


def _access_password() -> str:
    """优先读环境变量；否则生成并持久保存仅本机可读的访问密码。"""
    configured = os.environ.get("TA_WEB_PASSWORD", "").strip()
    if configured:
        return configured
    path = RESULTS_DIR / "web" / "access_token"
    if path.exists():
        saved = path.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    path.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(9)
    path.write_text(generated + "\n", encoding="utf-8")
    path.chmod(0o600)
    return generated


ACCESS_PASSWORD = _access_password()


def _session_value() -> str:
    return hmac.new(
        ACCESS_PASSWORD.encode("utf-8"), b"TradingAgents report session", hashlib.sha256
    ).hexdigest()


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

_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · TradingAgents</title>
<style>
:root { color-scheme: light dark; font-family: system-ui,-apple-system,"PingFang SC",sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px;
  background: #f5f3ee; color: #191d21; }
main { width: min(100%, 380px); padding: 28px; border: 1px solid #e6e3dc; border-radius: 14px;
  background: #fff; box-shadow: 0 8px 30px rgb(0 0 0 / .05); }
h1 { margin: 0; font-size: 24px; line-height: 1.3; }
p { margin: 10px 0 24px; color: #5f6870; font-size: 14px; line-height: 1.7; }
label { display: block; margin-bottom: 8px; font-size: 13px; font-weight: 600; }
input { width: 100%; height: 46px; padding: 0 12px; border: 1px solid #d6d2c8; border-radius: 7px;
  background: #fff; color: #191d21; font: 16px ui-monospace,monospace; }
button { width: 100%; height: 46px; margin-top: 12px; border: 0; border-radius: 7px;
  background: #1a5cae; color: #fff; font-size: 15px; font-weight: 600; }
.error { color: #c0362a; margin: 12px 0 0; font-size: 13px; }
.hint { margin: 18px 0 0; text-align: center; font-size: 12px; }
@media (prefers-color-scheme: dark) {
  body { background: #12171b; color: #e6eaee; }
  main { background: #171e23; border-color: #28323a; }
  input { background: #12171b; color: #e6eaee; border-color: #3a464f; }
  p { color: #97a3ad; } .error { color: #ef6d5e; }
}
</style></head><body><main>
<h1>TradingAgents</h1><p>输入访问密码，查看报告并发起个股分析。</p>
<form method="post" action="/login">
<input type="hidden" name="next" value="__NEXT__">
<label for="password">访问密码</label>
<input id="password" name="password" type="password" required autofocus autocomplete="current-password">
<button type="submit">登录</button>__ERROR__
</form><p class="hint">仅限你的设备与同一网络使用</p>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsLocal/1.0"

    def _authenticated(self) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get(_SESSION_COOKIE)
            return bool(value and hmac.compare_digest(value.value, _session_value()))
        except Exception:
            return False

    @staticmethod
    def _safe_next(raw: str) -> str:
        return raw if raw.startswith("/") and not raw.startswith("//") else "/"

    def _login(self, next_path: str = "/", error: str = "", status: int = 200) -> None:
        next_path = self._safe_next(next_path)
        error_html = f'<p class="error">{escape(error)}</p>' if error else ""
        data = _LOGIN_PAGE.replace("__NEXT__", escape(next_path, quote=True)).replace("__ERROR__", error_html).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _require_auth(self, api: bool = False) -> bool:
        if self._authenticated():
            return True
        if api:
            self._json({"error": "登录已失效，请重新登录"}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", f"/login?next={quote(self.path)}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        return False

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
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._json({"status": "ok"})
            return
        if path == "/login":
            next_path = parse_qs(parsed.query).get("next", ["/"])[0]
            self._login(next_path)
            return
        if not self._require_auth(path.startswith("/api/")):
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
        path = urlparse(self.path).path
        if path == "/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 2048:
                    raise ValueError
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._login(error="请求格式无效", status=400)
                return
            password = form.get("password", [""])[0]
            next_path = self._safe_next(form.get("next", ["/"])[0])
            if not hmac.compare_digest(password, ACCESS_PASSWORD):
                self._login(next_path, "密码不正确，请重试", 403)
                return
            self.send_response(303)
            self.send_header("Location", next_path)
            self.send_header(
                "Set-Cookie",
                f"{_SESSION_COOKIE}={_session_value()}; Path=/; Max-Age=604800; HttpOnly; SameSite=Strict",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if not self._require_auth(path.startswith("/api/")):
            return
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
    print(f"访问密码：{ACCESS_PASSWORD}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
