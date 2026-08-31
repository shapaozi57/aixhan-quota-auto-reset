from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_NAME = "AixHan Quota Auto Reset"
APP_VERSION = "2026-08-31.hidden-launcher-icon"
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
ASSETS_DIR = ROOT / "assets"
CONFIG_PATH = ROOT / "config.json"
RESET_HISTORY_PATH = LOG_DIR / "reset_history.jsonl"
APP_LOG_PATH = LOG_DIR / "app.log"
APP_ICON_PATH = ASSETS_DIR / "aixhan_quota.ico"

QUOTA_PATTERNS = [
    re.compile(p, re.I)
    for p in [
        r"额度不足",
        r"余额不足",
        r"今日额度.*(?:用完|不足|为\s*0)",
        r"剩余额度.*(?:0(?:\.0+)?|不足)",
        r"insufficient[_\s-]?quota",
        r"quota\s*(?:exceeded|exhausted|insufficient)",
        r"exceeded\s+your\s+current\s+quota",
        r"daily\s+limit\s+(?:exceeded|exhausted)",
        r"out\s+of\s+credits?",
        r"credit(?:s)?\s+(?:exhausted|insufficient)",
        r"billing\s+hard\s+limit",
        r"HTTP\s*402",
        r"status(?:_code)?[=:\s]+402",
        r"HTTP\s*429.*(?:quota|credit|limit)",
        r"status(?:_code)?[=:\s]+429.*(?:quota|credit|limit)",
    ]
]


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except Exception:
        return default


def mask_card_key(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    prefix = text[:4]
    hidden_len = max(len(text) - len(prefix), 4)
    return prefix + ("*" * hidden_len)


def is_masked_card_key(value: str, original: str) -> bool:
    value = str(value or "").strip()
    original = str(original or "").strip()
    if not value or not original:
        return False
    return value == mask_card_key(original) or (value.startswith(original[:4]) and set(value[4:] or "") <= {"*"})


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class Config:
    aixhan_url: str = "https://cdk.aixhan.com"
    aixhan_card_key: str = ""
    daily_limit_usd: float = 50.0
    reset_when_remaining_lte: float = 0.25
    poll_interval_seconds: float = 0.25
    reset_cooldown_seconds: int = 240
    daily_max_resets: int = 0  # 0 = 不限制
    codex_logs_sqlite: str = r"%USERPROFILE%\.codex\logs_2.sqlite"
    ccswitch_db: str = r"%USERPROFILE%\.cc-switch\cc-switch.db"
    codex_goals_sqlite: str = r"%USERPROFILE%\.codex\goals_1.sqlite"
    browser_profile_dir: str = str(DATA_DIR / "aixhan_browser_profile")
    browser_channel: str = "auto"  # auto, chrome, msedge, chromium
    headless: bool = False
    prewarm_browser_on_auto_start: bool = True
    trigger_on_ccswitch_402_error: bool = True
    trigger_on_codex_quota_error: bool = True
    trigger_on_ccswitch_remaining_zero: bool = False
    trigger_on_aixhan_remaining_zero: bool = False
    reset_after_codex_error_once: bool = True
    scan_recent_errors_on_start: bool = False
    recent_error_window_seconds: int = 120
    page_timeout_ms: int = 8000
    reset_verify_timeout_seconds: int = 18
    aixhan_page_check_interval_seconds: int = 15
    ccswitch_usage_refresh_seconds: int = 120
    card_action_delay_ms: int = 2500
    auto_continue_after_reset: bool = True
    auto_continue_all_402_threads: bool = True
    auto_resume_goal_after_reset: bool = True
    codex_continue_text: str = "continue"
    codex_continue_delay_seconds: float = 2.0
    codex_continue_window_keyword: str = "ChatGPT"
    codex_continue_click_bottom: bool = True
    codex_cli_path: str = ""

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            base = asdict(cls())
            base.update(raw)
            cfg = cls(**base)
        else:
            cfg = cls()
            cfg.save()
        return cfg

    def save(self) -> None:
        atomic_write_json(CONFIG_PATH, asdict(self))


class LogBus:
    def __init__(self) -> None:
        self.q: queue.Queue[tuple[str, str]] = queue.Queue()
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def log(self, msg: str, level: str = "INFO") -> None:
        line = f"[{now_text()}][{level}] {msg}"
        try:
            APP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with APP_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        self.q.put((level, line))


class CCSwitchReader:
    def __init__(self, cfg: Config, log: Callable[[str, str], None]) -> None:
        self.cfg = cfg
        self.log = log
        self.last_proxy_rowid = 0

    @property
    def db_path(self) -> Path:
        return expand_path(self.cfg.ccswitch_db)

    def _connect_ro(self) -> sqlite3.Connection:
        p = self.db_path
        # Do not use immutable=1 here: ccswitch/Codex keep hot WAL files and
        # immutable readers can miss fresh rows that are still in the WAL.
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=1.0)
        conn.execute("pragma query_only=1")
        conn.row_factory = sqlite3.Row
        return conn

    def get_current_provider_id(self) -> Optional[str]:
        p = self.db_path
        if not p.exists():
            return None
        try:
            with self._connect_ro() as conn:
                row = conn.execute("select value from settings where key='currentProviderCodex'").fetchone()
                if row and row[0]:
                    return str(row[0])
                row = conn.execute("select id from providers where app_type='codex' and is_current=1 limit 1").fetchone()
                return str(row[0]) if row else None
        except Exception:
            return None

    def get_usage_script_config(self) -> dict[str, Any]:
        p = self.db_path
        if not p.exists():
            return {}
        try:
            with self._connect_ro() as conn:
                provider_id = self.get_current_provider_id()
                if provider_id:
                    row = conn.execute("select meta from providers where id=? limit 1", (provider_id,)).fetchone()
                else:
                    row = conn.execute(
                        "select meta from providers where app_type='codex' and (id like 'aixhan%' or name like '%AixHan%') limit 1"
                    ).fetchone()
                meta = json.loads((row["meta"] if row else None) or "{}")
                usage_script = meta.get("usage_script") or {}
                return usage_script if isinstance(usage_script, dict) else {}
        except Exception as e:
            self.log(f"读取 ccswitch usage_script 配置失败: {e}", "WARN")
            return {}

    def get_public_usage(self) -> dict[str, Any]:
        """Read the same AixHan usage endpoint configured inside ccswitch.

        ccswitch's local proxy logs are request-cost records and can be stale or
        absent for the current day. The ccswitch UI's provider card uses
        providers.meta.usage_script, so mirror that path first.
        """
        result = {
            "ok": False,
            "source": "ccswitch_api",
            "used_usd": 0.0,
            "daily_limit_usd": self.cfg.daily_limit_usd,
            "remaining_usd": self.cfg.daily_limit_usd,
            "request_count": 0,
            "success_count": 0,
            "last_created_at": None,
            "provider_id": self.get_current_provider_id(),
            "note": "",
        }
        usage_script = self.get_usage_script_config()
        api_key = str(usage_script.get("apiKey") or self.cfg.aixhan_card_key or "").strip()
        base_url = str(usage_script.get("baseUrl") or self.cfg.aixhan_url or "https://cdk.aixhan.com").rstrip("/")
        if not api_key:
            result["note"] = "ccswitch usage_script 未配置 apiKey"
            return result
        url = f"{base_url}/api/public/usage/stats?key={urllib.parse.quote(api_key)}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 AixHan-Quota-Auto-Reset/1.0",
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": base_url + "/",
                },
            )
            with urllib.request.urlopen(req, timeout=max(3, int(getattr(self.cfg, "page_timeout_ms", 8000) / 1000))) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            data = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(data, dict):
                result["note"] = "usage API 返回结构异常"
                return result
            remaining = safe_float(data.get("remaining"), 0.0)
            daily_limit = safe_float(data.get("dailyQuota", data.get("quotaLimit", self.cfg.daily_limit_usd)), self.cfg.daily_limit_usd)
            used = safe_float(data.get("consumed", data.get("totalConsumed")), max(daily_limit - remaining, 0.0))
            requests = int(safe_float(data.get("requests"), 0.0))
            result.update(
                ok=True,
                used_usd=used,
                daily_limit_usd=daily_limit,
                remaining_usd=remaining,
                request_count=requests,
                success_count=requests,
                note=f"usage_script 实时读取 resetAt={data.get('resetAt', '--')}",
            )
            return result
        except Exception as e:
            result["note"] = f"usage API 读取失败，回退本地日志: {e}"
            return result

    def get_today_usage(self) -> dict[str, Any]:
        p = self.db_path
        live = self.get_public_usage()
        if live.get("ok"):
            return live
        result = {
            "ok": False,
            "source": "ccswitch",
            "used_usd": 0.0,
            "daily_limit_usd": self.cfg.daily_limit_usd,
            "remaining_usd": self.cfg.daily_limit_usd,
            "request_count": 0,
            "success_count": 0,
            "last_created_at": None,
            "provider_id": None,
            "note": "",
        }
        if not p.exists():
            result["note"] = f"ccswitch DB 不存在: {p}"
            return result
        try:
            with self._connect_ro() as conn:
                cur = conn.cursor()
                provider_id = self.get_current_provider_id()
                result["provider_id"] = provider_id
                max_row = cur.execute("select max(created_at) from proxy_request_logs").fetchone()
                max_ts = int(max_row[0] or 0)
                unit_ms = max_ts > 10**12
                start = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
                end = start + 86400
                if unit_ms:
                    start *= 1000
                    end *= 1000
                rows = cur.execute(
                    """
                    select count(*) n,
                           sum(case when status_code between 200 and 299 then 1 else 0 end) ok_n,
                           coalesce(sum(cast(total_cost_usd as real)), 0) cost,
                           max(created_at) last_ts
                    from proxy_request_logs
                    where app_type='codex' and created_at >= ? and created_at < ?
                    """,
                    (start, end),
                ).fetchone()
                used = safe_float(rows["cost"], 0.0) if rows else 0.0
                if used <= 0.0:
                    roll = cur.execute(
                        """
                        select coalesce(sum(cast(total_cost_usd as real)), 0) cost,
                               coalesce(sum(request_count), 0) n,
                               coalesce(sum(success_count), 0) ok_n
                        from usage_daily_rollups
                        where app_type='codex' and date=?
                        """,
                        (date.today().isoformat(),),
                    ).fetchone()
                    if roll:
                        used = safe_float(roll["cost"], 0.0)
                        if used > 0:
                            result["request_count"] = int(roll["n"] or 0)
                            result["success_count"] = int(roll["ok_n"] or 0)
                            result["note"] = "来自 usage_daily_rollups"
                if rows and result["request_count"] == 0:
                    result["request_count"] = int(rows["n"] or 0)
                    result["success_count"] = int(rows["ok_n"] or 0)
                    result["last_created_at"] = int(rows["last_ts"] or 0) if rows["last_ts"] else None
                remaining = max(self.cfg.daily_limit_usd - used, 0.0)
                result.update(ok=True, used_usd=used, remaining_usd=remaining)
                if live.get("note"):
                    result["note"] = f"{result.get('note', '')} {live.get('note')}".strip()
                return result
        except Exception as e:
            result["note"] = f"读取 ccswitch 失败: {e}"
            return result

    def prime_error_cursor(self) -> None:
        p = self.db_path
        if not p.exists():
            return
        try:
            with self._connect_ro() as conn:
                row = conn.execute("select max(rowid) from proxy_request_logs").fetchone()
                self.last_proxy_rowid = int(row[0] or 0)
        except Exception as e:
            self.log(f"初始化 ccswitch 402 游标失败: {e}", "WARN")

    def get_new_402_errors(self) -> list[str]:
        p = self.db_path
        if not p.exists():
            return []
        hits: list[str] = []
        try:
            with self._connect_ro() as conn:
                cur = conn.cursor()
                row = cur.execute("select max(rowid) from proxy_request_logs").fetchone()
                max_id = int(row[0] or 0)
                if self.last_proxy_rowid == 0:
                    self.last_proxy_rowid = max_id
                    return []
                rows = cur.execute(
                    """
                    select rowid, status_code, error_message, model, created_at
                    from proxy_request_logs
                    where rowid > ? and app_type='codex'
                    order by rowid asc
                    """,
                    (self.last_proxy_rowid,),
                ).fetchall()
                self.last_proxy_rowid = max_id
                for r in rows:
                    status = int(r["status_code"] or 0)
                    err = r["error_message"] or ""
                    text = f"rowid={r['rowid']} status={status} model={r['model']} created_at={r['created_at']} error={err}"
                    if status == 402 or re.search(r"\b402\b", err):
                        hits.append(text[:600])
        except Exception as e:
            self.log(f"实时扫描 ccswitch 402 失败: {e}", "WARN")
        return hits

    def get_new_quota_errors(self) -> list[str]:
        return self.get_new_402_errors()


