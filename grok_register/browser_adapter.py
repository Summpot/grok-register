"""
Camoufox adapter — drop-in compatibility layer for DrissionPage API.

Usage (app.py / browser_confirm.py / tab_pool.py):
    from grok_register.browser_adapter import Chromium, ChromiumOptions

Wraps Camoufox (stealth Firefox + Playwright API) and exposes the
DrissionPage surface used by this project so registration / CPA-mint
logic stays largely unchanged.

Browser: Camoufox Firefox (anti-detect at engine level).
No turnstilePatch extension / init-script injection — Turnstile is
solved via Playwright-native clicks with human-like pauses.
"""

from __future__ import annotations

import atexit
import json
import os
import random
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ── Playwright singleton (shared by Camoufox NewBrowser) ─────────────
_pw = None
_pw_lock = threading.Lock()


def _get_playwright():
    global _pw
    if _pw is not None:
        return _pw
    with _pw_lock:
        if _pw is not None:
            return _pw
        from playwright.sync_api import sync_playwright

        _pw = sync_playwright().start()
        atexit.register(_pw.stop)
        return _pw


# ── Human-like timing helpers ─────────────────────────────────────────

def human_pause(lo: float = 0.18, hi: float = 0.55) -> None:
    """Sleep a random short duration to mimic human hesitation."""
    if hi < lo:
        lo, hi = hi, lo
    time.sleep(random.uniform(lo, hi))


def human_type_delay_ms() -> int:
    """Per-keystroke delay (ms) for press_sequentially / type."""
    return random.randint(35, 115)


# ── Error compatibility ───────────────────────────────────────────────
class PageDisconnectedError(Exception):
    pass


class _NoSuchElementError(Exception):
    pass


# ── Proxy helpers ─────────────────────────────────────────────────────

def proxy_dict_from_url(proxy: str | None) -> dict[str, str] | None:
    """Build Playwright/Camoufox proxy dict from a URL (supports user:pass)."""
    p = (proxy or "").strip()
    if not p:
        return None
    p = p.split()[0].rstrip(")',\"")
    try:
        u = urlparse(p if "://" in p else f"http://{p}")
    except Exception:
        return None
    host = u.hostname or ""
    if not host:
        return None
    scheme = (u.scheme or "http").lower()
    port = u.port or (443 if scheme == "https" else 80)
    out: dict[str, str] = {"server": f"{scheme}://{host}:{port}"}
    if u.username:
        out["username"] = u.username
        out["password"] = u.password or ""
    return out


