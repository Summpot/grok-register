"""
Patchright adapter — drop-in compatibility layer for DrissionPage API.

Usage (replace in app.py / browser_confirm.py / tab_pool.py):
    OLD: from DrissionPage import Chromium, ChromiumOptions
    NEW: from grok_register.browser_adapter import Chromium, ChromiumOptions

The adapter wraps Patchright (patched Playwright) and exposes the
DrissionPage surface used by this project so that the bulk of the
registration and CPA-mint logic stays unchanged.

Key mapping:
    ChromiumOptions   → dict of launch args (auto_port, set_argument, …)
    Chromium          → PatchrightBrowser  (browser + context + page pool)
    Tab / Page        → PatchrightPage     (individual tab with run_js, ele, …)
    ChromiumElement   → PatchrightElement  (DOM node with click, attr, text, …)
"""

from __future__ import annotations

import atexit
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# ── Playwright singleton ──────────────────────────────────────────────
_pw = None
_pw_lock = threading.Lock()


def _get_playwright():
    global _pw
    if _pw is not None:
        return _pw
    with _pw_lock:
        if _pw is not None:
            return _pw
        from patchright.sync_api import sync_playwright

        _pw = sync_playwright().start()
        atexit.register(_pw.stop)
        return _pw


# ── Error compatibility ───────────────────────────────────────────────
class PageDisconnectedError(Exception):
    pass


class _NoSuchElementError(Exception):
    pass


# ── ChromiumOptions compatibility ─────────────────────────────────────
class ChromiumOptions:
    """Drop-in replacement for DrissionPage's ChromiumOptions.

    Collects flags / proxy / extensions / user-data-dir / headless settings
    and exposes them as a plain dict that the Chromium() factory later
    translates into Patchright launch_persistent_context() arguments.
    """

    def __init__(self):
        self._flags: list[str] = []
        self._proxy: str | None = None
        self._extensions: list[str] = []
        self._user_data_path: str | None = None
        self._headless: bool | None = None
        self._timeout_base: int = 2
        self._address: str = ""
        self._browser_path: str | None = None

    def auto_port(self):
        return self

    def set_timeouts(self, base: int = 2):
        self._timeout_base = base
        return self

    def set_argument(self, flag: str):
        self._flags.append(flag)
        return self

    def set_proxy(self, proxy: str):
        self._proxy = proxy
        return self

    def add_extension(self, path: str):
        self._extensions.append(path)
        return self

    def set_user_data_path(self, path: str):
        self._user_data_path = path
        return self

    def set_paths(self, *, user_data_path: str | None = None):
        if user_data_path:
            self._user_data_path = user_data_path
        return self

    def headless(self, on: bool = True):
        self._headless = on
        return self

    def set_browser_path(self, path: str):
        self._browser_path = path
        return self

    @property
    def address(self) -> str:
        return self._address

    def to_launch_kwargs(self) -> dict[str, Any]:
        """Return keyword arguments for `chromium.launch_persistent_context()`."""
        kw: dict[str, Any] = {}

        if self._headless is not None:
            kw["headless"] = self._headless

        if self._browser_path:
            kw["executable_path"] = self._browser_path

        if self._user_data_path:
            os.makedirs(self._user_data_path, exist_ok=True)
            kw["user_data_dir"] = self._user_data_path
        else:
            profile_dir = os.path.join(
                tempfile.gettempdir(),
                "grok_reg_chrome",
                f"patchright_{os.getpid()}_{uuid.uuid4().hex[:10]}",
            )
            os.makedirs(profile_dir, exist_ok=True)
            kw["user_data_dir"] = profile_dir

        args: list[str] = []

        for flag in self._flags:
            f = flag.strip()
            if not f.startswith("--"):
                continue
            if f.startswith("--remote-debugging-port="):
                continue
            if f.startswith("--window-position="):
                continue
            if f.startswith("--window-size="):
                continue
            if f.startswith("--user-data-dir="):
                continue
            args.append(f)

        defaults = [
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
        ]
        for d in defaults:
            if d not in args:
                args.append(d)

        for ext_path in self._extensions:
            if os.path.isdir(ext_path):
                args.append(f"--disable-extensions-except={ext_path}")
                args.append(f"--load-extension={ext_path}")

        kw["args"] = args

        if self._proxy:
            kw["proxy"] = {"server": self._proxy}

        kw.setdefault("no_viewport", True)

        return kw


