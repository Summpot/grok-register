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


# ── Turnstile: shadow-pierce bbox + mouse click (Camoufox recommended) ──
#
# Turnstile checkbox lives in a cross-origin iframe, often under open/closed
# shadow roots. frame_locator('#checkbox') frequently finds nothing even when
# the box is visible. Camoufox docs solve this with disable_coop + mouse.click
# at the widget coordinates.

_TURNSTILE_BBOX_JS = """
() => {
    const out = [];
    const seen = new Set();

    function pushRect(el, kind) {
        try {
            const r = el.getBoundingClientRect();
            if (!r || r.width < 12 || r.height < 12) return;
            if (r.bottom < 0 || r.right < 0) return;
            if (r.top > (window.innerHeight || 2000) + 40) return;
            const key = [
                Math.round(r.x), Math.round(r.y),
                Math.round(r.width), Math.round(r.height), kind
            ].join(',');
            if (seen.has(key)) return;
            seen.add(key);
            out.push({
                x: r.x,
                y: r.y,
                width: r.width,
                height: r.height,
                kind: kind,
                tag: String(el.tagName || '').toLowerCase(),
            });
        } catch (e) {}
    }

    function isCfIframe(el) {
        const src = String(el.src || el.getAttribute('src') || '');
        const name = String(el.name || el.getAttribute('name') || '');
        const title = String(el.title || el.getAttribute('title') || '');
        const id = String(el.id || '');
        const hay = (src + ' ' + name + ' ' + title + ' ' + id).toLowerCase();
        return (
            hay.includes('challenges.cloudflare.com')
            || hay.includes('turnstile')
            || hay.includes('cf-chl')
            || hay.includes('cf-turnstile')
        );
    }

    function walk(root) {
        if (!root || !root.querySelectorAll) return;
        try {
            for (const el of root.querySelectorAll(
                'div.cf-turnstile, .cf-turnstile, [data-sitekey], [data-callback]'
            )) {
                pushRect(el, 'widget');
            }
            for (const el of root.querySelectorAll('iframe')) {
                if (isCfIframe(el)) pushRect(el, 'iframe');
            }
            // Some hosts only expose a small wrapper around the shadow/iframe
            for (const el of root.querySelectorAll(
                '[id*="cf-" i], [id*="turnstile" i], [class*="turnstile" i], [class*="cf-turnstile" i]'
            )) {
                pushRect(el, 'host');
            }
        } catch (e) {}
        try {
            for (const el of root.querySelectorAll('*')) {
                if (el.shadowRoot) walk(el.shadowRoot);
            }
        } catch (e) {}
    }

    walk(document);
    // Prefer real iframes / widgets over generic hosts
    const rank = { iframe: 0, widget: 1, host: 2 };
    out.sort((a, b) => {
        const ra = rank[a.kind] ?? 9;
        const rb = rank[b.kind] ?? 9;
        if (ra !== rb) return ra - rb;
        return (a.y - b.y) || (a.x - b.x);
    });
    return out;
}
"""


def _checkbox_point_from_box(box: dict[str, Any]) -> tuple[float, float]:
    """Map widget bbox → typical Turnstile checkbox center (left side of widget)."""
    x = float(box.get("x") or 0)
    y = float(box.get("y") or 0)
    w = float(box.get("width") or 0)
    h = float(box.get("height") or 0)
    # Checkbox is a ~28px circle near the left edge, vertically centered.
    cx = x + min(28.0, max(14.0, w * 0.10)) + random.uniform(-2.5, 2.5)
    cy = y + h * 0.5 + random.uniform(-3.0, 3.0)
    return cx, cy


def mouse_click_xy(pw_page: Any, x: float, y: float) -> None:
    """Human-ish mouse move + click at viewport coordinates."""
    # Start from a nearby random offset if possible, then ease in.
    try:
        sx = max(0.0, x + random.uniform(-80, 40))
        sy = max(0.0, y + random.uniform(-40, 40))
        pw_page.mouse.move(sx, sy, steps=random.randint(4, 10))
        human_pause(0.04, 0.12)
    except Exception:
        pass
    pw_page.mouse.move(x, y, steps=random.randint(10, 24))
    human_pause(0.06, 0.18)
    # delay = hold time before mouseup (ms)
    try:
        pw_page.mouse.click(x, y, delay=random.randint(45, 140))
    except TypeError:
        pw_page.mouse.click(x, y)
    human_pause(0.12, 0.35)