# ── ChromiumOptions compatibility ─────────────────────────────────────
class ChromiumOptions:
    """Drop-in replacement for DrissionPage's ChromiumOptions.

    Collects flags / proxy / user-data-dir / headless settings and exposes
    them as kwargs for Camoufox NewBrowser.
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
        self._channel: str | None = None
        # Camoufox-specific
        self._humanize: bool | float = True
        self._disable_coop: bool = True
        self._geoip: bool | str | None = None
        self._os: str | list[str] | None = "windows"
        self._window: tuple[int, int] | None = None

    def auto_port(self):
        return self

    def set_timeouts(self, base: int = 2):
        self._timeout_base = base
        return self

    def set_argument(self, flag: str):
        # Camoufox/Firefox ignores most Chromium flags; keep only proxy-server.
        f = (flag or "").strip()
        if f.startswith("--proxy-server="):
            self._proxy = f.split("=", 1)[1].strip() or self._proxy
        self._flags.append(flag)
        return self

    def set_proxy(self, proxy: str):
        self._proxy = proxy
        return self

    def add_extension(self, path: str):
        # Firefox addons only; Chromium MV2 turnstilePatch is no longer used.
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
        # Camoufox ships its own Firefox; path is ignored but kept for API compat.
        self._browser_path = path
        return self

    def set_channel(self, channel: str):
        # No Chrome channel under Camoufox; kept as no-op for call sites.
        self._channel = channel
        return self

    def set_humanize(self, value: bool | float = True):
        self._humanize = value
        return self

    def set_disable_coop(self, on: bool = True):
        """Allow clicking cross-origin Turnstile iframe contents."""
        self._disable_coop = on
        return self

    def set_geoip(self, value: bool | str | None = True):
        self._geoip = value
        return self

    def set_os(self, os_name: str | list[str] | None = "windows"):
        self._os = os_name
        return self

    @property
    def address(self) -> str:
        return self._address

    def to_camoufox_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for camoufox NewBrowser / launch_options."""
        kw: dict[str, Any] = {
            "humanize": self._humanize if self._humanize is not None else True,
            "disable_coop": bool(self._disable_coop),
            "i_know_what_im_doing": True,  # suppress COOP leak warning (intentional)
        }

        if self._headless is not None:
            kw["headless"] = self._headless
        else:
            kw["headless"] = False

        if self._os is not None:
            kw["os"] = self._os

        if self._geoip is not None:
            kw["geoip"] = self._geoip
        elif self._proxy:
            # Match locale/timezone to proxy egress IP when possible.
            kw["geoip"] = True

        if self._window:
            kw["window"] = self._window

        proxy = proxy_dict_from_url(self._proxy)
        if proxy:
            kw["proxy"] = proxy

        # Only pass extracted Firefox addon dirs (not Chromium MV2).
        addons: list[str] = []
        for ext in self._extensions:
            if ext and os.path.isdir(ext):
                # Skip known Chromium-only turnstilePatch
                name = os.path.basename(ext.rstrip("\\/"))
                if name.lower() in {"turnstilepatch", "turnstile"}:
                    continue
                manifest = os.path.join(ext, "manifest.json")
                if os.path.isfile(manifest):
                    try:
                        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
                        # Firefox addons typically lack "manifest_version": 3 chrome keys
                        # or use applications.gecko — skip chrome-only MV2/MV3.
                        if "applications" in data or "browser_specific_settings" in data:
                            addons.append(ext)
                    except Exception:
                        pass
        if addons:
            kw["addons"] = addons

        # Persistent profile optional
        if self._user_data_path:
            os.makedirs(self._user_data_path, exist_ok=True)
            kw["persistent_context"] = True
            kw["user_data_dir"] = self._user_data_path

        return kw

    def to_launch_kwargs(self) -> dict[str, Any]:
        """Alias kept for older call sites."""
        return self.to_camoufox_kwargs()


# ── Element wrapper ───────────────────────────────────────────────────
class PatchrightElement:
    """Wraps a Playwright ElementHandle with DrissionPage-like API.

    If `is_shadow_host=True`, the element represents a shadow-root of the
    host element `handle`.
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

    @property
    def value(self) -> str:
        try:
            return self._h.input_value()
        except Exception:
            try:
                return str(self._h.evaluate("el => el.value || ''") or "")
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
        """Prefer Playwright-native click; JS only if explicitly requested or native fails."""
        if by_js:
            self._h.evaluate("el => el.click()")
            return
        try:
            human_pause(0.05, 0.18)
            self._h.scroll_into_view_if_needed()
            human_pause(0.08, 0.22)
            self._h.click(timeout=5000)
        except Exception:
            try:
                self._h.evaluate("el => el.click()")
            except Exception:
                raise

    def clear(self):
        """Clear input via Playwright-native fill('')."""
        try:
            self._h.fill("")
        except Exception:
            try:
                self._h.evaluate(
                    """el => {
                        el.focus();
                        el.value = '';
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }"""
                )
            except Exception:
                pass

    def input(self, text: str, *, clear: bool = True, human: bool = True):
        """Type into the element with Playwright-native APIs + optional key delays."""
        value = "" if text is None else str(text)
        try:
            self._h.scroll_into_view_if_needed()
        except Exception:
            pass
        human_pause(0.1, 0.28)
        try:
            self._h.click(timeout=4000)
        except Exception:
            pass
        human_pause(0.08, 0.2)
        if clear:
            try:
                self._h.fill("")
            except Exception:
                pass
            human_pause(0.05, 0.15)
        if human and value:
            try:
                # press_sequentially: native keyboard events with delay
                self._h.press_sequentially(value, delay=human_type_delay_ms())
            except Exception:
                try:
                    self._h.type(value, delay=human_type_delay_ms())
                except Exception:
                    self._h.fill(value)
        else:
            try:
                self._h.fill(value)
            except Exception:
                self._h.evaluate(
                    """(el, v) => {
                        el.focus();
                        el.value = v;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    value,
                )
        human_pause(0.12, 0.35)

    def fill(self, text: str, *, human: bool = True):
        """Alias of input()."""
        self.input(text, clear=True, human=human)

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
        # Iframe routing
        try:
            tag = self._h.evaluate("el => el.tagName")
            if tag and tag.upper() == "IFRAME":
                frame = self._h.content_frame()
                if frame:
                    el = frame.locator(css).first
                    try:
                        if el:
                            h = el.element_handle()
                            if h:
                                return PatchrightElement(h, self._page)
                    except Exception:
                        pass
                    try:
                        h = frame.evaluate_handle(f"() => document.querySelector({css!r})")
                        if h:
                            has_tag = h.evaluate("el => el ? el.tagName : null")
                            if has_tag:
                                return PatchrightElement(h, self._page)
                    except Exception:
                        pass
                return None
        except Exception:
            pass
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