# ── PatchrightElement wrapper ─────────────────────────────────────────
class PatchrightElement:
    """Wraps a Playwright JSHandle / ElementHandle with DrissionPage-like API.

    If `is_shadow_host=True`, the element represents a shadow-root of the
    host element `handle`.  Operations like `ele()` and `run_js()` will
    re-derive the shadowRoot from the host at call time via the page
    context instead of holding a stale JSHandle.
    """

    def __init__(self, handle: Any, page: Any | None = None, *, is_shadow_host: bool = False):
        self._h = handle
        self._page = page
        self._is_shadow = is_shadow_host

    def _shadow_query(self, css_selector: str) -> Any | None:
        if not self._page:
            return None
        try:
            return self._page.evaluate_handle(
                f"(host) => host.shadowRoot ? host.shadowRoot.querySelector({css_selector!r}) : null",
                self._h,
            )
        except Exception:
            return None

    def _shadow_eval(self, js_code: str) -> Any:
        if not self._page:
            return None
        try:
            sr_handle = self._page.evaluate_handle(
                "(host) => host.shadowRoot",
                self._h,
            )
            if not sr_handle:
                return None
            return self._page.evaluate(
                f"""(sr) => {{
                    if (!sr) return null;
                    {js_code}
                }}""",
                sr_handle,
            )
        except Exception:
            return None

    @property
    def text(self) -> str:
        try:
            return self._h.inner_text()
        except Exception:
            try:
                return self._h.evaluate("el => el.innerText || el.textContent || ''")
            except Exception:
                return ""

    def attr(self, name: str) -> str | None:
        try:
            return self._h.get_attribute(name)
        except Exception:
            try:
                return self._h.evaluate(f"el => el.getAttribute({name!r})")
            except Exception:
                return None

    def click(self, by_js: bool = False):
        if by_js:
            self._h.evaluate("el => el.click()")
        else:
            try:
                self._h.click()
            except Exception:
                self._h.evaluate("el => el.click()")

    @property
    def shadow_root(self) -> PatchrightElement | None:
        try:
            has_sr = self._h.evaluate("el => !!el.shadowRoot")
            if has_sr:
                return PatchrightElement(self._h, self._page, is_shadow_host=True)
        except Exception:
            pass
        return None

    def parent(self) -> PatchrightElement | None:
        try:
            p = self._h.evaluate_handle("el => el.parentElement")
            if p:
                return PatchrightElement(p, self._page)
        except Exception:
            pass
        return None

    def ele(self, selector: str) -> PatchrightElement | None:
        css = _dp_sel_to_css(selector)
        if self._is_shadow:
            child = self._shadow_query(css)
            if child:
                try:
                    has_tag = child.evaluate("el => el ? !!el.tagName : false")
                    if has_tag:
                        return PatchrightElement(child, self._page)
                except Exception:
                    pass
            return None
        try:
            child = self._h.query_selector(css)
            if child:
                return PatchrightElement(child, self._page)
        except Exception:
            pass
        if self._page:
            try:
                child = self._page.evaluate_handle(
                    f"""(root) => root.querySelector({css!r})""",
                    self._h,
                )
                if child:
                    try:
                        has_tag = child.evaluate("el => el ? el.tagName : null")
                        if has_tag:
                            return PatchrightElement(child, self._page)
                    except Exception:
                        pass
            except Exception:
                pass
        return None

    def run_js(self, js_code: str, *args) -> Any:
        if self._is_shadow:
            return self._shadow_eval(js_code)
        try:
            tag = self._h.evaluate("el => el.tagName")
            if tag and tag.upper() == "IFRAME":
                frame = self._h.content_frame()
                if frame:
                    return _eval_js(frame, js_code, *args)
        except Exception:
            pass
        try:
            owner = self._h.owner_frame()
            if owner:
                return _eval_js(owner, js_code, *args)
        except Exception:
            pass
        if self._page:
            return _eval_js(self._page, js_code, *args)
        return _eval_js(self._h, js_code, *args)

    @property
    def scroll(self):
        class _Scroll:
            def __init__(self, el):
                self._el = el
            def to_see(self):
                try:
                    self._el.scroll_into_view_if_needed()
                except Exception:
                    self._el.evaluate("el => el.scrollIntoView({block: 'center'})")
        return _Scroll(self._h)