def find_turnstile_boxes(pw_page: Any) -> list[dict[str, Any]]:
    """Return visible Turnstile widget/iframe bboxes.

    Combines:
      - JS walk through open shadow roots
      - Playwright frame.frame_element().bounding_box() for closed-shadow iframes
        (page.frames still lists them even when querySelector cannot)
    """
    boxes: list[dict[str, Any]] = []
    try:
        raw = pw_page.evaluate(_TURNSTILE_BBOX_JS)
        if isinstance(raw, list):
            boxes.extend(b for b in raw if isinstance(b, dict))
    except Exception:
        pass

    # Closed shadow / cross-origin: recover iframe geometry via frame handles
    try:
        for fr in list(getattr(pw_page, "frames", []) or []):
            try:
                url = (fr.url or "").lower()
            except Exception:
                url = ""
            if not any(
                k in url
                for k in ("cloudflare", "turnstile", "challenges", "cf-chl", "cdn-cgi")
            ):
                continue
            try:
                # main_frame is the page itself — skip
                if fr == pw_page.main_frame:
                    continue
            except Exception:
                pass
            try:
                fe = fr.frame_element()
                if not fe:
                    continue
                bb = fe.bounding_box()
                if not bb or bb.get("width", 0) < 12 or bb.get("height", 0) < 12:
                    continue
                boxes.append(
                    {
                        "x": bb["x"],
                        "y": bb["y"],
                        "width": bb["width"],
                        "height": bb["height"],
                        "kind": "iframe",
                        "tag": "iframe",
                        "frame_url": url[:120],
                    }
                )
            except Exception:
                continue
    except Exception:
        pass

    # Dedupe near-identical boxes
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for b in boxes:
        key = (
            int(round(float(b.get("x", 0)))),
            int(round(float(b.get("y", 0)))),
            int(round(float(b.get("width", 0)))),
            int(round(float(b.get("height", 0)))),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(b)

    rank = {"iframe": 0, "widget": 1, "host": 2}
    deduped.sort(
        key=lambda b: (
            rank.get(str(b.get("kind")), 9),
            float(b.get("y") or 0),
            float(b.get("x") or 0),
        )
    )
    return deduped


def click_turnstile_checkbox(pw_page: Any, *, log: Any | None = None) -> bool:
    """Attempt to click the Cloudflare Turnstile checkbox.

    Strategy (in order):
      1. Mouse click at checkbox coordinates from shadow-piercing bbox
         (Camoufox official pattern for disable_coop)
      2. Playwright frame API: page.frames + locator('#checkbox')
      3. frame_locator / main-page iframe locator click

    Returns True if at least one click strategy ran without hard failure.
    """
    clicked = False
    log_fn = log if callable(log) else None

    # Ensure any candidate is scrolled into view first (main document only).
    try:
        pw_page.evaluate(
            """() => {
                const sels = [
                  'iframe[src*="challenges.cloudflare.com"]',
                  'iframe[src*="turnstile"]',
                  'div.cf-turnstile',
                  '[data-sitekey]',
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el) { el.scrollIntoView({block:'center', inline:'center'}); return true; }
                }
                // shadow walk scroll
                function walk(root) {
                    if (!root || !root.querySelectorAll) return false;
                    for (const el of root.querySelectorAll('iframe, div.cf-turnstile, [data-sitekey]')) {
                        const src = String(el.src || el.getAttribute('src') || el.className || '');
                        if (/cloudflare|turnstile|cf-turnstile|data-sitekey/i.test(src + el.outerHTML.slice(0,200))) {
                            try { el.scrollIntoView({block:'center', inline:'center'}); return true; } catch(e) {}
                        }
                    }
                    for (const el of root.querySelectorAll('*')) {
                        if (el.shadowRoot && walk(el.shadowRoot)) return true;
                    }
                    return false;
                }
                return walk(document);
            }"""
        )
        human_pause(0.15, 0.35)
    except Exception:
        pass

    # 1) Coordinate mouse click (most reliable with Camoufox)
    boxes = find_turnstile_boxes(pw_page)
    if boxes:
        # Prefer first iframe/widget; if that fails, try next distinct box
        tried_pts: set[tuple[int, int]] = set()
        for box in boxes[:3]:
            try:
                cx, cy = _checkbox_point_from_box(box)
                key = (int(cx // 5), int(cy // 5))
                if key in tried_pts:
                    continue
                tried_pts.add(key)
                if log_fn:
                    log_fn(
                        f"turnstile mouse click @ ({cx:.0f},{cy:.0f}) "
                        f"box={box.get('kind')}:{box.get('width'):.0f}x{box.get('height'):.0f}"
                    )
                mouse_click_xy(pw_page, cx, cy)
                clicked = True
                # One solid click is enough; avoid multi-widget spam
                return True
            except Exception as exc:
                if log_fn:
                    log_fn(f"turnstile mouse click failed: {exc}")

    # 2) Direct frame handles — only Cloudflare/turnstile frames, never main doc body
    try:
        for fr in list(getattr(pw_page, "frames", []) or []):
            try:
                url = (fr.url or "").lower()
            except Exception:
                url = ""
            is_cf = any(
                k in url
                for k in ("cloudflare", "turnstile", "challenges", "cf-chl", "cdn-cgi")
            )
            # Skip top-level page frame unless URL itself is a challenge page
            try:
                if fr == pw_page.main_frame and not is_cf:
                    continue
            except Exception:
                if not is_cf:
                    continue
            if not is_cf:
                # Non-CF child frame: only attempt real checkbox selectors
                sels = (
                    "#checkbox",
                    'input[type="checkbox"]',
                    ".mark",
                    '[role="checkbox"]',
                )
            else:
                sels = (
                    "#checkbox",
                    'input[type="checkbox"]',
                    ".mark",
                    '[role="checkbox"]',
                    "label.cb-lb",
                    "label",
                )
            for sel in sels:
                try:
                    loc = fr.locator(sel)
                    if loc.count() <= 0:
                        continue
                    target = loc.first
                    try:
                        if not target.is_visible(timeout=400):
                            continue
                    except Exception:
                        pass
                    human_pause(0.1, 0.25)
                    target.click(timeout=3000, force=True)
                    clicked = True
                    if log_fn:
                        log_fn(f"turnstile frame click sel={sel!r} url={url[:80]}")
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    # 3) frame_locator checkbox (when accessible)
    try:
        frame = pw_page.frame_locator(
            'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
        )
        for sel in ("#checkbox", 'input[type="checkbox"]', ".mark", '[role="checkbox"]'):
            try:
                cb = frame.locator(sel)
                if cb.count() > 0:
                    human_pause(0.1, 0.25)
                    cb.first.click(timeout=3000, force=True)
                    clicked = True
                    if log_fn:
                        log_fn(f"turnstile frame_locator click sel={sel!r}")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # 4) Locator bounding_box → mouse (pierces some open shadow via CSS)
    try:
        widget = pw_page.locator(
            'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey]'
        )
        n = widget.count()
        for i in range(min(n, 4)):
            cand = widget.nth(i)
            try:
                if not cand.is_visible(timeout=300):
                    continue
                bb = cand.bounding_box()
                if not bb or bb.get("width", 0) < 12:
                    continue
                cx = bb["x"] + min(28.0, bb["width"] * 0.1) + random.uniform(-2, 2)
                cy = bb["y"] + bb["height"] * 0.5 + random.uniform(-2, 2)
                mouse_click_xy(pw_page, cx, cy)
                clicked = True
                if log_fn:
                    log_fn(f"turnstile locator bbox click ({cx:.0f},{cy:.0f})")
                return True
            except Exception:
                try:
                    cand.click(timeout=2000, force=True, position={"x": 25, "y": 30})
                    clicked = True
                    if log_fn:
                        log_fn("turnstile locator position click")
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    return clicked


def turnstile_token_len(pw_page: Any) -> int:
    """Length of input[name=cf-turnstile-response] value, or 0."""
    try:
        token = pw_page.evaluate(
            """() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                return (el && el.value) || '';
            }"""
        )
        return len(str(token or "").strip())
    except Exception:
        return 0


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