# Back-compat aliases
CamoufoxElement = PatchrightElement
ChromiumElement = PatchrightElement


# ── Page wrapper ──────────────────────────────────────────────────────
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
        human_pause(0.25, 0.7)

    @property
    def wait(self):
        class _PageWaiter:
            def __init__(self, p):
                self._p = p

            def doc_loaded(self):
                self._p.wait_for_load_state("domcontentloaded")
                human_pause(0.15, 0.4)

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

    def locator(self, selector: str):
        """Expose Playwright locator for native fill/click flows."""
        return self._p.locator(selector)

    def fill_first(
        self,
        selectors: list[str],
        value: str,
        *,
        human: bool = True,
        timeout_ms: int = 4000,
    ) -> bool:
        """Playwright-native fill of the first matching visible input."""
        for sel in selectors:
            try:
                loc = self._p.locator(sel)
                count = loc.count()
                if count <= 0:
                    continue
                target = None
                for i in range(min(count, 8)):
                    cand = loc.nth(i)
                    try:
                        if cand.is_visible(timeout=400):
                            target = cand
                            break
                    except Exception:
                        continue
                if target is None:
                    continue
                human_pause(0.12, 0.35)
                try:
                    target.scroll_into_view_if_needed(timeout=timeout_ms)
                except Exception:
                    pass
                human_pause(0.08, 0.2)
                target.click(timeout=timeout_ms)
                human_pause(0.1, 0.28)
                target.fill("", timeout=timeout_ms)
                human_pause(0.05, 0.15)
                text = "" if value is None else str(value)
                if human and text:
                    try:
                        target.press_sequentially(text, delay=human_type_delay_ms())
                    except Exception:
                        target.fill(text, timeout=timeout_ms)
                else:
                    target.fill(text, timeout=timeout_ms)
                human_pause(0.15, 0.4)
                return True
            except Exception:
                continue
        return False

    def click_first(
        self,
        selectors: list[str],
        *,
        timeout_ms: int = 4000,
        exact_text: str | None = None,
    ) -> bool:
        """Playwright-native click of the first matching visible control."""
        for sel in selectors:
            try:
                loc = self._p.locator(sel)
                if exact_text is not None:
                    loc = loc.filter(has_text=re.compile(f"^{re.escape(exact_text)}$"))
                count = loc.count()
                if count <= 0:
                    continue
                for i in range(min(count, 10)):
                    cand = loc.nth(i)
                    try:
                        if not cand.is_visible(timeout=300):
                            continue
                    except Exception:
                        continue
                    human_pause(0.12, 0.35)
                    try:
                        cand.scroll_into_view_if_needed(timeout=timeout_ms)
                    except Exception:
                        pass
                    human_pause(0.08, 0.22)
                    cand.click(timeout=timeout_ms)
                    human_pause(0.2, 0.5)
                    return True
            except Exception:
                continue
        return False

    def click_by_text(
        self,
        labels: list[str],
        *,
        role: str = "button",
        timeout_ms: int = 4000,
    ) -> str | None:
        """Click first visible control whose text matches any label (substring)."""
        for label in labels:
            if not label:
                continue
            # role-based
            try:
                loc = self._p.get_by_role(role, name=re.compile(re.escape(label), re.I))
                if loc.count() > 0:
                    for i in range(min(loc.count(), 6)):
                        cand = loc.nth(i)
                        try:
                            if cand.is_visible(timeout=300):
                                human_pause(0.12, 0.32)
                                cand.click(timeout=timeout_ms)
                                human_pause(0.2, 0.5)
                                return label
                        except Exception:
                            continue
            except Exception:
                pass
            # text locator fallback
            try:
                loc = self._p.get_by_text(label, exact=False)
                if loc.count() > 0:
                    for i in range(min(loc.count(), 6)):
                        cand = loc.nth(i)
                        try:
                            if cand.is_visible(timeout=300):
                                human_pause(0.12, 0.32)
                                cand.click(timeout=timeout_ms)
                                human_pause(0.2, 0.5)
                                return label
                        except Exception:
                            continue
            except Exception:
                pass
        return None

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
            return self._p.context.cookies()
        except Exception:
            pass
        return []