# ── PatchrightPage wrapper ────────────────────────────────────────────
class PatchrightPage:
    def __init__(self, pw_page: Any, pw_context: Any | None = None):
        self._p = pw_page
        self._ctx = pw_context

    @property
    def url(self) -> str:
        return self._p.url

    @property
    def html(self) -> str:
        return self._p.content()

    def get(self, url: str, timeout: float | None = None, **kwargs):
        ms = int(timeout * 1000) if timeout else 30000
        try:
            self._p.goto(url, timeout=ms, **kwargs)
        except TypeError:
            self._p.goto(url)

    @property
    def wait(self):
        class _PageWaiter:
            def __init__(self, p):
                self._p = p
            def doc_loaded(self):
                self._p.wait_for_load_state("domcontentloaded")
            def __call__(self):
                self._p.wait_for_load_state("domcontentloaded")
        return _PageWaiter(self._p)

    def run_js(self, js_code: str, *args) -> Any:
        return _eval_js(self._p, js_code, *args)

    def ele(self, selector: str, timeout: float | None = None) -> PatchrightElement | None:
        css = _dp_sel_to_css(selector)
        try:
            if timeout and timeout > 0:
                ms = int(timeout * 1000)
                el = self._p.wait_for_selector(css, timeout=ms)
            else:
                el = self._p.query_selector(css)
            if el:
                return PatchrightElement(el, self._p)
        except Exception:
            pass
        return None

    def eles(self, selector: str) -> list[PatchrightElement]:
        css = _dp_sel_to_css(selector)
        try:
            els = self._p.query_selector_all(css)
            return [PatchrightElement(el, self._p) for el in els]
        except Exception:
            return []

    def close(self):
        try:
            self._p.close()
        except Exception:
            pass

    def __bool__(self):
        return True

    @property
    def set(self):
        class _PageCookieMgr:
            def __init__(self, page):
                self._p = page
            def clear(self):
                self._p.context.clear_cookies()
            def __call__(self, cookies_or_items):
                if isinstance(cookies_or_items, dict):
                    items = [cookies_or_items]
                else:
                    items = list(cookies_or_items)
                self._p.context.add_cookies(items)
        class _PageCookiesProxy:
            cookies = _PageCookieMgr(None)
        mgr = _PageCookieMgr(self._p)
        proxy = _PageCookiesProxy()
        proxy.cookies = mgr
        return proxy

    def cookies(self, all_domains: bool = False, all_info: bool = False) -> list[dict]:
        _ = all_domains, all_info
        try:
            if self._ctx:
                return self._ctx.cookies()
        except Exception:
            pass
        return []