class CodexLogWatcher:
    def __init__(self, cfg: Config, log: Callable[[str, str], None]) -> None:
        self.cfg = cfg
        self.log = log
        self.last_log_id = 0

    @property
    def db_path(self) -> Path:
        return expand_path(self.cfg.codex_logs_sqlite)

    def _connect_ro(self) -> sqlite3.Connection:
        p = self.db_path
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=1.0)
        conn.execute("pragma query_only=1")
        conn.row_factory = sqlite3.Row
        return conn

    def _is_runtime_402_error(self, level: str, target: str, body: str) -> bool:
        # Codex logs also contain the user's prompt/tool text. Only accept
        # transport/session runtime records, not echoed text that merely says "402".
        text = f"{level} {target} {body or ''}"
        allowed_targets = (
            "codex_core::responses_retry",
            "codex_core::session::turn",
            "codex_http_client::client",
        )
        if not any(t in target for t in allowed_targets):
            return False
        exact_patterns = [
            r"unexpected\s+status\s+402\s+Payment\s+Required",
            r"status\s*=\s*402\s+Payment\s+Required",
            r"status\s+402\s+Payment\s+Required",
            r"sampling_error=.*status\s+402",
            r"每日额度超限",
        ]
        return any(re.search(p, text, re.I | re.S) for p in exact_patterns)

    def _extract_thread_ids(self, text: str) -> list[str]:
        ids: list[str] = []
        patterns = [
            r"thread(?:_id|\.id)?[=:\s\{\(]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            r"cch_session_id:\s*codex_[^_\s,)]*_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        ]
        for pat in patterns:
            for m in re.finditer(pat, text, re.I):
                tid = m.group(1).lower()
                if tid not in ids:
                    ids.append(tid)
        return ids

    def prime(self) -> None:
        p = self.db_path
        if not p.exists():
            return
        try:
            with self._connect_ro() as conn:
                if self.cfg.scan_recent_errors_on_start:
                    self.last_log_id = 0
                else:
                    row = conn.execute("select max(id) from logs").fetchone()
                    self.last_log_id = int(row[0] or 0)
        except Exception as e:
            self.log(f"Codex 日志初始化失败: {e}", "WARN")

    def get_new_quota_errors(self) -> list[dict[str, Any]]:
        if not self.cfg.trigger_on_codex_quota_error:
            return []
        p = self.db_path
        if not p.exists():
            return []
        hits: list[dict[str, Any]] = []
        try:
            with self._connect_ro() as conn:
                cur = conn.cursor()
                if self.last_log_id == 0:
                    self.prime()
                    if not self.cfg.scan_recent_errors_on_start:
                        return []
                row = cur.execute("select max(id) from logs").fetchone()
                max_id = int(row[0] or self.last_log_id)
                rows = cur.execute(
                    """
                    select id, level, target, feedback_log_body
                    from logs
                    where id > ?
                    order by id asc
                    limit 5000
                    """,
                    (self.last_log_id,),
                ).fetchall()
                self.last_log_id = max_id
                for r in rows:
                    level = str(r["level"] or "")
                    target = str(r["target"] or "")
                    body = str(r["feedback_log_body"] or "")
                    text = f"{level} {target} {body}"
                    if "account/rateLimits/updated" in text:
                        continue
                    if self._is_runtime_402_error(level, target, body):
                        hits.append(
                            {
                                "log_id": int(r["id"]),
                                "target": target,
                                "thread_ids": self._extract_thread_ids(text),
                                "text": text[:800],
                            }
                        )
        except Exception as e:
            self.log(f"扫描 Codex 日志失败: {e}", "WARN")
        return hits


def parse_money_after(text: str, labels: list[str]) -> Optional[float]:
    flat = re.sub(r"\s+", " ", text)
    for label in labels:
        patterns = [
            rf"{re.escape(label)}[^$\d-]{{0,80}}\$?\s*(-?\d+(?:\.\d+)?)",
            rf"\$\s*(-?\d+(?:\.\d+)?)[^\n]{{0,80}}{re.escape(label)}",
        ]
        for pat in patterns:
            m = re.search(pat, flat, re.I)
            if m:
                return safe_float(m.group(1), 0.0)
    return None


