from pathlib import Path
import py_compile

# ---------- register_cli.py ----------
p = Path("register_cli.py")
text = p.read_text(encoding="utf-8")

old = '''def _ensure_browser(worker_id: int, force_recycle: bool = False):
    """Start browser if missing; optional full recycle."""
    if force_recycle:
        try:
            reg.stop_browser()
        except Exception:
            pass
    if reg.TabPool.get_browser() is None:
        reg.start_browser(log_callback=lambda m: log(worker_id, m))
'''
new = '''def _ensure_browser(worker_id: int, force_recycle: bool = False):
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
'''
if old not in text:
    raise SystemExit("ensure_browser block missing")
text = text.replace(old, new, 1)

old = '''            reg.prepare_browser_for_next_account(log_callback=lambda m: log(worker_id, m))
        except Exception:
            try:
                reg.stop_browser()
            except Exception:
                pass
'''
new = '''            # Reuse register browser across accounts when enabled; otherwise quit.
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
'''
if old not in text:
    raise SystemExit("prepare_browser block missing")
text = text.replace(old, new, 1)

old = '''    try:
        reg.shutdown_browser()
    except Exception:
        pass
    try:
        from cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass
'''
new = '''    try:
        reg.shutdown_browser()
    except Exception:
        pass
    try:
        from cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass
    # Last resort: kill leftover DrissionPage/automation Chrome orphans.
    try:
        reg.kill_orphaned_automation_browsers(log_callback=lambda m: log(0, m))
    except Exception as kill_exc:
        log(0, f"[browser] orphan cleanup skipped: {kill_exc}")
'''
if old not in text:
    raise SystemExit("shutdown cleanup block missing")
text = text.replace(old, new, 1)
p.write_text(text, encoding="utf-8")
print("register_cli patched")

# ---------- grok_register_ttk.py ----------
p2 = Path("grok_register_ttk.py")
t2 = p2.read_text(encoding="utf-8")

old = '''def start_browser(log_callback=None):
    global browser, page
    last_exc = None
    for attempt in range(1, 5):
        try:
            try:
                opts = create_browser_options(unique_profile=True, profile_tag="reg")
            except TypeError:
                opts = create_browser_options()
            browser = Chromium(opts)
'''
new = '''def start_browser(log_callback=None):
    global browser, page
    # Never leave a previous thread-local Chromium alive when opening a new one.
    try:
        existing = _tls_get_browser()
    except Exception:
        existing = browser
    if existing is not None:
        try:
            stop_browser()
        except Exception:
            pass
    last_exc = None
    for attempt in range(1, 5):
        try:
            try:
                opts = create_browser_options(unique_profile=True, profile_tag="reg")
            except TypeError:
                opts = create_browser_options()
            browser = Chromium(opts)
'''
if old not in t2:
    raise SystemExit("start_browser header missing")
t2 = t2.replace(old, new, 1)

old = '''def stop_browser():
    global browser, page
    _bind_thread_browser_globals()
    if browser is not None:
        try:
            browser.quit(del_data=True)
        except Exception:
            pass
        _unregister_thread_browser(browser)
    browser = None
    page = None
    _sync_thread_browser_globals()
'''
new = '''def stop_browser():
    global browser, page
    _bind_thread_browser_globals()
    if browser is not None:
        try:
            browser.quit(del_data=True)
        except TypeError:
            try:
                browser.quit()
            except Exception:
                pass
        except Exception:
            try:
                browser.quit()
            except Exception:
                pass
        _unregister_thread_browser(browser)
    browser = None
    page = None
    _sync_thread_browser_globals()
'''
if old not in t2:
    raise SystemExit("stop_browser missing")
t2 = t2.replace(old, new, 1)

old = '''def shutdown_browser():
    stop_browser()
    with _browser_registry_lock:
        browsers = list(_all_thread_browsers)
        _all_thread_browsers.clear()
    for b in browsers:
        try:
            b.quit(del_data=True)
        except Exception:
            try:
                b.quit()
            except Exception:
                pass
'''
new = '''def shutdown_browser():
    stop_browser()
    with _browser_registry_lock:
        browsers = list(_all_thread_browsers)
        _all_thread_browsers.clear()
    for b in browsers:
        try:
            b.quit(del_data=True)
        except Exception:
            try:
                b.quit()
            except Exception:
                pass
    try:
        from cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass


def kill_orphaned_automation_browsers(log_callback=None):
    """Kill leftover Chrome processes started by DrissionPage / this project.

    Only matches automation fingerprints: autoPortData / grok_reg_chrome /
    DrissionPage temp profiles. Normal user Chrome is left alone.
    """
    log = log_callback or (lambda m: None)
    try:
        import subprocess
    except Exception:
        return 0

    if os.name != "nt":
        try:
            subprocess.run(
                ["pkill", "-f", "autoPortData|grok_reg_chrome|DrissionPage"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        return 0

    ps = (
        "$patterns = @('autoPortData','grok_reg_chrome','DrissionPage'); "
        "$killed = 0; "
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | ForEach-Object { "
        "  $cmd = $_.CommandLine; if (-not $cmd) { return }; "
        "  foreach ($p in $patterns) { if ($cmd -match $p) { "
        "    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch {}; break "
        "  } } "
        "}; Write-Output $killed"
    )
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        n = int(out.splitlines()[-1]) if out else 0
        if n:
            log(f"[browser] killed orphaned automation chrome processes: {n}")
        else:
            log("[browser] no orphaned automation chrome found")
        return n
    except Exception as exc:
        log(f"[browser] orphan kill failed: {exc}")
        return 0
'''
if old not in t2:
    raise SystemExit("shutdown_browser missing")
t2 = t2.replace(old, new, 1)

# Align TabPool with TLS after configure_perf
if "TabPool.get_browser = classmethod" not in t2:
    anchor = "    return PERF_FLAGS\n"
    idx = t2.find("def configure_perf(")
    if idx < 0:
        raise SystemExit("configure_perf not found")
    idx2 = t2.find(anchor, idx)
    if idx2 < 0:
        raise SystemExit("PERF_FLAGS return not found")
    idx2 += len(anchor)
    bind = '''
# Align TabPool with thread-local browsers so register_cli reuse checks work.
try:
    TabPool.get_browser = classmethod(lambda cls: _tls_get_browser())  # type: ignore
    TabPool.get_tab = classmethod(lambda cls, url=None: _tls_get_page())  # type: ignore
    TabPool.release_tab = classmethod(lambda cls: stop_browser())  # type: ignore
    TabPool.shutdown = classmethod(lambda cls: shutdown_browser())  # type: ignore
    TabPool.clear_session = classmethod(  # type: ignore
        lambda cls, log_callback=None: prepare_browser_for_next_account(log_callback=log_callback)
    )
except Exception:
    pass
'''
    t2 = t2[:idx2] + bind + t2[idx2:]
    print("TabPool aligned")

p2.write_text(t2, encoding="utf-8")
print("grok_register_ttk patched")

py_compile.compile("register_cli.py", doraise=True)
py_compile.compile("grok_register_ttk.py", doraise=True)
print("compile ok")