CamoufoxPage = PatchrightPage


# ── Browser wrapper ───────────────────────────────────────────────────
class PatchrightBrowser:
    def __init__(
        self,
        pw_browser: Any,
        pw_context: Any,
        launch_opts: ChromiumOptions | None = None,
        *,
        is_persistent_context: bool = False,
    ):
        self._b = pw_browser
        self._ctx = pw_context
        self._opts = launch_opts
        self._is_persistent = is_persistent_context

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
            human_pause(0.2, 0.5)
        return PatchrightPage(page, self._ctx)

    def get_tabs(self) -> list[PatchrightPage]:
        return [PatchrightPage(p, self._ctx) for p in self._ctx.pages]

    def get_tab(self, tab_id_or_index: int) -> PatchrightPage | None:
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
                    remaining = [
                        c
                        for c in cks
                        if not (
                            c.get("name") == cookie.get("name")
                            and c.get("domain") == cookie.get("domain")
                        )
                    ]
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
        _ = del_data
        try:
            if self._is_persistent:
                self._ctx.close()
            else:
                try:
                    self._ctx.close()
                except Exception:
                    pass
                try:
                    self._b.close()
                except Exception:
                    pass
        except Exception:
            pass

    @property
    def user_data_path(self) -> str | None:
        if self._opts:
            return self._opts._user_data_path
        return None


CamoufoxBrowser = PatchrightBrowser


# ── Chromium factory (Camoufox) ───────────────────────────────────────
def Chromium(opts: ChromiumOptions) -> PatchrightBrowser:
    """Launch Camoufox and return a DrissionPage-compatible browser wrapper."""
    from camoufox.sync_api import NewBrowser

    pw = _get_playwright()
    kw = opts.to_camoufox_kwargs()
    persistent = bool(kw.pop("persistent_context", False))
    user_data_dir = kw.pop("user_data_dir", None)

    last_err: Exception | None = None

    for attempt in range(1, 5):
        try:
            if persistent and user_data_dir:
                context = NewBrowser(
                    pw,
                    persistent_context=True,
                    user_data_dir=user_data_dir,
                    **kw,
                )
                # persistent returns BrowserContext
                if not context.pages:
                    context.new_page()
                return PatchrightBrowser(
                    getattr(context, "browser", None) or context,
                    context,
                    opts,
                    is_persistent_context=True,
                )

            browser = NewBrowser(pw, **kw)
            # Prefer a single shared context (multi-tab like Chromium)
            context = browser.new_context()
            if not context.pages:
                context.new_page()
            return PatchrightBrowser(browser, context, opts, is_persistent_context=False)
        except Exception as exc:
            last_err = exc
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
