"""CLI wrapper for grok_register — multi-thread register + optional grok2api upload.

Architecture:
  Register workers (R)  →  accounts_cli.txt + optional grok2api Web/Build pool

Browser lifecycle:
  - One Chromium per register worker, reused via TabPool.clear_session
  - Full recycle every N accounts or on error
"""
from __future__ import annotations

import argparse
import os
import queue
import signal
import sys
import threading
import time
import traceback
from grok_register import app as reg  # noqa: E402
from grok_register.paths import OUTPUT_DIR, ensure_output_dir


# Camoufox 适配: humanize 光标 + disable_coop（便于原生点击 Turnstile iframe）
# 不再加载 turnstilePatch 扩展，也不再绑定系统 Chrome channel
_orig_create_browser_options = reg.create_browser_options


def _patched_create_browser_options(*args, **kwargs):
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
        from grok_register.browser_adapter import ChromiumOptions

        opts = ChromiumOptions()
        opts.auto_port()
        opts.set_timeouts(base=1)

    try:
        opts.auto_port()
    except Exception:
        pass
    try:
        opts.set_timeouts(base=1)
    except Exception:
        pass
    try:
        opts.set_humanize(True)
        opts.set_disable_coop(True)
        opts.set_os("windows")
    except Exception:
        pass
    return opts


reg.create_browser_options = _patched_create_browser_options


# ── 线程安全日志 ──

_log_queue: queue.Queue = queue.Queue()


def _log_writer():
    while not _cancel_event.is_set():
        try:
            msg = _log_queue.get(timeout=0.3)
            if msg is None:
                break
            print(msg, flush=True)
        except queue.Empty:
            continue


def log(worker_id: int | str, msg: str) -> None:
    _log_queue.put(f"[{time.strftime('%H:%M:%S')}] [W{worker_id}] {msg}")


# ── 统计 ──

_stats_lock = threading.Lock()
_stats = {
    "reg_success": 0,
    "reg_fail": 0,
}


