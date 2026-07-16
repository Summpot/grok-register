"""CLI wrapper for grok_register_ttk — multi-thread register + async CPA mint pipeline.

Architecture:
  Register workers (R)  →  accounts_cli + mint_queue
  Mint workers (M)      →  cpa_auths/xai-*.json + optional hotload

Browser lifecycle:
  - One Chromium per register worker, reused via TabPool.clear_session
  - Full recycle every N accounts or on error
  - Register browser released BEFORE mint (mint always standalone Chromium)
  - Peak browsers ≈ R + M (not 2×R)
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import traceback
from typing import Any
from pathlib import Path

from grok_register import app as reg  # noqa: E402
from grok_register.paths import PROJECT_ROOT, OUTPUT_DIR, ensure_output_dir, TURNSTILE_DIR


# Linux 适配: DrissionPage 默认找 'chrome', 我们装的是 chromium
# 保留原版 slim flags + proxy，再补 chromium 路径与 turnstilePatch。
_orig_create_browser_options = reg.create_browser_options


def _patched_create_browser_options(*args, **kwargs):
    # Prefer original factory (proxy + CHROMIUM_SLIM_FLAGS + extension + background flags)
    try:
        opts = _orig_create_browser_options(*args, **kwargs)
    except TypeError:
        try:
            opts = _orig_create_browser_options()
        except Exception:
            opts = None
    except Exception:
        opts = None
    if opts is None:
        from DrissionPage import ChromiumOptions

        opts = ChromiumOptions()
        opts.auto_port()
        opts.set_timeouts(base=1)
        for flag in getattr(reg, "CHROMIUM_SLIM_FLAGS", ()) or ():
            try:
                opts.set_argument(flag)
            except Exception:
                pass
        try:
            apply_bg = getattr(reg, "_apply_register_background_flags", None)
            if callable(apply_bg):
                apply_bg(opts)
        except Exception:
            pass

    try:
        opts.auto_port()
    except Exception:
        pass
    try:
        opts.set_timeouts(base=1)
    except Exception:
        pass

    for cand in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if os.path.isfile(cand):
            try:
                opts.set_browser_path(cand)
            except Exception:
                pass
            break

    ext_path = str(TURNSTILE_DIR)
    if os.path.isdir(ext_path):
        try:
            opts.add_extension(ext_path)
        except Exception:
            pass
    return opts


reg.create_browser_options = _patched_create_browser_options


# ── 线程安全日志 ──

_log_queue: queue.Queue = queue.Queue()


def _log_writer():
    while True:
        msg = _log_queue.get()
        if msg is None:
            break
        print(msg, flush=True)


def log(worker_id: int | str, msg: str) -> None:
    _log_queue.put(f"[{time.strftime('%H:%M:%S')}] [W{worker_id}] {msg}")


# ── 统计 ──

_stats_lock = threading.Lock()
_stats = {
    "reg_success": 0,
    "reg_fail": 0,
    "mint_success": 0,
    "mint_fail": 0,
    "mint_skip": 0,
    "cloud_upload_success": 0,
    "cloud_upload_fail": 0,
    "cloud_upload_skip": 0,
}

# CPA cloud upload: queue during mint; flush every N accounts (and final drain).
_pending_cloud_lock = threading.Lock()
_pending_cloud_paths: list[str] = []
_cloud_flush_lock = threading.Lock()


def _queue_cloud_upload(path: str | None, config: dict | None = None) -> None:
    """Queue a CPA file; when pending reaches batch_every, flush that chunk."""
    if not path:
        return
    pth = str(path)
    to_flush: list[str] = []
    with _pending_cloud_lock:
        if pth not in _pending_cloud_paths:
            _pending_cloud_paths.append(pth)
        cfg = config or {}
        if cfg.get("cpa_cloud_upload_enabled", False):
            try:
                every = int(cfg.get("cpa_cloud_upload_batch_every", 10) or 10)
            except Exception:
                every = 10
            every = max(0, min(every, 1000))
            if every > 0 and len(_pending_cloud_paths) >= every:
                to_flush = _pending_cloud_paths[:every]
                del _pending_cloud_paths[:every]
    if to_flush and config is not None:
        log(0, f"[cloud-cpa] pending reached {len(to_flush)} — mid-batch upload")
        _flush_cloud_uploads(config, paths=to_flush)


def _flush_cloud_uploads(config: dict, paths: list[str] | None = None) -> dict:
    """Upload CPA files with account-round-robin chat gate.

    - paths=None: drain all pending (final flush)
    - paths=[...]: flush a pre-taken chunk (mid-batch every N)
    """
    if not config.get("cpa_cloud_upload_enabled", False):
        if paths is None:
            with _pending_cloud_lock:
                _pending_cloud_paths.clear()
        log(0, "[cloud-cpa] batch upload skipped: cpa_cloud_upload_enabled=false")
        return {"ok": True, "skipped": True, "count": 0}
    if paths is None:
        with _pending_cloud_lock:
            paths = list(_pending_cloud_paths)
            _pending_cloud_paths.clear()
    if not paths:
        log(0, "[cloud-cpa] batch upload: no local CPA files to upload")
        return {"ok": True, "count": 0}

    # Serialize mid-batch + final flushes so stats/logs stay coherent.
    with _cloud_flush_lock:
        return _flush_cloud_uploads_locked(config, paths)


def _flush_cloud_uploads_locked(config: dict, paths: list[str]) -> dict:
    upload_paths = list(paths)
    skip = 0
    if bool(config.get("cpa_cloud_upload_require_chat", True)):
        log(
            0,
            f"[cloud-cpa] batch chat gate start: {len(paths)} account(s) "
            f"(round-robin, not consecutive N probes per account)",
        )
        gate = reg.probe_cpa_auth_paths_round_robin(
            paths,
            cfg=config,
            log_callback=lambda m: log(0, m),
        )
        upload_paths = list(gate.get("passed") or [])
        failed = gate.get("failed") or {}
        for fpath, fres in failed.items():
            skip += 1
            _inc("cloud_upload_skip")
            st = fres.get("status")
            reason = fres.get("reason") or "chat_not_usable"
            log(
                0,
                f"[cloud-cpa] skipped {Path(fpath).name}: {reason}"
                + (f" chat_status={st}" if st is not None else ""),
            )
        log(
            0,
            f"[cloud-cpa] batch chat gate result: pass={len(upload_paths)} "
            f"skip={skip} rounds={gate.get('rounds')}",
        )

    log(0, f"[cloud-cpa] batch upload start: {len(upload_paths)} file(s) (of {len(paths)} minted)")
    ok = fail = 0
    for path in upload_paths:
        try:
            res = reg.upload_cpa_auth_file_to_cloud(
                path,
                config,
                log_callback=lambda m: log(0, m),
                skip_chat_gate=True,
            )
            if res.get("ok"):
                ok += 1
                _inc("cloud_upload_success")
                log(0, f"[cloud-cpa] uploaded -> {Path(path).name}")
            elif res.get("skipped"):
                skip += 1
                _inc("cloud_upload_skip")
                reason = res.get("reason") or "skipped"
                log(0, f"[cloud-cpa] skipped {Path(path).name}: {reason}")
            else:
                fail += 1
                _inc("cloud_upload_fail")
                log(0, f"[cloud-cpa] upload failed {Path(path).name}: {res.get('error') or res}")
        except Exception as exc:
            fail += 1
            _inc("cloud_upload_fail")
            log(0, f"[cloud-cpa] upload exception {Path(path).name}: {exc}")
    log(
        0,
        f"[cloud-cpa] batch upload done: ok={ok} skip={skip} fail={fail} total={len(paths)}",
    )
    return {
        "ok": fail == 0,
        "count": len(paths),
        "success": ok,
        "skip": skip,
        "fail": fail,
    }


def _inc(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


# forever 任务索引
_next_idx_lock = threading.Lock()
_next_idx = [1]

# mint 队列结束哨兵
_MINT_STOP = object()


def resolve_mint_workers(
    *,
    cli_value: int,
    threads: int,
    config: dict,
    inline_mint: bool,
) -> int:
    """Resolve mint worker count.

    Priority: --inline-mint > CLI --mint-workers (>=0) > config cpa_mint_workers > auto.
    auto (-1): min(threads, 4) when CPA export enabled, else 0.
    0: inline mint on register threads.
    """
    if inline_mint:
        return 0
    if cli_value >= 0:
        return max(0, min(int(cli_value), 10))
    cfg_v = config.get("cpa_mint_workers", -1)
    try:
        cfg_v = int(cfg_v)
    except Exception:
        cfg_v = -1
    if cfg_v >= 0:
        return max(0, min(cfg_v, 10))
    # auto
    if config.get("cpa_export_enabled", True):
        return max(1, min(int(threads), 4))
    return 0


def resolve_mint_queue_max(config: dict, mint_workers: int, cli_value: int | None = None) -> int:
    if cli_value is not None and cli_value >= 0:
        return int(cli_value)
    try:
        v = int(config.get("cpa_mint_queue_max", 0) or 0)
    except Exception:
        v = 0
    if v > 0:
        return v
    # default backpressure: 2 × mint workers (0 if no mint pool)
    return max(0, mint_workers * 2) if mint_workers > 0 else 0


class DummyStop:
    def __call__(self) -> bool:
        return False


def _ensure_browser(worker_id: int, force_recycle: bool = False):
    """Start browser if missing; optional full recycle.

    Use thread-local browser from grok_register_ttk. TabPool had a separate
    empty registry, so every account opened a new Chrome and leaked the old one.
    """
    if force_recycle:
        try:
            reg.stop_browser()
        except Exception:
            pass
    browser = None
    try:
        browser = reg._tls_get_browser()
    except Exception:
        try:
            browser = reg.TabPool.get_browser()
        except Exception:
            browser = None
    if browser is None:
        reg.start_browser(log_callback=lambda m: log(worker_id, m))


def register_one(
    worker_id: int,
    idx: int,
    total: int,
    accounts_file: str,
    *,
    do_mint_inline: bool = False,
    mint_queue: queue.Queue | None = None,
) -> dict | None:
    """Run one registration. Enqueue CPA mint (default) instead of blocking.

    Returns dict(email, sso, profile) or None.
    """
    email = ""
    dev_token = ""
    max_mail_retry = 3
    cancel = DummyStop()

    # Pin a proxy for this account/thread before browser start.
    try:
        reg.assign_thread_proxy(lambda m: log(worker_id, m), force_new=True)
    except Exception as exc:
        log(worker_id, f"[proxy] assign failed: {exc}")

    try:
        _ensure_browser(worker_id, force_recycle=False)
    except Exception as exc:
        log(worker_id, f"! 浏览器启动失败: {exc}")
        return None

    for mail_try in range(1, max_mail_retry + 1):
        try:
            log(worker_id, f"--- 第 {idx}/{total} 个账号, 邮箱尝试 {mail_try}/{max_mail_retry} ---")
            log(worker_id, "1. 打开注册页")
            reg.open_signup_page(log_callback=lambda m: log(worker_id, m), cancel_callback=cancel)
            log(worker_id, "2. 创建邮箱并提交")
            email, dev_token = reg.fill_email_and_submit(
                log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
            )
            log(worker_id, f"邮箱: {email}")
            log(worker_id, "3. 拉取验证码")
            code = reg.fill_code_and_submit(
                email,
                dev_token,
                log_callback=lambda m: log(worker_id, m),
                cancel_callback=cancel,
            )
            log(worker_id, f"验证码: {code}")
            break
        except Exception as exc:
            msg = str(exc)
            try:
                from grok_register.cpa_xai.proxyutil import (
                    disable_proxy,
                    get_runtime_proxy,
                    is_proxy_failure,
                    proxy_log_label,
                )
                if is_proxy_failure(msg):
                    cur = (get_runtime_proxy() or "").strip()
                    if cur:
                        disable_proxy(cur, reason=msg[:160])
                        log(
                            worker_id,
                            f"[proxy] disabled {proxy_log_label(cur)} (email_stage)",
                        )
                        # force new proxy on browser restart
                        try:
                            reg.assign_thread_proxy(
                                lambda m: log(worker_id, m), force_new=True
                            )
                        except Exception:
                            pass
            except Exception:
                pass
            if ("未收到验证码" in msg or "验证码" in msg) and mail_try < max_mail_retry:
                log(worker_id, f"! 本邮箱未取到验证码，换邮箱重试: {msg}")
                try:
                    reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                except Exception:
                    pass
                reg.sleep_with_cancel(1, cancel)
                continue
            log(worker_id, f"! 邮箱阶段失败: {msg}")
            traceback.print_exc()
            _inc("reg_fail")
            try:
                reg.restart_browser(log_callback=lambda m: log(worker_id, m))
            except Exception:
                pass
            return None

    try:
        log(worker_id, "4. 填写资料")
        profile = reg.fill_profile_and_submit(
            log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
        )
        log(worker_id, f"资料已填: {profile.get('given_name')} {profile.get('family_name')}")
        log(worker_id, "5. 等待 sso cookie")
        sso = reg.wait_for_sso_cookie(
            log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
        )
        password = profile.get("password", "") or ""
        line = f"{email}----{password}----{sso}\n"
        os.makedirs(os.path.dirname(os.path.abspath(accounts_file)) or ".", exist_ok=True)
        with open(accounts_file, "a", encoding="utf-8") as f:
            f.write(line)
        log(worker_id, f"+ 注册成功: {email}")
        reg.mark_used(email, password)

        # Capture cookies BEFORE releasing browser (for mint cookie inject)
        page = reg._get_page()
        cookies = []
        try:
            from grok_register import cpa_export as _cpa_exp

            cookies = _cpa_exp.export_cookies_from_page(page) if page is not None else []
        except Exception:
            cookies = []
        if cookies:
            log(worker_id, f"[*] 导出 cookie {len(cookies)} 条供 mint 注入")

        if page and reg.PERF_FLAGS.get("cookie_snapshot", True):
            try:
                reg.save_cookies_snapshot(page, "success", email)
            except Exception:
                pass
        try:
            reg.add_token_to_grok2api_pools(
                sso, email=email, log_callback=lambda m: log(worker_id, m)
            )
        except Exception as exc:
            log(worker_id, f"[Debug] grok2api: {exc}")

        # Release / recycle register browser BEFORE mint so peak browsers ≈ R+M
        try:
            # Reuse register browser across accounts when enabled; otherwise quit.
            reuse = True
            try:
                reuse = bool(getattr(reg, "PERF_FLAGS", {}).get("browser_reuse", True))
            except Exception:
                reuse = True
            if reuse:
                reg.prepare_browser_for_next_account(log_callback=lambda m: log(worker_id, m))
            else:
                reg.stop_browser()
        except Exception:
            try:
                reg.stop_browser()
            except Exception:
                pass

        try:
            from grok_register.cpa_xai.proxyutil import get_runtime_proxy

            job_proxy = get_runtime_proxy() or ""
        except Exception:
            job_proxy = ""
        job = {
            "email": email,
            "password": password,
            "sso": sso,
            "profile": profile,
            "idx": idx,
            "cookies": cookies,
            "proxy": job_proxy,
        }

        if do_mint_inline:
            _run_mint_job(f"R{worker_id}", job, getattr(reg, "config", {}) or {})
        elif mint_queue is not None:
            # backpressure: wait while queue is saturated
            qmax = int(getattr(mint_queue, "_reg_qmax", 0) or 0)
            while qmax > 0 and mint_queue.qsize() >= qmax:
                log(worker_id, f"[cpa] mint 队列背压 qsize={mint_queue.qsize()}≥{qmax}，等待...")
                time.sleep(1.0)
            mint_queue.put(job)
            log(worker_id, f"[cpa] enqueued mint for {email} (queue≈{mint_queue.qsize()})")
        else:
            log(worker_id, "[cpa] mint skipped (no queue / inline)")

        _inc("reg_success")
        return job
    except Exception as exc:
        log(worker_id, f"! 注册失败: {exc}")
        try:
            from grok_register.cpa_xai.proxyutil import (
                disable_proxy,
                get_runtime_proxy,
                is_proxy_failure,
                proxy_log_label,
            )
            if is_proxy_failure(exc):
                cur = (get_runtime_proxy() or "").strip()
                if cur:
                    disable_proxy(cur, reason=str(exc)[:160])
                    log(worker_id, f"[proxy] disabled {proxy_log_label(cur)} (register_fail)")
        except Exception:
            pass
        reg.mark_error(email or "", reason=str(exc)[:120])
        traceback.print_exc()
        _inc("reg_fail")
        try:
            reg.restart_browser(log_callback=lambda m: log(worker_id, m))
        except Exception:
            pass
        return None


def _run_mint_job(worker_id: int | str, job: dict[str, Any], config: dict) -> dict:
    """Standalone CPA mint (own Chromium). Never reuses register browser.

    On mint failure, if proxy pool is enabled, rotate to a new random proxy and
    retry up to cpa_mint_proxy_retries times (default 3).
    """
    email = job.get("email") or ""
    password = job.get("password") or ""
    if not email or not password:
        _inc("mint_fail")
        return {"ok": False, "error": "missing email/password", "email": email}
    if not config.get("cpa_export_enabled", True):
        _inc("mint_skip")
        log(worker_id, f"[cpa] export disabled, skip {email}")
        return {"ok": False, "skipped": True, "email": email}

    try:
        from grok_register.cpa_xai.proxyutil import (
            ensure_pool_from_config,
            next_pool_proxy,
            proxy_log_label,
            set_runtime_proxy,
        )
        from grok_register import cpa_export
    except Exception as exc:
        _inc("mint_fail")
        log(worker_id, f"! CPA import failed: {exc}")
        return {"ok": False, "error": str(exc), "email": email}

    pool_on = bool(config.get("proxy_pool_enabled", False))
    mode = str(config.get("proxy_pool_mode") or "random").strip().lower() or "random"
    try:
        max_tries = int(config.get("cpa_mint_proxy_retries", 3) or 3)
    except Exception:
        max_tries = 3
    max_tries = max(1, min(max_tries, 8))
    if not pool_on:
        max_tries = 1

    if pool_on:
        ensure_pool_from_config(config)

    last_result: dict = {"ok": False, "error": "no attempt", "email": email}
    for attempt in range(1, max_tries + 1):
        # Proxy selection: first try job proxy (once), then always new pool proxy on retries.
        try:
            if attempt == 1 and str(job.get("proxy") or "").strip():
                jp = str(job.get("proxy") or "").strip()
                set_runtime_proxy(jp)
                log(worker_id, f"[proxy] mint pin {proxy_log_label(jp)} (try {attempt}/{max_tries})")
            elif pool_on:
                from grok_register.cpa_xai.proxyutil import (
                    is_pool_exhausted,
                    note_pool_exhausted_message,
                )

                p = next_pool_proxy(mode)
                if p:
                    log(
                        worker_id,
                        f"[proxy] mint assigned {proxy_log_label(p)} (try {attempt}/{max_tries})",
                    )
                else:
                    log(worker_id, note_pool_exhausted_message("mint"))
                    try:
                        config["proxy_pool_enabled"] = False
                    except Exception:
                        pass
                    pool_on = False
                    max_tries = attempt  # no more proxy rotations
                    set_runtime_proxy(None)
                    log(worker_id, "[proxy] mint continue direct (pool exhausted)")
            else:
                # single config proxy / none
                pass
        except Exception as exc:
            log(worker_id, f"[proxy] mint assign skip: {exc}")

        try:
            result = cpa_export.export_cpa_xai_for_account(
                email,
                password,
                page=None,
                cookies=job.get("cookies"),
                sso=job.get("sso") or "",
                config=config,
                log_callback=lambda m: log(worker_id, m),
            )
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "email": email}
            log(worker_id, f"[cpa] mint exception (try {attempt}/{max_tries}): {exc}")

        last_result = result if isinstance(result, dict) else {"ok": False, "error": str(result)}
        if last_result.get("ok"):
            log(worker_id, f"+ CPA auth: {last_result.get('path')}")
            _inc("mint_success")
            cloud_path = last_result.get("cpa_path") or last_result.get("path")
            if cloud_path and config.get("cpa_cloud_upload_enabled", False):
                _queue_cloud_upload(str(cloud_path), config)
                log(
                    worker_id,
                    f"[cloud-cpa] queued for batch upload: {Path(str(cloud_path)).name}",
                )
            # Optional: chenyme grok2api v3 Grok Build OAuth import (CPA xai-*.json)
            if cloud_path and config.get("grok2api_auto_add_build", False):
                try:
                    from grok_register import app as reg_app

                    reg_app.config.update(config)
                    reg_app.add_cpa_auth_to_grok2api_v3_build(
                        str(cloud_path),
                        log_callback=lambda m: log(worker_id, m),
                    )
                except Exception as g2a_exc:
                    log(worker_id, f"[Debug] grok2api Build 导入失败: {g2a_exc}")
            return last_result
        if last_result.get("skipped"):
            _inc("mint_skip")
            log(worker_id, f"[cpa] skipped: {last_result.get('reason')}")
            return last_result

        err = last_result.get("error") or last_result
        log(worker_id, f"! CPA auth 未成功 (try {attempt}/{max_tries}): {err}")
        # Disable dead/overloaded proxies (429 / CONNECT fail / etc.)
        try:
            from grok_register.cpa_xai.proxyutil import (
                disable_proxy,
                get_runtime_proxy,
                is_proxy_failure,
                proxy_log_label,
            )

            if is_proxy_failure(err):
                cur = (get_runtime_proxy() or "").strip()
                if cur:
                    disable_proxy(cur, reason=str(err)[:160])
                    log(
                        worker_id,
                        f"[proxy] disabled {proxy_log_label(cur)} (reason=proxy_fail)",
                    )
        except Exception as _px:
            log(worker_id, f"[proxy] disable skip: {_px}")
        if attempt < max_tries and pool_on:
            try:
                from grok_register.cpa_xai.proxyutil import is_pool_exhausted, pool_size

                if is_pool_exhausted() or pool_size() <= 0:
                    log(worker_id, "[proxy] pool exhausted after disable — retry direct once")
                    pool_on = False
                    try:
                        config["proxy_pool_enabled"] = False
                    except Exception:
                        pass
                    set_runtime_proxy(None)
                    time.sleep(min(0.5 * attempt, 2.0))
                    continue
            except Exception:
                pass
            log(worker_id, f"[proxy] mint fail → rotate proxy and retry")
            time.sleep(min(0.5 * attempt, 2.0))
            continue
        break

    _inc("mint_fail")
    return last_result



def _register_worker(
    worker_id: int,
    task_queue: queue.Queue,
    total: int,
    accounts_file: str,
    mint_queue: queue.Queue | None,
    forever: bool,
    do_mint_inline: bool,
):
    while True:
        try:
            idx = task_queue.get_nowait()
        except queue.Empty:
            if not forever:
                break
            with _next_idx_lock:
                nxt = _next_idx[0]
                _next_idx[0] = nxt + 5
            for i in range(nxt, nxt + 5):
                task_queue.put(i)
            continue

        retry = 0
        while retry < 2:
            try:
                result = register_one(
                    worker_id,
                    idx,
                    total,
                    accounts_file,
                    do_mint_inline=do_mint_inline,
                    mint_queue=mint_queue,
                )
                if result:
                    break
                retry += 1
                if retry < 2:
                    log(worker_id, f"[retry] 账号 {idx} 失败，重试 {retry}/1")
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass
            except Exception:
                retry += 1
                if retry < 2:
                    log(worker_id, f"[retry] 账号 {idx} 异常，重试 {retry}/1")
                    traceback.print_exc()
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass

        if retry >= 2:
            # register_one already counted fail on exception path
            pass

    # worker exit: free browser
    try:
        reg.stop_browser()
    except Exception:
        pass
    log(worker_id, "register worker exit")


def _mint_worker(worker_id: str, mint_queue: queue.Queue, config: dict):
    while True:
        job = mint_queue.get()
        try:
            if job is _MINT_STOP:
                break
            if not isinstance(job, dict):
                continue
            _run_mint_job(worker_id, job, config)
        finally:
            mint_queue.task_done()
    try:
        from grok_register.cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass
    log(worker_id, "mint worker exit")


def main() -> int:
    ensure_output_dir()
    parser = argparse.ArgumentParser(description="CLI runner for grok_register_ttk (pipelined).")
    parser.add_argument("--count", type=int, default=1, help="账号总数目标（0=不限；含已有）")
    parser.add_argument(
        "--extra",
        type=int,
        default=0,
        help="在已有 accounts 基础上再新注册 N 个",
    )
    parser.add_argument("--threads", type=int, default=1, help="注册并发线程数（1-10）")
    parser.add_argument(
        "--mint-workers",
        type=int,
        default=-1,
        help="CPA mint 并发：-1=用 config/auto；0=内联；1-10=固定。覆盖 config.cpa_mint_workers",
    )
    parser.add_argument(
        "--mint-queue-max",
        type=int,
        default=-1,
        help="mint 队列背压上限：-1=用 config/auto(2×workers)；0=不限制",
    )
    parser.add_argument("--accounts-file", default=str(OUTPUT_DIR / "accounts_cli.txt"))
    parser.add_argument("--fast", action="store_true", default=True, help="快速模式（默认开）：压缩 sleep、关截图")
    parser.add_argument("--no-fast", action="store_true", help="关闭快速模式")
    parser.add_argument("--no-browser-reuse", action="store_true", help="每号强制 quit 浏览器")
    parser.add_argument("--browser-recycle-every", type=int, default=25, help="复用 N 次后完整回收")
    parser.add_argument("--cookie-snapshot", action="store_true", help="注册成功写 cookie 快照（默认关，fast）")
    parser.add_argument("--inline-mint", action="store_true", help="强制注册线程内联 mint（调试用）")
    parser.add_argument(
        "--mint-backend",
        choices=["protocol", "browser", "auto"],
        default=None,
        help="CPA mint 后端：protocol=无浏览器SSO协议；browser=Chrome设备码；auto=有SSO先协议",
    )
    args = parser.parse_args()

    reg.load_config()
    cfg0 = getattr(reg, "config", {}) or {}
    if args.mint_backend:
        cfg0["cpa_mint_backend"] = args.mint_backend
        try:
            reg.config["cpa_mint_backend"] = args.mint_backend
        except Exception:
            pass
    threads = max(1, min(args.threads, 10))
    fast = bool(args.fast) and not bool(args.no_fast)

    mint_workers = resolve_mint_workers(
        cli_value=args.mint_workers,
        threads=threads,
        config=cfg0,
        inline_mint=bool(args.inline_mint),
    )
    do_mint_inline = mint_workers == 0
    mint_qmax = resolve_mint_queue_max(
        cfg0,
        mint_workers,
        cli_value=(None if args.mint_queue_max < 0 else args.mint_queue_max),
    )

    # perf knobs
    reg.configure_perf(
        fast=fast,
        sleep_scale=0.15 if fast else 1.0,
        skip_debug_io=fast,
        cookie_snapshot=bool(args.cookie_snapshot) or not fast,
        async_side_effects=True,
        browser_reuse=not args.no_browser_reuse,
        browser_recycle_every=max(1, int(args.browser_recycle_every)),
    )

    # 断点续跑
    done_count = 0
    if os.path.exists(args.accounts_file):
        with open(args.accounts_file) as f:
            done_count = sum(1 for line in f if line.strip())

    if args.extra and args.extra > 0:
        target_total = done_count + args.extra
        remaining = args.extra
        print(
            f"[*] 配置加载完成，额外新注册 {args.extra} 个（当前已有 {done_count} → 目标 {target_total}），"
            f"注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
        args.count = target_total
    elif args.count == 0:
        remaining = None
        print(
            f"[*] 配置加载完成，不限数量，注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    else:
        remaining = max(0, args.count - done_count)
        print(
            f"[*] 配置加载完成，目标 {args.count} 个账号，注册线程={threads} "
            f"mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    print(f"[*] accounts_file = {args.accounts_file}", flush=True)
    try:
        from grok_register.cpa_xai.proxyutil import ensure_pool_from_config, proxy_log_label

        cfg0 = getattr(reg, "config", {}) or {}
        if cfg0.get("proxy_pool_enabled"):
            n = ensure_pool_from_config(cfg0)
            print(
                f"[*] proxy_pool enabled file={cfg0.get('proxy_pool_file')} "
                f"size={n} mode={cfg0.get('proxy_pool_mode', 'round_robin')}",
                flush=True,
            )
        elif (cfg0.get("proxy") or "").strip():
            print(f"[*] proxy={proxy_log_label(str(cfg0.get('proxy')))}", flush=True)
    except Exception as exc:
        print(f"[*] proxy pool init: {exc}", flush=True)
    if done_count > 0:
        print(f"[*] 断点续跑：已完成 {done_count}", flush=True)
    if remaining is not None and remaining <= 0:
        print("[*] 所有账号已完成，无需继续（可用 --extra N 再注册）", flush=True)
        return 0

    log_thread = threading.Thread(target=_log_writer, daemon=True)
    log_thread.start()

    try:
        reg.TabPool.init(reg.create_browser_options, log_callback=lambda m: log(0, m))
    except Exception as exc:
        print(f"[!] 浏览器初始化失败: {exc}", flush=True)
        return 1

    task_queue: queue.Queue = queue.Queue()
    mint_queue: queue.Queue | None = queue.Queue() if not do_mint_inline else None
    if mint_queue is not None:
        mint_queue._reg_qmax = mint_qmax  # type: ignore[attr-defined]
    global _next_idx
    _next_idx[0] = done_count + 1
    if remaining is not None:
        for i in range(done_count + 1, args.count + 1):
            task_queue.put(i)
    else:
        for i in range(done_count + 1, done_count + threads * 5 + 1):
            task_queue.put(i)
        _next_idx[0] = done_count + threads * 5 + 1

    forever = remaining is None
    cfg = getattr(reg, "config", {}) or {}

    # mint workers first (so queue consumers ready)
    mint_threads: list[threading.Thread] = []
    if mint_queue is not None and mint_workers > 0:
        for i in range(1, mint_workers + 1):
            wid = f"M{i}"
            t = threading.Thread(
                target=_mint_worker,
                args=(wid, mint_queue, cfg),
                daemon=True,
                name=f"mint-{i}",
            )
            t.start()
            mint_threads.append(t)

    reg_threads: list[threading.Thread] = []
    for wid in range(1, threads + 1):
        t = threading.Thread(
            target=_register_worker,
            args=(wid, task_queue, args.count, args.accounts_file, mint_queue, forever, do_mint_inline),
            daemon=True,
            name=f"reg-{wid}",
        )
        t.start()
        reg_threads.append(t)

    try:
        for t in reg_threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[!] 用户中断", flush=True)

    # drain mint queue
    if mint_queue is not None:
        log(0, f"[cpa] 等待 mint 队列清空（qsize≈{mint_queue.qsize()}）...")
        mint_queue.join()
        for _ in mint_threads:
            mint_queue.put(_MINT_STOP)
        for t in mint_threads:
            t.join(timeout=600)

    # All CPA local files first, then one batch cloud upload.
    try:
        _flush_cloud_uploads(cfg)
    except Exception as upload_exc:
        log(0, f"[cloud-cpa] batch upload exception: {upload_exc}")

    try:
        reg.shutdown_browser()
    except Exception:
        pass
    try:
        from grok_register.cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass
    # Last resort: kill leftover DrissionPage/automation Chrome orphans.
    try:
        reg.kill_orphaned_automation_browsers(log_callback=lambda m: log(0, m))
    except Exception as kill_exc:
        log(0, f"[browser] orphan cleanup skipped: {kill_exc}")

    # stop side-effect pool
    try:
        pool = getattr(reg, "_side_effect_pool", None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

    _log_queue.put(None)
    log_thread.join(timeout=2)

    with _stats_lock:
        s = dict(_stats)
    print(
        f"=== 完成: 注册成功 {s.get('reg_success', 0)}, 注册失败 {s.get('reg_fail', 0)}, "
        f"CPA成功 {s.get('mint_success', 0)}, CPA失败 {s.get('mint_fail', 0)}, "
        f"CPA跳过 {s.get('mint_skip', 0)}, "
        f"云上传成功 {s.get('cloud_upload_success', 0)}, "
        f"云上传跳过 {s.get('cloud_upload_skip', 0)}, "
        f"云上传失败 {s.get('cloud_upload_fail', 0)} ===",
        flush=True,
    )
    return 0 if s.get("reg_success", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