class AixhanAutomator:
    def __init__(self, cfg: Config, log: Callable[[str, str], None]) -> None:
        self.cfg = cfg
        self.log = log
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = threading.RLock()
        # Playwright browser objects stay on one async worker thread. Callers
        # from GUI/monitor threads only submit jobs and wait for results.
        self._jobs: Optional[queue.Queue] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_thread_id: Optional[int] = None

    def _import_playwright(self):
        try:
            from playwright.async_api import async_playwright  # type: ignore

            return async_playwright
        except Exception as e:
            raise RuntimeError(
                "Playwright 未安装。请先运行 install.bat，或执行: python -m pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple && python -m playwright install chromium"
            ) from e

    def _start_worker(self) -> None:
        with self._lock:
            if self._worker_thread and self._worker_thread.is_alive() and self._jobs is not None:
                return
            self._jobs = queue.Queue()
            self._worker_thread = threading.Thread(target=self._browser_worker, name="aixhan-browser-worker", daemon=True)
            self._worker_thread.start()

    def _browser_worker(self) -> None:
        self._worker_thread_id = threading.get_ident()
        try:
            asyncio.run(self._browser_worker_async())
        finally:
            self._worker_thread_id = None

    async def _browser_worker_async(self) -> None:
        assert self._jobs is not None
        loop = asyncio.get_running_loop()
        while True:
            job = await loop.run_in_executor(None, self._jobs.get)
            if job is None:
                break
            fn, args, kwargs, result_q = job
            try:
                value = fn(*args, **kwargs)
                if inspect.isawaitable(value):
                    value = await value
                result_q.put((True, value))
            except Exception as e:
                result_q.put((False, e))
        await self._close_browser_resources()

    def _run_browser_job(self, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = None, **kwargs: Any) -> Any:
        if threading.get_ident() == self._worker_thread_id:
            raise RuntimeError("浏览器任务不能在浏览器工作线程内同步嵌套调用。")
        self._start_worker()
        assert self._jobs is not None
        result_q: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        self._jobs.put((fn, args, kwargs, result_q))
        ok, value = result_q.get(timeout=timeout)
        if ok:
            return value
        raise value

    def ensure_browser(self) -> bool:
        self._run_browser_job(self._ensure_browser)
        return True

    async def _ensure_browser(self):
        if self._page is not None:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:
                pass
        async_playwright = self._import_playwright()
        profile = expand_path(self.cfg.browser_profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        self.log("启动 AixHan 浏览器会话，首次使用请在页面输入/确认卡密。")
        self._pw = await async_playwright().start()
        configured = str(self.cfg.browser_channel or "auto").strip().lower()
        channels: list[Optional[str]]
        if configured and configured != "auto":
            channels = [configured]
        else:
            channels = [None, "chrome", "msedge"]
        last_error: Optional[Exception] = None
        for channel in channels:
            try:
                kwargs = {
                    "user_data_dir": str(profile),
                    "headless": bool(self.cfg.headless),
                    "viewport": {"width": 1280, "height": 900},
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if channel:
                    kwargs["channel"] = channel
                self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
                self.log(f"浏览器已启动: {channel or 'playwright-chromium'}")
                break
            except Exception as e:
                last_error = e
        if self._ctx is None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
            raise RuntimeError(f"浏览器启动失败，请安装 Chrome/Edge 或运行 install_browser.bat: {last_error}")
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.set_default_timeout(int(self.cfg.page_timeout_ms))
        return self._page

    def open_card_page(self) -> None:
        self._run_browser_job(self._open_card_page)

    async def _open_card_page(self) -> None:
        page = await self._ensure_browser()
        await self._goto_aixhan(page)
        await self._ensure_card_key_loaded(page)
        self.log("已打开 AixHan 页面；如页面要求卡密，请输入/确认卡密后再读取额度。")

    def open_login_page(self) -> None:
        self.open_card_page()

    def close(self) -> None:
        thread = self._worker_thread
        jobs = self._jobs
        if thread and thread.is_alive() and jobs is not None:
            try:
                self._run_browser_job(self._close_browser_resources, timeout=20)
            except Exception:
                pass
            try:
                jobs.put(None)
                thread.join(timeout=5)
            except Exception:
                pass
        else:
            # Browser resources are created only on the dedicated worker thread.
            pass
        self._worker_thread = None
        self._jobs = None

    async def _close_browser_resources(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None
        self._page = None

    async def _goto_aixhan(self, page) -> None:
        await page.goto(self.cfg.aixhan_url, wait_until="domcontentloaded", timeout=max(15000, self.cfg.page_timeout_ms))
        try:
            await page.wait_for_load_state("networkidle", timeout=max(5000, int(self.cfg.page_timeout_ms)))
        except Exception:
            await page.wait_for_timeout(max(1200, int(self.cfg.card_action_delay_ms)))
        try:
            await page.wait_for_function(
                """() => document.readyState === 'complete' || document.readyState === 'interactive'""",
                timeout=max(3000, int(self.cfg.page_timeout_ms)),
            )
        except Exception:
            pass
        await page.wait_for_timeout(max(500, int(self.cfg.card_action_delay_ms // 2)))

    async def _body_text(self, page) -> str:
        try:
            return await page.locator("body").inner_text(timeout=self.cfg.page_timeout_ms)
        except Exception:
            return ""

    async def _enter_card_key_tab(self, page, timeout_ms: int = 5000) -> dict[str, Any]:
        """先进入卡密页，再做任何充值/重置按钮查找。"""
        deadline = time.time() + max(1.0, timeout_ms / 1000.0)
        last: dict[str, Any] = {"clicked": False, "reason": "not_started"}
        while time.time() < deadline:
            try:
                result = await page.evaluate(
                    """() => {
                        const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                        const visible = (el) => {
                            const st = window.getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return st && st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                        };
                        const textOf = (el) => norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                        const nodes = Array.from(document.querySelectorAll('a,button,[role=tab],[role=button],nav *,header *,div,span'));
                        let best = null;
                        let bestScore = -1;
                        for (const el of nodes) {
                            if (!visible(el)) continue;
                            const text = textOf(el);
                            if (!text || text.length > 50) continue;
                            const rect = el.getBoundingClientRect();
                            if (rect.width > window.innerWidth * 0.70 || rect.height > 120) continue;
                            let score = -1;
                            if (text === '卡密激活') score = 120;
                            else if (text.includes('卡密激活')) score = 100;
                            else if (text.includes('卡密') && text.includes('激活')) score = 90;
                            else if (text.includes('卡密')) score = 60;
                            if (el.closest('nav,header')) score += 20;
                            if (el.getAttribute('role') === 'tab') score += 10;
                            if (score > bestScore) { best = el; bestScore = score; }
                        }
                        if (!best) return {clicked:false, reason:'no_card_tab'};
                        best.scrollIntoView({block:'center', inline:'center'});
                        best.click();
                        return {clicked:true, text:textOf(best).slice(0,80), score:bestScore};
                    }"""
                )
                last = result if isinstance(result, dict) else {"clicked": bool(result), "result": result}
                if last.get("clicked"):
                    try:
                        await page.wait_for_load_state("networkidle", timeout=3000)
                    except Exception:
                        await page.wait_for_timeout(max(1000, int(self.cfg.card_action_delay_ms // 2)))
                    await page.wait_for_timeout(max(800, int(self.cfg.card_action_delay_ms // 2)))
                    return last
            except Exception as e:
                last = {"clicked": False, "reason": str(e)}
            await page.wait_for_timeout(250)
        return last

    async def _ensure_card_key_loaded(self, page) -> bool:
        card_key = str(self.cfg.aixhan_card_key or "").strip()
        text = await self._body_text(page)
        if "选择适合你的套餐" not in text and any(s in text for s in ["账号管理", "当前卡密", "剩余额度", "今日用量"]):
            return True
        if not card_key:
            return False

        fill_script = """async ([cardKey, delayMs]) => {
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const visible = (el) => {
                const st = window.getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return st && st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const scoreInput = (el) => {
                const attrs = [el.placeholder, el.name, el.id, el.getAttribute('aria-label'), el.getAttribute('autocomplete')].map(norm).join(' ');
                const parent = norm(el.closest('label,div,form,section')?.innerText || '');
                const hay = `${attrs} ${parent}`;
                let score = 0;
                for (const w of ['卡密', '卡号', '兑换码', 'CDK', 'code', 'key', 'token']) {
                    if (hay.toLowerCase().includes(w.toLowerCase())) score += 10;
                }
                if ((el.tagName || '').toLowerCase() === 'textarea') score += 1;
                if (el.type === 'password' || el.type === 'text' || !el.type) score += 2;
                return score;
            };
            const inputs = Array.from(document.querySelectorAll('input,textarea')).filter(visible);
            if (!inputs.length) return {filled:false, clicked:false, reason:'no_visible_input'};
            inputs.sort((a,b) => scoreInput(b) - scoreInput(a));
            const input = inputs[0];
            input.focus();
            input.value = cardKey;
            input.dispatchEvent(new Event('input', {bubbles:true}));
            input.dispatchEvent(new Event('change', {bubbles:true}));
            await new Promise(resolve => setTimeout(resolve, Math.max(300, delayMs || 1200)));
            const buttonWords = ['确认', '查询', '进入', '提交', '验证', '使用', '导入', '绑定', '查看', '激活'];
            const skipWords = ['套餐选购', '订单查询', '模型健康'];
            const isSubmitLike = (el) => {
                const t = norm(el.innerText || el.textContent || '');
                if (!t || t.length > 80) return false;
                if (t === '卡密激活') return false;
                if (skipWords.some(w => t.includes(w))) return false;
                if (el.closest('nav,header')) return false;
                return buttonWords.some(w => t.includes(w));
            };
            const containers = [];
            let cur = input.closest('form,section');
            if (cur) containers.push(cur);
            cur = input.parentElement;
            for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) containers.push(cur);
            for (const c of containers) {
                const localButtons = Array.from(c.querySelectorAll('button,[role=button],a,div')).filter(visible);
                for (const el of localButtons) {
                    if (isSubmitLike(el)) {
                        const t = norm(el.innerText || el.textContent || '');
                        el.click();
                        return {filled:true, clicked:true, button:t.slice(0,80)};
                    }
                }
            }
            const buttons = Array.from(document.querySelectorAll('button,[role=button],a,div')).filter(visible);
            for (const el of buttons) {
                if (isSubmitLike(el)) {
                    const t = norm(el.innerText || el.textContent || '');
                    el.click();
                    return {filled:true, clicked:true, button:t.slice(0,80)};
                }
            }
            input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', bubbles:true}));
            input.dispatchEvent(new KeyboardEvent('keyup', {key:'Enter', code:'Enter', bubbles:true}));
            return {filled:true, clicked:false, reason:'enter_sent'};
        }"""

        switch_tab_script = """() => {
            const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
            const visible = (el) => {
                const st = window.getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return st && st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const words = ['卡密激活', '卡密管理', '卡密', '激活'];
            const nodes = Array.from(document.querySelectorAll('a,button,[role=tab],[role=button],nav *,header *,div,span'));
            let best = null;
            let bestScore = -1;
            for (const el of nodes) {
                if (!visible(el)) continue;
                const text = norm(el.innerText || el.textContent || '');
                if (!text || text.length > 40) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width > window.innerWidth * 0.75 || rect.height > 120) continue;
                let score = -1;
                if (text === '卡密激活') score = 100;
                else if (text.includes('卡密激活')) score = 90;
                else if (text.includes('卡密') && text.includes('激活')) score = 80;
                else if (text.includes('卡密')) score = 60;
                else if (text.includes('激活')) score = 40;
                if (score > bestScore) { best = el; bestScore = score; }
            }
            if (best) {
                best.click();
                return {clicked:true, text:norm(best.innerText || best.textContent || '').slice(0,80), score:bestScore};
            }
            return {clicked:false, reason:'no_card_tab'};
        }"""

        try:
            delay_ms = max(500, int(self.cfg.card_action_delay_ms))
            changed = await page.evaluate(fill_script, [card_key, delay_ms])
            if isinstance(changed, dict) and not changed.get("filled") and changed.get("reason") == "no_visible_input":
                switched = await page.evaluate(switch_tab_script)
                self.log(f"未找到卡密输入框，已尝试切换到卡密激活页: {switched}", "WARN")
                try:
                    await page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    await page.wait_for_timeout(max(1200, delay_ms))
                try:
                    await page.wait_for_function(
                        """() => Array.from(document.querySelectorAll('input,textarea')).some(el => {
                            const st = window.getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return st && st.display !== 'none' && st.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                        })""",
                        timeout=max(3000, self.cfg.page_timeout_ms),
                    )
                except Exception:
                    await page.wait_for_timeout(delay_ms)
                await page.wait_for_timeout(delay_ms)
                changed = await page.evaluate(fill_script, [card_key, delay_ms])
            self.log(f"已尝试自动填入 AixHan 卡密: {changed}")
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                await page.wait_for_timeout(max(800, delay_ms // 2))
            return bool(not isinstance(changed, dict) or changed.get("filled"))
        except Exception as e:
            self.log(f"自动填卡密失败: {e}", "WARN")
            return False

    def read_quota_from_page(self, navigate: bool = True) -> dict[str, Any]:
        return self._run_browser_job(self._read_quota_from_page, navigate)

    async def _read_quota_from_page(self, navigate: bool = True) -> dict[str, Any]:
        page = await self._ensure_browser()
        if navigate:
            await self._goto_aixhan(page)
            await self._ensure_card_key_loaded(page)
        text = await self._body_text(page)
        remaining = parse_money_after(text, ["剩余额度", "剩余", "可用额度"])
        used = parse_money_after(text, ["今日已消耗", "今日用量", "已消耗"])
        limit = parse_money_after(text, ["每日限额", "今日限额", "日限额"])
        if limit is not None and limit > 0:
            self.cfg.daily_limit_usd = float(limit)
            self.cfg.save()
        if remaining is None and used is not None:
            remaining = max(float(self.cfg.daily_limit_usd) - used, 0.0)
        return {
            "ok": remaining is not None or used is not None,
            "source": "aixhan_page",
            "remaining_usd": remaining,
            "used_usd": used,
            "daily_limit_usd": limit or self.cfg.daily_limit_usd,
            "raw_excerpt": text[:1000],
        }

    async def _click_button_text(self, page, includes: list[str], timeout_ms: int = 5000) -> bool:
        deadline = time.time() + timeout_ms / 1000.0
        last_error = None
        while time.time() < deadline:
            for word in includes:
                try:
                    loc = page.locator("button").filter(has_text=re.compile(re.escape(word)))
                    count = await loc.count()
                    for i in range(count - 1, -1, -1):
                        btn = loc.nth(i)
                        if await btn.is_visible() and await btn.is_enabled():
                            await btn.click(timeout=1200)
                            return True
                except Exception as e:
                    last_error = e
                try:
                    loc = page.get_by_role("button", name=re.compile(re.escape(word)))
                    count = await loc.count()
                    for i in range(count - 1, -1, -1):
                        btn = loc.nth(i)
                        if await btn.is_visible() and await btn.is_enabled():
                            await btn.click(timeout=1200)
                            return True
                except Exception as e:
                    last_error = e
            try:
                clicked = await page.evaluate(
                    """(words) => {
                        const isVisible = (el) => {
                            const s = window.getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return s && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                        };
                        const textOf = (el) => (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || el.value || '').replace(/\s+/g, ' ').trim();
                        const nodes = Array.from(document.querySelectorAll('button,[role=button],div,a'));
                        for (let i = nodes.length - 1; i >= 0; i--) {
                            const el = nodes[i];
                            const txt = textOf(el);
                            if (!txt) continue;
                            if (txt.length > 120) continue;
                            if (words.some(w => txt.includes(w)) && isVisible(el)) {
                                el.click();
                                return txt;
                            }
                        }
                        return null;
                    }""",
                    includes,
                )
                if clicked:
                    return True
            except Exception as e:
                last_error = e
            await page.wait_for_timeout(200)
        if last_error:
            self.log(f"点击按钮失败: {last_error}", "WARN")
        return False

    async def _visible_action_candidates(self, page, limit: int = 24) -> list[str]:
        try:
            values = await page.evaluate(
                """(limit) => {
                    const isVisible = (el) => {
                        const s = window.getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return s && s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
                    };
                    const textOf = (el) => (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || el.value || '').replace(/\s+/g, ' ').trim();
                    const nodes = Array.from(document.querySelectorAll('button,[role=button],a,input[type=button],input[type=submit]'));
                    const out = [];
                    for (const el of nodes) {
                        if (!isVisible(el)) continue;
                        const txt = textOf(el);
                        if (!txt || txt.length > 80) continue;
                        if (!out.includes(txt)) out.push(txt);
                        if (out.length >= limit) break;
                    }
                    return out;
                }""",
                max(1, int(limit)),
            )
            return [str(v) for v in values if str(v).strip()]
        except Exception:
            return []

    def reset_quota(self) -> dict[str, Any]:
        return self._run_browser_job(self._reset_quota)

    async def _reset_quota(self) -> dict[str, Any]:
        started = time.time()
        page = await self._ensure_browser()
        self.log("开始执行 AixHan 充值/重置额度。")
        await self._goto_aixhan(page)
        tab_result = await self._enter_card_key_tab(page, timeout_ms=5000)
        self.log(f"已先进入卡密页再寻找充值/重置按钮: {tab_result}", "OK" if tab_result.get("clicked") else "WARN")
        card_loaded = await self._ensure_card_key_loaded(page)
        if not card_loaded:
            self.log("当前仍在套餐页/未进入卡密账号页，再切一次卡密激活页并重试。", "WARN")
            tab_result = await self._enter_card_key_tab(page, timeout_ms=5000)
            self.log(f"二次进入卡密页结果: {tab_result}", "OK" if tab_result.get("clicked") else "WARN")
            card_loaded = await self._ensure_card_key_loaded(page)
            if not card_loaded:
                candidates = await self._visible_action_candidates(page)
                hint = f"；当前可见操作: {' / '.join(candidates[:12])}" if candidates else ""
                raise RuntimeError(f"未进入卡密账号页，已停止充值/重置点击，避免误点套餐购买{hint}。")
        before = {"source": "no_page_quota_read", "note": "按要求不从页面读取额度，只执行充值/重置点击"}
        action_words = ["充值额度", "重置额度", "恢复额度", "补充额度", "续充额度"]
        if not await self._click_button_text(page, action_words, timeout_ms=7000):
            # 有些页面默认停在套餐页或卡密页，额度操作藏在卡密激活后的账号/额度管理页；先切导航并重新载入卡密。
            await self._enter_card_key_tab(page, timeout_ms=5000)
            await self._ensure_card_key_loaded(page)
            await self._click_button_text(page, ["账号管理", "额度管理", "卡密管理"], timeout_ms=2500)
            try:
                await page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                await page.wait_for_timeout(max(900, int(self.cfg.card_action_delay_ms // 2)))
            if not await self._click_button_text(page, action_words, timeout_ms=7000):
                candidates = await self._visible_action_candidates(page)
                hint = f"；当前可见操作: {' / '.join(candidates[:12])}" if candidates else ""
                raise RuntimeError(f"页面上没有找到“充值额度/重置额度”按钮{hint}。")
        await page.wait_for_timeout(max(600, int(self.cfg.card_action_delay_ms // 3)))
        confirm_words = ["确认充值", "确认重置", "确认扣", "并充值额度", "并重置额度", "立即充值", "确定", "确认"]
        if not await self._click_button_text(page, confirm_words, timeout_ms=8000):
            candidates = await self._visible_action_candidates(page)
            hint = f"；当前可见操作: {' / '.join(candidates[:12])}" if candidates else ""
            raise RuntimeError(f"没有找到确认充值/重置按钮{hint}。")
        self.log("已点击确认，等待页面刷新额度。")
        elapsed = time.time() - started
        after = {"ok": True, "source": "reset_click", "note": "已点击确认重置；未读取页面额度", "elapsed_seconds": elapsed}
        self.log(f"重置点击流程完成，耗时 {elapsed:.1f}s；未读取页面额度。", "OK")
        self._write_history(True, "reset_click_ok_no_page_quota_read", before, after, elapsed)
        return {"ok": True, "before": before, "after": after, "elapsed_seconds": elapsed}

    def _write_history(self, ok: bool, reason: str, before: dict[str, Any], after: dict[str, Any], elapsed: float) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": now_text(),
                "ok": ok,
                "reason": reason,
                "before": before,
                "after": after,
                "elapsed_seconds": round(elapsed, 3),
            }
            with RESET_HISTORY_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass


class CodexContinuator:
    def __init__(self, cfg: Config, log: Callable[[str, str], None]) -> None:
        self.cfg = cfg
        self.log = log

    @staticmethod
    def _ps_quote(value: Any) -> str:
        return "'" + str(value or "").replace("'", "''") + "'"

    def _find_codex_cli(self) -> Optional[str]:
        configured = str(getattr(self.cfg, "codex_cli_path", "") or "").strip()
        candidates: list[Path] = []
        if configured:
            p = expand_path(configured)
            candidates.append(p / "codex.exe" if p.is_dir() else p)
        local_bin = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        if local_bin.exists():
            candidates.extend(sorted(local_bin.glob("*\codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True))
        found = shutil.which("codex")
        if found:
            candidates.append(Path(found))
        resources = Path(r"C:\Program Files\WindowsApps")
        if resources.exists():
            try:
                candidates.extend(
                    sorted(resources.glob("OpenAI.Codex_*_x64__*\app\resources\codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
                )
            except Exception:
                pass
        for p in candidates:
            try:
                if p.exists() and p.name.lower() == "codex.exe":
                    probe = subprocess.run(
                        [str(p), "--help"],
                        capture_output=True,
                        text=True,
                        timeout=4,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if probe.returncode == 0:
                        return str(p)
            except Exception:
                continue
        return None

    def _resume_goal_threads(self, thread_ids: list[str]) -> dict[str, Any]:
        unique: list[str] = []
        for tid in thread_ids or []:
            tid = str(tid or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tid) and tid not in unique:
                unique.append(tid)
        if not unique:
            return {"resumed": 0, "goal_thread_ids": set()}
        db_path = expand_path(getattr(self.cfg, "codex_goals_sqlite", r"%USERPROFILE%\.codex\goals_1.sqlite"))
        if not db_path.exists():
            self.log(f"目标库不存在，按普通会话处理: {db_path}", "WARN")
            return {"resumed": 0, "goal_thread_ids": set()}
        resumed = 0
        goal_thread_ids: set[str] = set()
        should_resume = bool(getattr(self.cfg, "auto_resume_goal_after_reset", True))
        try:
            now_ms = int(time.time() * 1000)
            with sqlite3.connect(str(db_path), timeout=3.0) as conn:
                cur = conn.cursor()
                for tid in unique:
                    row = cur.execute("select status from thread_goals where thread_id=?", (tid,)).fetchone()
                    if not row:
                        continue
                    goal_thread_ids.add(tid)
                    status = str(row[0] or "")
                    if not should_resume:
                        self.log(f"检测到目标模式但恢复开关关闭，跳过 continue: {tid[:8]}…{tid[-4:]} ({status})", "WARN")
                        continue
                    if status == "active":
                        self.log(f"目标模式已是 active，仅恢复逻辑完成，不发送 continue: {tid[:8]}…{tid[-4:]}", "INFO")
                        continue
                    if status in {"paused", "blocked", "usage_limited"}:
                        cur.execute("update thread_goals set status='active', updated_at_ms=? where thread_id=?", (now_ms, tid))
                        try:
                            cur.execute("delete from thread_goal_continuation_deferrals where thread_id=?", (tid,))
                        except sqlite3.Error:
                            pass
                        resumed += 1
                        self.log(f"已恢复目标模式，不发送 continue: {tid[:8]}…{tid[-4:]} ({status} -> active)", "OK")
                    else:
                        self.log(f"目标状态 {status}，不发送 continue: {tid[:8]}…{tid[-4:]}", "INFO")
                conn.commit()
        except Exception as e:
            self.log(f"恢复目标模式失败: {e}", "WARN")
        return {"resumed": resumed, "goal_thread_ids": goal_thread_ids}

    def resume_goals_for_threads(self, thread_ids: list[str]) -> int:
        return int(self._resume_goal_threads(thread_ids).get("resumed", 0))

    def send_continue_to_threads(self, thread_ids: list[str], reason: str = "") -> int:
        unique: list[str] = []
        for tid in thread_ids or []:
            tid = str(tid or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tid) and tid not in unique:
                unique.append(tid)
        if not unique:
            return 0
        goal_result = self._resume_goal_threads(unique)
        goal_thread_ids = set(goal_result.get("goal_thread_ids") or set())
        continue_ids = [tid for tid in unique if tid not in goal_thread_ids]
        handled = len(goal_thread_ids)
        if goal_thread_ids:
            self.log(
                f"检测到 {len(goal_thread_ids)} 个目标模式 402 会话：只恢复目标，不发送 continue。",
                "OK" if goal_result.get("resumed", 0) else "INFO",
            )
        if not continue_ids:
            self.log(f"全部 402 会话都是目标模式，已跳过 queue continue，来源 {reason or '--'}", "OK")
            return handled
        if not bool(self.cfg.auto_continue_after_reset):
            self.log(f"continue 续跑开关关闭，跳过 {len(continue_ids)} 个非目标 402 会话。", "WARN")
            return handled
        if not bool(getattr(self.cfg, "auto_continue_all_402_threads", True)):
            self.log(f"非目标 402 批量 continue 开关关闭，跳过 {len(continue_ids)} 个会话。", "WARN")
            return handled
        delay = max(0.0, float(self.cfg.codex_continue_delay_seconds))
        if delay:
            time.sleep(delay)
        cli = self._find_codex_cli()
        if not cli:
            self.log("未找到 codex.exe，无法对多个 402 会话批量 queue continue，改用当前窗口发送。", "WARN")
            return handled + int(bool(self.send_continue(reason or "fallback-current-window")))
        message = str(self.cfg.codex_continue_text or "continue").strip() or "continue"
        sent = 0
        failed: list[str] = []
        for tid in continue_ids:
            try:
                completed = subprocess.run(
                    [cli, "queue", "--thread", tid, "--message", message],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                output = (completed.stdout or completed.stderr or "").strip()
                if completed.returncode == 0:
                    sent += 1
                    self.log(f"已 queue {message!r} 到 402 会话 {tid[:8]}…{tid[-4:]}: {output[:160]}", "OK")
                else:
                    failed.append(f"{tid[:8]}…{tid[-4:]}:{output[:120]}")
            except Exception as e:
                failed.append(f"{tid[:8]}…{tid[-4:]}:{e}")
        if failed:
            self.log(f"部分 402 会话 queue continue 失败: {'; '.join(failed[:5])}", "WARN")
        self.log(f"非目标会话批量 continue 完成：{sent}/{len(continue_ids)} 个 402 会话，来源 {reason or '--'}", "OK" if sent else "WARN")
        return handled + sent

    def send_continue(self, reason: str = "") -> bool:
        if not bool(self.cfg.auto_continue_after_reset):
            return False
        delay = max(0.0, float(self.cfg.codex_continue_delay_seconds))
        if delay:
            time.sleep(delay)
        message = str(self.cfg.codex_continue_text or "continue").strip() or "continue"
        keyword = str(self.cfg.codex_continue_window_keyword or "ChatGPT").strip() or "ChatGPT"
        click_bottom = "$true" if bool(self.cfg.codex_continue_click_bottom) else "$false"
        script = f"""
$WindowKeyword = {self._ps_quote(keyword)}
$Message = {self._ps_quote(message)}
[bool]$ClickBottom = {click_bottom}
""" + r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
public class CodexAutoContinueWin32 {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
}
"@
$keywordMode = -not [string]::IsNullOrWhiteSpace($WindowKeyword)
$candidates = Get-Process | Where-Object {
    $_.MainWindowHandle -ne 0 -and
    $_.MainWindowTitle -and
    $_.MainWindowTitle -notlike '*AixHan Quota Auto Reset*' -and
    (($keywordMode -and $_.MainWindowTitle -like "*$WindowKeyword*") -or ((-not $keywordMode) -and $_.ProcessName -in @('ChatGPT','Codex')))
}
$target = $candidates | Sort-Object `
    @{Expression = { if ($_.ProcessName -eq 'ChatGPT') { 2 } elseif ($_.ProcessName -eq 'Codex') { 1 } else { 0 } }; Descending = $true}, `
    @{Expression = { $_.StartTime }; Descending = $true} | Select-Object -First 1
if (-not $target) { throw "未找到 Codex/ChatGPT 窗口，关键词=$WindowKeyword" }
$hwnd = $target.MainWindowHandle
[CodexAutoContinueWin32]::ShowWindowAsync($hwnd, 9) | Out-Null
Start-Sleep -Milliseconds 250
try { (New-Object -ComObject WScript.Shell).AppActivate([int]$target.Id) | Out-Null } catch {}
[CodexAutoContinueWin32]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 350
if ($ClickBottom) {
    $rect = New-Object RECT
    if ([CodexAutoContinueWin32]::GetWindowRect($hwnd, [ref]$rect)) {
        $x = [int](($rect.Left + $rect.Right) / 2)
        $height = [Math]::Max(1, $rect.Bottom - $rect.Top)
        $offset = if ($height -gt 500) { 95 } else { 55 }
        $y = [int]($rect.Bottom - $offset)
        [CodexAutoContinueWin32]::SetCursorPos($x, $y) | Out-Null
        Start-Sleep -Milliseconds 120
        [CodexAutoContinueWin32]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 60
        [CodexAutoContinueWin32]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 220
    }
}
$oldClipboardText = $null
try { $oldClipboardText = [System.Windows.Forms.Clipboard]::GetText() } catch {}
[System.Windows.Forms.Clipboard]::SetText($Message)
[System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 120
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
Start-Sleep -Milliseconds 200
try { if ($null -ne $oldClipboardText) { [System.Windows.Forms.Clipboard]::SetText($oldClipboardText) } } catch {}
Write-Output ("sent process=" + $target.ProcessName + " pid=" + $target.Id + " title=" + $target.MainWindowTitle)
'''
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            completed = subprocess.run(
                ["powershell.exe", "-Sta", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                capture_output=True,
                text=True,
                timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.log(f"发送 continue 失败: {e}", "WARN")
            return False
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode == 0:
            self.log(f"已向 Codex 会话发送 {message!r}: {output}", "OK")
            return True
        self.log(f"发送 continue 失败: {output}", "WARN")
        return False


class AutoResetService:
    def __init__(self, cfg: Config, bus: LogBus, status_cb: Callable[[dict[str, Any]], None]) -> None:
        self.cfg = cfg
        self.bus = bus
        self.log = bus.log
        self.status_cb = status_cb
        self.ccswitch = CCSwitchReader(cfg, self.log)
        self.codex = CodexLogWatcher(cfg, self.log)
        self.aixhan = AixhanAutomator(cfg, self.log)
        self.continuator = CodexContinuator(cfg, self.log)
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.reset_lock = threading.Lock()
        self.pending_continue_lock = threading.Lock()
        self.pending_continue_thread_ids: set[str] = set()
        self.last_reset_at = 0.0
        self.last_trigger_key = ""
        self.last_usage_refresh_at = 0.0

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.codex.prime()
        self.ccswitch.prime_error_cursor()
        self.thread = threading.Thread(target=self._loop, name="auto-reset-loop", daemon=True)
        self.thread.start()
        self.log(f"自动重置已开启：实时抓 ccswitch/Codex 402；目标模式只恢复，非目标会话批量 continue。版本 {APP_VERSION}", "OK")
        if self.cfg.prewarm_browser_on_auto_start:
            threading.Thread(target=self._prewarm_browser, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        self.log("自动重置已关闭。")

    def close(self) -> None:
        self.stop()
        self.aixhan.close()

    def _prewarm_browser(self) -> None:
        try:
            self.aixhan.open_login_page()
        except Exception as e:
            self.log(f"浏览器预热失败: {e}", "WARN")

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as e:
                self.log(f"自动检测异常: {e}\n{traceback.format_exc(limit=3)}", "ERROR")
            self.stop_event.wait(max(0.2, float(self.cfg.poll_interval_seconds)))

    def tick(self) -> None:
        # Fast path: ccswitch 402 if it gets written, then Codex's own runtime
        # 402 log as a fallback because ccswitch can record only a final 200.
        if self.cfg.trigger_on_ccswitch_402_error:
            for hit in self.ccswitch.get_new_402_errors():
                self.trigger_reset(f"ccswitch 实时捕获 402: {hit}")
                return
        codex_hits = self.codex.get_new_quota_errors()
        if codex_hits:
            thread_ids: list[str] = []
            for hit in codex_hits:
                thread_ids.extend(hit.get("thread_ids") or [])
            first = codex_hits[0]
            self.trigger_reset(
                f"Codex 运行日志捕获 402: log_id={first.get('log_id')} target={first.get('target')} threads={len(set(thread_ids))} {first.get('text')}",
                continue_thread_ids=thread_ids,
            )
            return
        # 402 is the fast path. The ccswitch usage endpoint refreshes slower,
        # but when it already reports 0 it is a useful fallback if no 402 row was logged.
        now = time.time()
        interval = max(30, int(self.cfg.ccswitch_usage_refresh_seconds))
        if now - self.last_usage_refresh_at >= interval:
            self.last_usage_refresh_at = now
            usage = self.ccswitch.get_today_usage()
            usage["note"] = f"{usage.get('note', '')}；显示用量每 {interval}s 刷新，剩余为0时兜底触发".strip("；")
            self.status_cb(usage)
            if self.maybe_trigger_from_usage(usage, "ccswitch 用量接口"):
                return

    def maybe_trigger_from_usage(self, usage: dict[str, Any], source: str = "ccswitch 用量接口") -> bool:
        if not self.cfg.trigger_on_ccswitch_remaining_zero:
            return False
        if not usage or not usage.get("ok"):
            return False
        remaining = usage.get("remaining_usd")
        if remaining is None:
            return False
        threshold = float(self.cfg.reset_when_remaining_lte)
        rem = safe_float(remaining, self.cfg.daily_limit_usd)
        if rem <= threshold:
            self.trigger_reset(f"{source}显示剩余 ${rem:.2f} ≤ 阈值 ${threshold:.2f}，执行402兜底重置")
            return True
        return False

    def _trigger_source_label(self, reason: str) -> str:
        if reason.startswith("ccswitch 实时捕获 402"):
            return "ccswitch_402"
        if reason.startswith("Codex 运行日志捕获 402"):
            return "codex_runtime_402"
        if "剩余" in reason and "兜底" in reason:
            return "cc_remaining_zero_fallback"
        if reason.startswith("手动"):
            return "manual"
        return "reset_trigger"

    def _add_pending_continue_thread_ids(self, thread_ids: Optional[list[str]]) -> list[str]:
        added: list[str] = []
        if not thread_ids:
            return added
        with self.pending_continue_lock:
            for tid in thread_ids:
                tid = str(tid or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tid):
                    if tid not in self.pending_continue_thread_ids:
                        added.append(tid)
                    self.pending_continue_thread_ids.add(tid)
        if added:
            self.log(f"已记录 {len(added)} 个待 continue 的 402 会话: {', '.join(t[:8] + '…' + t[-4:] for t in added[:8])}", "INFO")
        return added

    def _consume_pending_continue_thread_ids(self) -> list[str]:
        with self.pending_continue_lock:
            ids = sorted(self.pending_continue_thread_ids)
            self.pending_continue_thread_ids.clear()
        return ids

    def _today_successful_reset_count(self) -> int:
        if not RESET_HISTORY_PATH.exists():
            return 0
        today = date.today().isoformat()
        count = 0
        try:
            with RESET_HISTORY_PATH.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("ok") is True and str(rec.get("ts", "")).startswith(today):
                        count += 1
        except Exception as e:
            self.log(f"读取今日重置次数失败: {e}", "WARN")
        return count

    def _daily_reset_limit_status(self) -> tuple[bool, int, int]:
        max_resets = max(0, int(getattr(self.cfg, "daily_max_resets", 0) or 0))
        if max_resets <= 0:
            return False, self._today_successful_reset_count(), 0
        count = self._today_successful_reset_count()
        return count >= max_resets, count, max_resets

    def trigger_reset(self, reason: str, continue_thread_ids: Optional[list[str]] = None) -> None:
        added_ids = self._add_pending_continue_thread_ids(continue_thread_ids)
        if time.time() - self.last_reset_at < int(self.cfg.reset_cooldown_seconds):
            self.log(f"触发被冷却跳过: {reason}", "WARN")
            if added_ids:
                threading.Thread(target=self.continuator.send_continue_to_threads, args=(added_ids, "cooldown_after_reset"), daemon=True).start()
            return
        key = reason[:200]
        if self.cfg.reset_after_codex_error_once and key == self.last_trigger_key:
            self.log(f"重复触发已跳过: {reason}", "WARN")
            return
        limited, count, max_resets = self._daily_reset_limit_status()
        if limited:
            self.log(f"已达到当日最大重置次数 {count}/{max_resets}，本次不再自动重置: {reason}", "WARN")
            return
        self.last_trigger_key = key
        threading.Thread(target=self._reset_worker, args=(reason,), daemon=True).start()

    def _reset_worker(self, reason: str) -> None:
        if not self.reset_lock.acquire(blocking=False):
            self.log(f"已有重置任务运行中，本次跳过: {reason}", "WARN")
            return
        try:
            trigger_source = self._trigger_source_label(reason)
            limited, count, max_resets = self._daily_reset_limit_status()
            if limited:
                self.log(f"已达到当日最大重置次数 {count}/{max_resets}，跳过本次重置 [{trigger_source}]。", "WARN")
                return
            self.log(f"触发自动重置 [{trigger_source}]: {reason}", "TRIGGER")
            result = self.aixhan.reset_quota()
            self.last_reset_at = time.time()
            limited_after, count_after, max_after = self._daily_reset_limit_status()
            if max_after:
                self.log(f"今日成功重置次数: {count_after}/{max_after}", "OK" if count_after < max_after else "WARN")
            after = result.get("after") or {}
            self.status_cb(
                {
                    "ok": True,
                    "source": trigger_source,
                    "used_usd": None,
                    "remaining_usd": None,
                    "daily_limit_usd": self.cfg.daily_limit_usd,
                    "note": f"刚完成重置，来源 {trigger_source}，耗时 {result.get('elapsed_seconds', 0):.1f}s",
                }
            )
            if trigger_source != "manual":
                thread_ids = self._consume_pending_continue_thread_ids()
                sent = self.continuator.send_continue_to_threads(thread_ids, trigger_source)
                allow_continue = bool(self.cfg.auto_continue_after_reset)
                if thread_ids and not sent and allow_continue:
                    self.log("非目标会话批量 queue continue 未成功，改为给当前 Codex 窗口发送 continue。", "WARN")
                    self.continuator.send_continue(trigger_source)
                elif not sent and not thread_ids and allow_continue:
                    self.log("本次 402 未提取到 thread_id，改为给当前 Codex 窗口发送 continue。", "WARN")
                    self.continuator.send_continue(trigger_source)
        except Exception as e:
            self.last_reset_at = time.time()
            self.log(f"重置失败: {e}\n{traceback.format_exc(limit=4)}", "ERROR")
        finally:
            self.reset_lock.release()

    def reset_now(self) -> None:
        threading.Thread(target=self._reset_worker, args=("手动点击立即重置",), daemon=True).start()

    def send_continue_now(self) -> None:
        threading.Thread(target=self.continuator.send_continue, args=("手动测试continue",), daemon=True).start()

    def open_login_page(self) -> None:
        threading.Thread(target=self._prewarm_browser, daemon=True).start()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.cfg = Config.load()
        self.bus = LogBus()
        self.service = AutoResetService(self.cfg, self.bus, self.on_status)
        self.title(f"{APP_NAME} - {APP_VERSION}")
        self.geometry("1180x760")
        self.minsize(1020, 660)
        self.configure(bg="#F4F7FB")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._apply_window_icon()
        self._status_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.entries: dict[str, tk.Variable] = {}
        self.card_value_labels: dict[str, tk.Label] = {}
        self.action_buttons: list[ttk.Button] = []
        self._configure_style()
        self._build_ui()
        self._load_config_to_ui()
        self.after(120, self._drain_logs)
        self.after(300, self._drain_status)
        self.bus.log(f"程序已启动，版本 {APP_VERSION}。请先填卡密并点击“打开/输入卡密”，确认页面能看到账号管理。", "OK")

    def _apply_window_icon(self) -> None:
        try:
            if APP_ICON_PATH.exists():
                self.iconbitmap(default=str(APP_ICON_PATH))
        except Exception:
            pass

    def _configure_style(self) -> None:
        self.colors = {
            "bg": "#F4F7FB",
            "panel": "#FFFFFF",
            "panel_alt": "#F8FAFC",
            "text": "#0F172A",
            "muted": "#64748B",
            "border": "#DDE5F2",
            "accent": "#2563EB",
            "accent_dark": "#1D4ED8",
            "success": "#16A34A",
            "danger": "#DC2626",
            "warning": "#D97706",
            "purple": "#7C3AED",
            "log_bg": "#0B1220",
        }
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=self.colors["bg"])
        style.configure("Panel.TFrame", background=self.colors["panel"])
        style.configure("Soft.TFrame", background=self.colors["panel_alt"])
        style.configure("TLabel", background=self.colors["bg"], foreground=self.colors["text"], font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=self.colors["panel"], foreground=self.colors["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=self.colors["bg"], foreground=self.colors["text"], font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", background=self.colors["bg"], foreground=self.colors["muted"], font=("Microsoft YaHei UI", 10))
        style.configure("Section.TLabel", background=self.colors["panel"], foreground=self.colors["text"], font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("TEntry", fieldbackground="#FFFFFF", foreground=self.colors["text"], bordercolor=self.colors["border"], lightcolor=self.colors["border"], darkcolor=self.colors["border"], padding=7)
        style.map("TEntry", bordercolor=[("focus", self.colors["accent"])])
        style.configure("TCheckbutton", background=self.colors["panel"], foreground=self.colors["text"], font=("Microsoft YaHei UI", 9))
        style.map("TCheckbutton", background=[("active", self.colors["panel"])] )
        style.configure("Primary.TButton", background=self.colors["accent"], foreground="#FFFFFF", borderwidth=0, focusthickness=0, padding=(15, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", self.colors["accent_dark"]), ("pressed", self.colors["accent_dark"])], foreground=[("disabled", "#E2E8F0")])
        style.configure("Success.TButton", background=self.colors["success"], foreground="#FFFFFF", borderwidth=0, focusthickness=0, padding=(15, 9), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Success.TButton", background=[("active", "#15803D"), ("pressed", "#166534")])
        style.configure("Ghost.TButton", background="#EEF4FF", foreground=self.colors["accent"], borderwidth=0, focusthickness=0, padding=(14, 9), font=("Microsoft YaHei UI", 10))
        style.map("Ghost.TButton", background=[("active", "#DBEAFE"), ("pressed", "#BFDBFE")])
        style.configure("Tiny.TButton", background="#EEF2FF", foreground=self.colors["accent"], borderwidth=0, padding=(8, 5), font=("Microsoft YaHei UI", 9))
        style.map("Tiny.TButton", background=[("active", "#DBEAFE")])
        style.configure("TPanedwindow", background=self.colors["bg"], borderwidth=0)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        hero = tk.Frame(self, bg=self.colors["bg"], padx=24, pady=18)
        hero.grid(row=0, column=0, sticky="ew")
        hero.columnconfigure(0, weight=1)
        tk.Label(hero, text="AixHan 额度守护", bg=self.colors["bg"], fg=self.colors["text"], font=("Microsoft YaHei UI", 22, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(hero, text="实时监听 ccswitch/Codex 的 402；目标模式只恢复目标，非目标 402 会话才 queue continue。", bg=self.colors["bg"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 10)).grid(row=1, column=0, sticky="w", pady=(5, 0))
        self.status_var = tk.StringVar(value="待启动")
        self.status_chip = tk.Label(hero, textvariable=self.status_var, bg="#E2E8F0", fg=self.colors["text"], padx=14, pady=7, font=("Microsoft YaHei UI", 10, "bold"))
        self.status_chip.grid(row=0, column=1, rowspan=2, sticky="e")

        actions = tk.Frame(self, bg=self.colors["panel"], highlightthickness=1, highlightbackground=self.colors["border"], padx=16, pady=14)
        actions.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 14))
        actions.columnconfigure(8, weight=1)
        self.auto_var = tk.BooleanVar(value=False)
        self.auto_btn = ttk.Checkbutton(actions, text="自动重置 OFF", variable=self.auto_var, command=self.toggle_auto)
        self.auto_btn.grid(row=0, column=0, padx=(0, 14), sticky="w")
        ttk.Button(actions, text="立即重置", command=self.reset_now, style="Primary.TButton").grid(row=0, column=1, padx=5)
        ttk.Button(actions, text="打开/输入卡密", command=self.open_login, style="Success.TButton").grid(row=0, column=2, padx=5)
        ttk.Button(actions, text="刷新CC额度", command=self.read_ccswitch, style="Ghost.TButton").grid(row=0, column=3, padx=5)
        ttk.Button(actions, text="保存配置", command=self.save_config_from_ui, style="Ghost.TButton").grid(row=0, column=4, padx=5)
        ttk.Button(actions, text="测试continue", command=self.send_continue, style="Ghost.TButton").grid(row=0, column=5, padx=5)
        tk.Label(actions, text="触发不读页面额度：目标/continue/次数均可选", bg=self.colors["panel"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).grid(row=0, column=8, sticky="e")

        quick = tk.Frame(actions, bg="#F8FAFC", highlightthickness=1, highlightbackground=self.colors["border"], padx=10, pady=8)
        quick.grid(row=1, column=0, columnspan=9, sticky="ew", pady=(12, 0))
        quick.columnconfigure(6, weight=1)
        tk.Label(quick, text="续跑策略", bg="#F8FAFC", fg=self.colors["accent"], font=("Microsoft YaHei UI", 9, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.entries["auto_continue_after_reset"] = tk.BooleanVar()
        self.entries["auto_resume_goal_after_reset"] = tk.BooleanVar()
        self.entries["daily_max_resets"] = tk.StringVar()
        ttk.Checkbutton(quick, text="继续 continue", variable=self.entries["auto_continue_after_reset"]).grid(row=0, column=1, sticky="w", padx=(0, 10))
        ttk.Checkbutton(quick, text="继续目标", variable=self.entries["auto_resume_goal_after_reset"]).grid(row=0, column=2, sticky="w", padx=(0, 12))
        tk.Label(quick, text="当日最多重置", bg="#F8FAFC", fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).grid(row=0, column=3, sticky="w")
        ttk.Entry(quick, textvariable=self.entries["daily_max_resets"], width=6).grid(row=0, column=4, sticky="w", padx=(6, 4))
        tk.Label(quick, text="次（0=不限）", bg="#F8FAFC", fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).grid(row=0, column=5, sticky="w")

        cards = tk.Frame(self, bg=self.colors["bg"], padx=24)
        cards.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="metric")
        self.used_var = tk.StringVar(value="--")
        self.remaining_var = tk.StringVar(value="--")
        self.limit_var = tk.StringVar(value="--")
        self.note_var = tk.StringVar(value="--")
        self._make_metric_card(cards, 0, "今日已消耗", self.used_var, "#EFF6FF", self.colors["accent"], "used")
        self._make_metric_card(cards, 1, "剩余额度", self.remaining_var, "#ECFDF5", self.colors["success"], "remaining")
        self._make_metric_card(cards, 2, "每日限额", self.limit_var, "#F5F3FF", self.colors["purple"], "limit")
        self._make_metric_card(cards, 3, "数据来源", self.note_var, "#FFF7ED", self.colors["warning"], "note", small=True)

        pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        pane.grid(row=3, column=0, sticky="nsew", padx=24, pady=(0, 20))
        cfg_frame = tk.Frame(pane, bg=self.colors["panel"], highlightthickness=1, highlightbackground=self.colors["border"], padx=16, pady=14)
        log_frame = tk.Frame(pane, bg=self.colors["panel"], highlightthickness=1, highlightbackground=self.colors["border"], padx=16, pady=14)
        pane.add(cfg_frame, weight=2)
        pane.add(log_frame, weight=3)

        tk.Label(cfg_frame, text="配置中心", bg=self.colors["panel"], fg=self.colors["text"], font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        tk.Label(cfg_frame, text="卡密显示前四位，其余用星号隐藏；更换卡密时直接输入完整新卡密。", bg=self.colors["panel"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 12))

        fields = [
            ("aixhan_url", "AixHan 地址"),
            ("aixhan_card_key", "AixHan 卡密"),
            ("daily_limit_usd", "每日限额 USD"),
            ("reset_when_remaining_lte", "触发阈值 USD"),
            ("poll_interval_seconds", "402轮询秒数"),
            ("reset_cooldown_seconds", "重置冷却秒"),
            ("daily_max_resets", "当日最大重置次数"),
            ("ccswitch_usage_refresh_seconds", "CC额度刷新秒"),
            ("card_action_delay_ms", "卡密点击延迟ms"),
            ("codex_continue_delay_seconds", "continue等待秒"),
            ("codex_continue_text", "continue文本"),
            ("codex_continue_window_keyword", "Codex窗口关键词"),
            ("codex_cli_path", "codex.exe路径"),
            ("codex_logs_sqlite", "Codex 日志 DB"),
            ("codex_goals_sqlite", "Codex 目标 DB"),
            ("ccswitch_db", "ccswitch DB"),
            ("browser_profile_dir", "浏览器资料目录"),
            ("browser_channel", "浏览器通道"),
        ]
        cfg_frame.columnconfigure(1, weight=1)
        for i, (key, label) in enumerate(fields):
            r = i + 2
            tk.Label(cfg_frame, text=label, bg=self.colors["panel"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).grid(row=r, column=0, sticky="w", pady=5)
            existing_var = self.entries.get(key)
            var = existing_var if isinstance(existing_var, tk.StringVar) else tk.StringVar()
            self.entries[key] = var
            ttk.Entry(cfg_frame, textvariable=var).grid(row=r, column=1, sticky="ew", pady=5, padx=(10, 0))
            if key in {"codex_logs_sqlite", "codex_goals_sqlite", "ccswitch_db", "browser_profile_dir", "codex_cli_path"}:
                ttk.Button(cfg_frame, text="选择", width=5, command=lambda k=key: self.browse_path(k), style="Tiny.TButton").grid(
                    row=r, column=2, padx=(7, 0)
                )

        bools = [
            ("headless", "隐藏浏览器"),
            ("prewarm_browser_on_auto_start", "自动开启时预热浏览器"),
            ("auto_continue_after_reset", "是否继续 continue"),
            ("auto_continue_all_402_threads", "非目标402会话continue"),
            ("auto_resume_goal_after_reset", "是否继续目标"),
            ("codex_continue_click_bottom", "发送前点击窗口底部输入框"),
            ("trigger_on_ccswitch_402_error", "实时抓 ccswitch 402 触发"),
            ("trigger_on_codex_quota_error", "Codex日志402兜底触发"),
            ("trigger_on_ccswitch_remaining_zero", "CC剩余为0备用触发"),
        ]
        base_row = len(fields) + 3
        tk.Label(cfg_frame, text="自动策略", bg=self.colors["panel"], fg=self.colors["text"], font=("Microsoft YaHei UI", 11, "bold")).grid(row=base_row - 1, column=0, columnspan=3, sticky="w", pady=(12, 4))
        for i, (key, label) in enumerate(bools):
            existing_var = self.entries.get(key)
            var = existing_var if isinstance(existing_var, tk.BooleanVar) else tk.BooleanVar()
            self.entries[key] = var
            ttk.Checkbutton(cfg_frame, text=label, variable=var).grid(
                row=base_row + i, column=0, columnspan=3, sticky="w", pady=2
            )

        tip = tk.Frame(cfg_frame, bg="#F8FAFC", highlightthickness=1, highlightbackground=self.colors["border"], padx=12, pady=10)
        tip.grid(row=base_row + len(bools), column=0, columnspan=3, sticky="ew", pady=(14, 0))
        tk.Label(tip, text="小提示", bg="#F8FAFC", fg=self.colors["accent"], font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        tk.Label(tip, text="自动重置会收集本轮所有真实 402 的 thread_id；目标模式按“是否继续目标”恢复为 active，非目标会话按“是否继续 continue”发送 queue continue。当日最大重置次数为 0 时不限制。", bg="#F8FAFC", fg=self.colors["muted"], font=("Microsoft YaHei UI", 9), wraplength=360, justify="left").pack(anchor="w", pady=(4, 0))

        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_head = tk.Frame(log_frame, bg=self.colors["panel"])
        log_head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        log_head.columnconfigure(0, weight=1)
        tk.Label(log_head, text="实时重置日志", bg=self.colors["panel"], fg=self.colors["text"], font=("Microsoft YaHei UI", 13, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(log_head, text="OK / WARN / ERROR 会自动着色", bg=self.colors["panel"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).grid(row=0, column=1, sticky="e")

        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            height=24,
            state="disabled",
            font=("Consolas", 10),
            bg=self.colors["log_bg"],
            fg="#DCE7F7",
            insertbackground="#FFFFFF",
            relief="flat",
            padx=12,
            pady=10,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.tag_configure("INFO", foreground="#DCE7F7")
        self.log_text.tag_configure("OK", foreground="#86EFAC")
        self.log_text.tag_configure("WARN", foreground="#FCD34D")
        self.log_text.tag_configure("ERROR", foreground="#FCA5A5")
        self.log_text.tag_configure("TRIGGER", foreground="#C4B5FD")
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        yscroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=yscroll.set)

    def _make_metric_card(self, parent: tk.Frame, column: int, title: str, var: tk.StringVar, bg: str, accent: str, key: str, small: bool = False) -> None:
        card = tk.Frame(parent, bg=self.colors["panel"], highlightthickness=1, highlightbackground=self.colors["border"], padx=14, pady=12)
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0 if column == 3 else 8))
        icon = tk.Label(card, text="●", bg=bg, fg=accent, width=2, font=("Microsoft YaHei UI", 12, "bold"))
        icon.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(0, 10))
        tk.Label(card, text=title, bg=self.colors["panel"], fg=self.colors["muted"], font=("Microsoft YaHei UI", 9)).grid(row=0, column=1, sticky="w")
        value = tk.Label(card, textvariable=var, bg=self.colors["panel"], fg=self.colors["text"], font=("Microsoft YaHei UI", 11 if small else 16, "bold"), anchor="w", justify="left")
        value.grid(row=1, column=1, sticky="ew", pady=(3, 0))
        card.columnconfigure(1, weight=1)
        self.card_value_labels[key] = value

    def _set_status_chip(self, text: str, bg: str, fg: str = "#FFFFFF") -> None:
        self.status_var.set(text)
        self.status_chip.configure(bg=bg, fg=fg)

    def _load_config_to_ui(self) -> None:
        for key, var in self.entries.items():
            val = getattr(self.cfg, key)
            if isinstance(var, tk.BooleanVar):
                var.set(bool(val))
            elif key == "aixhan_card_key":
                var.set(mask_card_key(val))
            else:
                var.set(str(val))

    def _coerce(self, key: str, value: str) -> Any:
        current = getattr(self.cfg, key)
        if key == "daily_max_resets":
            return max(0, int(float(value or 0)))
        if isinstance(current, bool):
            return bool(value)
        if isinstance(current, int):
            return int(float(value))
        if isinstance(current, float):
            return float(value)
        return value

    def save_config_from_ui(self) -> None:
        try:
            for key, var in self.entries.items():
                if isinstance(var, tk.BooleanVar):
                    setattr(self.cfg, key, bool(var.get()))
                elif key == "aixhan_card_key":
                    raw = str(var.get()).strip()
                    current = str(getattr(self.cfg, key) or "").strip()
                    if is_masked_card_key(raw, current):
                        setattr(self.cfg, key, current)
                    else:
                        setattr(self.cfg, key, raw)
                else:
                    setattr(self.cfg, key, self._coerce(key, str(var.get()).strip()))
            self.cfg.save()
            if "aixhan_card_key" in self.entries:
                self.entries["aixhan_card_key"].set(mask_card_key(self.cfg.aixhan_card_key))
            self.bus.log("配置已保存。", "OK")
        except Exception as e:
            messagebox.showerror(APP_NAME, f"保存配置失败: {e}")

    def browse_path(self, key: str) -> None:
        if key.endswith("_dir") or key == "browser_profile_dir":
            val = filedialog.askdirectory(title=key)
        else:
            val = filedialog.askopenfilename(title=key)
        if val:
            self.entries[key].set(val)

    def toggle_auto(self) -> None:
        self.save_config_from_ui()
        if self.auto_var.get():
            self.auto_btn.configure(text="自动重置 ON")
            self.service.start()
            self._set_status_chip("● 自动重置运行中", self.colors["success"])
        else:
            self.auto_btn.configure(text="自动重置 OFF")
            self.service.stop()
            self._set_status_chip("● 已停止", "#94A3B8")

    def reset_now(self) -> None:
        self.save_config_from_ui()
        self.service.reset_now()

    def open_login(self) -> None:
        self.save_config_from_ui()
        self.service.open_login_page()

    def send_continue(self) -> None:
        self.save_config_from_ui()
        self.service.send_continue_now()

    def read_ccswitch(self) -> None:
        self.save_config_from_ui()
        usage = self.service.ccswitch.get_today_usage()
        self.on_status(usage)
        self.bus.log(f"ccswitch 读取结果: {json.dumps(usage, ensure_ascii=False)}")
        if self.auto_var.get():
            self.service.maybe_trigger_from_usage(usage, "手动刷新CC额度")

    def on_status(self, data: dict[str, Any]) -> None:
        self._status_queue.put(data)

    def _drain_status(self) -> None:
        latest = None
        while True:
            try:
                latest = self._status_queue.get_nowait()
            except queue.Empty:
                break
        if latest:
            used = latest.get("used_usd")
            rem = latest.get("remaining_usd")
            limit = latest.get("daily_limit_usd")
            self.used_var.set(f"${safe_float(used, 0):.2f}" if used is not None else "--")
            rem_val = safe_float(rem, 0)
            self.remaining_var.set(f"${rem_val:.2f}" if rem is not None else "--")
            self.limit_var.set(f"${safe_float(limit, self.cfg.daily_limit_usd):.2f}")
            src = latest.get("source", "--")
            note = latest.get("note", "")
            self.note_var.set(f"{src} {note}"[:70])
            remaining_label = self.card_value_labels.get("remaining")
            if remaining_label and rem is not None:
                if rem_val <= float(self.cfg.reset_when_remaining_lte):
                    remaining_label.configure(fg=self.colors["danger"])
                elif rem_val <= max(1.0, float(self.cfg.daily_limit_usd) * 0.1):
                    remaining_label.configure(fg=self.colors["warning"])
                else:
                    remaining_label.configure(fg=self.colors["success"])
        self.after(500, self._drain_status)

    def _log_level_from_line(self, line: str) -> str:
        if "][ERROR]" in line:
            return "ERROR"
        if "][WARN]" in line:
            return "WARN"
        if "][OK]" in line:
            return "OK"
        if "][TRIGGER]" in line:
            return "TRIGGER"
        return "INFO"

    def _drain_logs(self) -> None:
        changed = False
        while True:
            try:
                _level, line = self.bus.q.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n", self._log_level_from_line(line))
            self.log_text.configure(state="disabled")
            changed = True
        if changed:
            self.log_text.see("end")
        self.after(150, self._drain_logs)

    def on_close(self) -> None:
        self.service.close()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