# ── PatchrightBrowser wrapper ─────────────────────────────────────────
class PatchrightBrowser:
    def __init__(self, pw_browser: Any, pw_context: Any, launch_opts: ChromiumOptions | None = None):
        self._b = pw_browser
        self._ctx = pw_context
        self._opts = launch_opts

    @property
    def latest_tab(self) -> PatchrightPage | None:
        pages = self._ctx.pages
        return PatchrightPage(pages[-1], self._ctx) if pages else None

    @property
    def tab_ids(self) -> list[int]:
        return [id(p) for p in self._ctx.pages]

    def new_tab(self, url: str | None = None) -> PatchrightPage:
        page = self._ctx.new_page()
        if url:
            page.goto(url)
        return PatchrightPage(page, self._ctx)

    def get_tabs(self) -> list[PatchrightPage]:
        return [PatchrightPage(p, self._ctx) for p in self._ctx.pages]

    def get_tab(self, tab_id_or_index: int) -> PatchrightPage | None:
        """Get tab by id or index (0-based, DrissionPage compat)."""
        pages = self._ctx.pages
        if not pages:
            return None
        for p in pages:
            if id(p) == tab_id_or_index:
                return PatchrightPage(p, self._ctx)
        if 0 <= tab_id_or_index < len(pages):
            return PatchrightPage(pages[tab_id_or_index], self._ctx)
        return None

    @property
    def set(self):
        ctx = self._ctx
        class _CookieMgr:
            def clear(self):
                ctx.clear_cookies()
            def remove(self, cookie):
                try:
                    cks = ctx.cookies()
                    remaining = [c for c in cks if not (c.get("name") == cookie.get("name") and c.get("domain") == cookie.get("domain"))]
                    ctx.clear_cookies()
                    if remaining:
                        ctx.add_cookies(remaining)
                except Exception:
                    ctx.clear_cookies()
            def __call__(self, cookies_or_items):
                if isinstance(cookies_or_items, dict):
                    items = [cookies_or_items]
                else:
                    items = list(cookies_or_items)
                ctx.add_cookies(items)
        class _CookiesProxy:
            cookies = _CookieMgr()
        return _CookiesProxy()

    def cookies(self) -> list[dict]:
        try:
            return self._ctx.cookies()
        except Exception:
            return []

    def quit(self, del_data: bool = False):
        try:
            self._b.close()
        except Exception:
            pass

    @property
    def user_data_path(self) -> str | None:
        if self._opts:
            return self._opts._user_data_path
        return None


# ── Chromium factory ──────────────────────────────────────────────────
def Chromium(opts: ChromiumOptions) -> PatchrightBrowser:
    pw = _get_playwright()
    kw = opts.to_launch_kwargs()

    user_data_dir = kw.pop("user_data_dir", None)
    headless = kw.pop("headless", False)
    executable_path = kw.pop("executable_path", None)
    args = kw.pop("args", [])
    proxy = kw.pop("proxy", None)
    no_viewport = kw.pop("no_viewport", True)

    launch_options: dict[str, Any] = {
        "headless": headless,
        "args": args,
    }
    if executable_path:
        launch_options["executable_path"] = executable_path
    if proxy:
        launch_options["proxy"] = proxy

    last_err: Exception | None = None

    for attempt in range(1, 5):
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                **launch_options,
                no_viewport=no_viewport,
            )
            browser = context.browser
            return PatchrightBrowser(browser, context, opts)
        except Exception as exc:
            last_err = exc
            _kill_chrome_user_data_dir(user_data_dir)
            time.sleep(min(1.5 * attempt, 4.0))

    raise Exception(f"浏览器启动失败，已重试4次: {last_err}")


# ── Internal helpers ──────────────────────────────────────────────────

def _dp_sel_to_css(selector: str) -> str:
    s = selector.strip()
    if s.startswith("@name="):
        return f'[name="{s[6:]}"]'
    if s.startswith("@"):
        return f"[{s[1:]}]"
    if s.startswith("css:"):
        return s[4:]
    if s.startswith("tag:"):
        return s[4:]
    if s.startswith("xpath:"):
        return f"xpath={s[6:]}"
    if s.startswith("//"):
        return f"xpath={s}"
    return s


def _eval_js(target: Any, js_code: str, *args) -> Any:
    if not args:
        return target.evaluate(f"() => {{ {js_code} }}")
    arg_list = json.dumps(list(args))
    return target.evaluate(
        f"() => new Function({json.dumps(js_code)}).apply(null, {arg_list})"
    )


def _kill_chrome_user_data_dir(user_data_dir: str | None) -> None:
    if not user_data_dir or os.name != "nt":
        return
    import subprocess

    safe_dir = user_data_dir.replace("'", "''")
    try:
        subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-CimInstance Win32_Process -Filter \"name='chrome.exe' and CommandLine like '%--user-data-dir={safe_dir}%'\" "
                f"| ForEach-Object {{ try {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }} catch {{}} }}",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:
        pass