def _inc(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


# forever 任务索引
_next_idx_lock = threading.Lock()
_next_idx = [1]

# Ctrl+C 取消事件 — 任何阻塞循环都应检查此事件
_cancel_event = threading.Event()


def _setup_signal_handler():
    """注册 SIGINT 处理器，使 Ctrl+C 可靠地停止所有线程。"""
    def _handler(sig_num, frame):
        _cancel_event.set()
        # 用 os.write 避免 print 在信号处理器中可能的死锁
        try:
            sys.stderr.write("\n[!] 正在停止...（再次按 Ctrl+C 强制结束）\n")
            sys.stderr.flush()
        except Exception:
            pass
    try:
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        pass


class DummyStop:
    def __call__(self) -> bool:
        return False


def _ensure_browser(worker_id: int, force_recycle: bool = False):
    """Start browser if missing; optional full recycle.

    Use thread-local browser from grok_register.app. TabPool had a separate
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
) -> dict | None:
    """Run one registration; write accounts file and optional grok2api upload.

    Returns dict(email, sso, profile) or None.
    """
    email = ""
    dev_token = ""
    max_mail_retry = 3
    cancel = DummyStop()
    attempt_finished = False

    try:
        from grok_register.reg_stats import (
            abandon_attempt,
            begin_attempt,
            finish_attempt,
            update_attempt,
        )
        from grok_register.proxyutil import get_runtime_proxy
    except Exception:
        abandon_attempt = None  # type: ignore
        begin_attempt = None  # type: ignore
        finish_attempt = None  # type: ignore
        update_attempt = None  # type: ignore
        get_runtime_proxy = None  # type: ignore

    def _stats_begin() -> None:
        if not begin_attempt:
            return
        try:
            proxy = ""
            try:
                proxy = (get_runtime_proxy() or "") if get_runtime_proxy else ""
            except Exception:
                proxy = ""
            begin_attempt(
                worker_id=worker_id,
                idx=idx,
                user_agent=str(reg.config.get("user_agent", "") or ""),
                proxy=proxy,
            )
        except Exception:
            pass

    def _stats_finish(outcome: str, **kwargs) -> None:
        nonlocal attempt_finished
        if attempt_finished or not finish_attempt:
            return
        try:
            finish_attempt(outcome, **kwargs)
            attempt_finished = True
        except Exception:
            pass

    def _stats_abandon() -> None:
        nonlocal attempt_finished
        if attempt_finished:
            return
        if abandon_attempt:
            try:
                abandon_attempt()
            except Exception:
                pass

    # Pin a proxy for this account/thread before browser start.
    try:
        reg.assign_thread_proxy(lambda m: log(worker_id, m), force_new=True)
    except Exception as exc:
        log(worker_id, f"[proxy] assign failed: {exc}")

    _stats_begin()

    try:
        return _register_one_body(
            worker_id=worker_id,
            idx=idx,
            total=total,
            accounts_file=accounts_file,
            cancel=cancel,
            max_mail_retry=max_mail_retry,
            update_attempt=update_attempt,
            get_runtime_proxy=get_runtime_proxy,
            stats_finish=_stats_finish,
        )
    finally:
        _stats_abandon()


def _register_one_body(
    *,
    worker_id: int,
    idx: int,
    total: int,
    accounts_file: str,
    cancel,
    max_mail_retry: int,
    update_attempt,
    get_runtime_proxy,
    stats_finish,
) -> dict | None:
    email = ""
    dev_token = ""

    try:
        _ensure_browser(worker_id, force_recycle=False)
    except Exception as exc:
        log(worker_id, f"! 浏览器启动失败: {exc}")
        stats_finish("error", reason="browser_start_failed", error=str(exc)[:400])
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
            if update_attempt:
                try:
                    proxy = ""
                    try:
                        proxy = (get_runtime_proxy() or "") if get_runtime_proxy else ""
                    except Exception:
                        pass
                    update_attempt(email=email, proxy=proxy)
                except Exception:
                    pass
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
                from grok_register.proxyutil import (
                    disable_proxy,
                    get_runtime_proxy as _grp,
                    is_proxy_failure,
                    proxy_log_label,
                )
                if is_proxy_failure(msg):
                    cur = (_grp() or "").strip()
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
            stats_finish("error", reason="email_stage", error=msg[:400])
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
        page = reg._get_page()
        if page and reg.PERF_FLAGS.get("cookie_snapshot", True):
            try:
                reg.save_cookies_snapshot(page, "success", email)
            except Exception:
                pass

        pool = reg.apply_post_register_pools(
            sso,
            email=email,
            log_callback=lambda m: log(worker_id, m),
            page=page,
        )

        try:
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

        if not pool.get("ok", True):
            log(
                worker_id,
                f"! 注册失败: bot_flag_source=1 ({email})，未导入 Web/Build"
                "（可设 allow_bot_flagged=true 强制继续）",
            )
            reg.mark_error(email or "", reason="bot_flag_source=1")
            _inc("reg_fail")
            stats_finish(
                "bot_flag",
                reason="bot_flag_source=1",
                bot_flagged=True,
                access_token=(pool.get("build_seed") or {}).get("access_token"),
            )
            return None

        line = f"{email}----{password}----{sso}\n"
        os.makedirs(os.path.dirname(os.path.abspath(accounts_file)) or ".", exist_ok=True)
        with open(accounts_file, "a", encoding="utf-8") as f:
            f.write(line)
        if pool.get("bot_flagged"):
            log(worker_id, f"+ 注册成功(bot标记已允许): {email}")
        else:
            log(worker_id, f"+ 注册成功: {email}")
        reg.mark_used(email, password)

        job = {
            "email": email,
            "password": password,
            "sso": sso,
            "profile": profile,
            "idx": idx,
            "build": bool(pool.get("build_seed")),
            "skipped_web": bool(pool.get("skipped_web")),
        }

        _inc("reg_success")
        stats_finish(
            "success",
            reason="bot_flag_allowed" if pool.get("bot_flagged") else "",
            bot_flagged=bool(pool.get("bot_flagged")),
            access_token=(pool.get("build_seed") or {}).get("access_token"),
        )
        return job
    except Exception as exc:
        log(worker_id, f"! 注册失败: {exc}")
        try:
            from grok_register.proxyutil import (
                disable_proxy,
                get_runtime_proxy as _grp2,
                is_proxy_failure,
                proxy_log_label,
            )
            if is_proxy_failure(exc):
                cur = (_grp2() or "").strip()
                if cur:
                    disable_proxy(cur, reason=str(exc)[:160])
                    log(worker_id, f"[proxy] disabled {proxy_log_label(cur)} (register_fail)")
        except Exception:
            pass
        reg.mark_error(email or "", reason=str(exc)[:120])
        traceback.print_exc()
        _inc("reg_fail")
        stats_finish("error", reason=str(exc)[:200], error=str(exc)[:400])
        try:
            reg.restart_browser(log_callback=lambda m: log(worker_id, m))
        except Exception:
            pass
        return None


def _register_worker(
    worker_id: int,
    task_queue: queue.Queue,
    total: int,
    accounts_file: str,
    forever: bool,
):
    while not _cancel_event.is_set():
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

    # worker exit: free browser + this thread's Playwright driver
    try:
        reg.stop_browser()
    except Exception:
        pass
    try:
        from grok_register.browser_adapter import stop_thread_playwright

        stop_thread_playwright()
    except Exception:
        pass
    log(worker_id, "register worker exit")


def main() -> int:
    ensure_output_dir()
    _setup_signal_handler()
    parser = argparse.ArgumentParser(description="CLI runner for grok_register (register + grok2api).")
    parser.add_argument("--count", type=int, default=1, help="账号总数目标（0=不限；含已有）")
    parser.add_argument(
        "--extra",
        type=int,
        default=0,
        help="在已有 accounts 基础上再新注册 N 个",
    )
    parser.add_argument("--threads", type=int, default=1, help="注册并发线程数（1-10）")
    parser.add_argument("--accounts-file", default=str(OUTPUT_DIR / "accounts_cli.txt"))
    parser.add_argument("--fast", action="store_true", default=True, help="快速模式（默认开）：压缩 sleep、关截图")
    parser.add_argument("--no-fast", action="store_true", help="关闭快速模式")
    parser.add_argument("--no-browser-reuse", action="store_true", help="每号强制 quit 浏览器")
    parser.add_argument("--browser-recycle-every", type=int, default=25, help="复用 N 次后完整回收")
    parser.add_argument("--cookie-snapshot", action="store_true", help="注册成功写 cookie 快照（默认关，fast）")
    args = parser.parse_args()

    reg.load_config()
    cfg0 = getattr(reg, "config", {}) or {}
    threads = max(1, min(args.threads, 10))
    fast = bool(args.fast) and not bool(args.no_fast)

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
            f"注册线程={threads} fast={fast}",
            flush=True,
        )
        args.count = target_total
    elif args.count == 0:
        remaining = None
        print(
            f"[*] 配置加载完成，不限数量，注册线程={threads} fast={fast}",
            flush=True,
        )
    else:
        remaining = max(0, args.count - done_count)
        print(
            f"[*] 配置加载完成，目标 {args.count} 个账号，注册线程={threads} fast={fast}",
            flush=True,
        )
    print(f"[*] accounts_file = {args.accounts_file}", flush=True)
    g2a_remote = bool(cfg0.get("grok2api_auto_add_remote"))
    g2a_build = bool(cfg0.get("grok2api_auto_add_build"))
    local_build = bool(cfg0.get("local_build_device_flow"))
    if g2a_remote or g2a_build or local_build:
        print(
            f"[*] grok2api: remote_web={g2a_remote} remote_build={g2a_build} "
            f"local_device_flow={local_build} mode=browser "
            f"base={cfg0.get('grok2api_remote_base') or '(empty)'}",
            flush=True,
        )
    try:
        from grok_register.proxyutil import ensure_pool_from_config, proxy_log_label

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

    reg_threads: list[threading.Thread] = []
    for wid in range(1, threads + 1):
        t = threading.Thread(
            target=_register_worker,
            args=(wid, task_queue, args.count, args.accounts_file, forever),
            daemon=True,
            name=f"reg-{wid}",
        )
        t.start()
        reg_threads.append(t)
        # Stagger first Camoufox launches across workers (Windows process races).
        if threads > 1 and wid < threads:
            time.sleep(0.8)

    # 轮询等待注册线程完成，同时响应 Ctrl+C 取消
    try:
        while not _cancel_event.is_set():
            alive = [t for t in reg_threads if t.is_alive()]
            if not alive:
                break
            for t in alive:
                t.join(timeout=0.3)
    except KeyboardInterrupt:
        _cancel_event.set()
        print("\n[!] 用户中断", flush=True)

    if _cancel_event.is_set():
        print("[!] 取消中...", flush=True)

    try:
        reg.shutdown_browser()
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
        f"=== 完成: 注册成功 {s.get('reg_success', 0)}, 注册失败 {s.get('reg_fail', 0)} ===",
        flush=True,
    )
    return 0 if s.get("reg_success", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
