"""
Camoufox adapter — drop-in compatibility layer for DrissionPage API.

Usage (app.py / tab_pool.py):
    from grok_register.browser_adapter import Chromium, ChromiumOptions

Wraps Camoufox (stealth Firefox + Playwright API) and exposes the
DrissionPage surface used by this project so registration logic stays
largely unchanged.

Browser: Camoufox Firefox (anti-detect at engine level).
No turnstilePatch extension / init-script injection — Turnstile is
solved via Playwright-native clicks with human-like pauses.
"""

from __future__ import annotations

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

# ── Playwright per-thread driver (Camoufox NewBrowser) ────────────────
#
# Playwright's sync API is greenlet-bound to the OS thread that called
# sync_playwright().start(). Sharing one process-wide instance across
# register workers causes:
#   greenlet.error: Cannot switch to a different thread
# Keep one driver per thread; never call it from another thread.
_pw_tls = threading.local()
_pw_start_lock = threading.Lock()


def _get_playwright():
    pw = getattr(_pw_tls, "pw", None)
    if pw is not None:
        return pw
    from playwright.sync_api import sync_playwright

    # Serialize start() only: avoids concurrent driver bootstrap races on
    # Windows while still giving each thread its own instance.
    with _pw_start_lock:
        pw = getattr(_pw_tls, "pw", None)
        if pw is not None:
            return pw
        pw = sync_playwright().start()
        _pw_tls.pw = pw
        return pw


def stop_thread_playwright() -> None:
    """Stop the current thread's Playwright driver (call after browsers quit)."""
    pw = getattr(_pw_tls, "pw", None)
    if pw is None:
        return
    try:
        pw.stop()
    except Exception:
        pass
    try:
        _pw_tls.pw = None
    except Exception:
        pass


# ── Human-like timing helpers ─────────────────────────────────────────

def human_pause(lo: float = 0.18, hi: float = 0.55) -> None:
    """Sleep a random short duration to mimic human hesitation."""
    if hi < lo:
        lo, hi = hi, lo
    time.sleep(random.uniform(lo, hi))


def human_type_delay_ms() -> int:
    """Per-keystroke delay (ms) for press_sequentially / type."""
    return random.randint(35, 115)


def _gauss_clamp(mu: float, sigma: float, lo: float, hi: float) -> float:
    """Gaussian sample clamped into [lo, hi]."""
    if hi < lo:
        lo, hi = hi, lo
    for _ in range(8):
        v = random.gauss(mu, sigma)
        if lo <= v <= hi:
            return v
    return max(lo, min(hi, mu))


# ── Turnstile: shadow-pierce bbox + mouse click (Camoufox recommended) ──
#
# Turnstile checkbox lives in a cross-origin iframe, often under open/closed
# shadow roots. frame_locator('#checkbox') frequently finds nothing even when
# the box is visible. Camoufox docs solve this with disable_coop + mouse.click
# at the widget coordinates.
#
# Bot-score notes:
#   - Prefer ONE patient click + long wait over rapid re-clicks
#   - Allow managed-mode auto-solve before touching the widget
#   - Mouse path should look like hover-aim-click, not teleport+force click

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
    # Sample near the center of that circle (not a fixed pixel) for less
    # "always same coordinate" fingerprinting.
    base_cx = x + min(30.0, max(15.0, w * 0.095))
    base_cy = y + h * 0.50
    cx = _gauss_clamp(base_cx, 2.4, x + 8.0, x + min(42.0, max(18.0, w * 0.22)))
    cy = _gauss_clamp(base_cy, 2.0, y + h * 0.28, y + h * 0.72)
    return cx, cy


def _box_click_priority(box: dict[str, Any]) -> tuple[int, float, float]:
    """Rank Turnstile candidates: classic checkbox widget first, full-page last."""
    kind = str(box.get("kind") or "")
    w = float(box.get("width") or 0)
    h = float(box.get("height") or 0)
    score = 0
    # Managed checkbox widget is typically ~300x65
    if 240 <= w <= 340 and 45 <= h <= 100:
        score += 20
    elif 60 <= w <= 120 and 45 <= h <= 100:
        # compact checkbox-only frame
        score += 18
    elif w >= 400 or h >= 200:
        # large challenge / interstitial — click only as last resort
        score -= 10
    if kind == "iframe":
        score += 6
    elif kind == "widget":
        score += 4
    elif kind == "host":
        score += 1
    return (-score, float(box.get("y") or 0), float(box.get("x") or 0))


def mouse_click_xy(pw_page: Any, x: float, y: float) -> None:
    """Human-ish multi-segment mouse move + hover + click at viewport coords."""
    # Start from a plausible on-page position (not teleporting from 0,0).
    try:
        vp = getattr(pw_page, "viewport_size", None) or {}
        vw = float(vp.get("width") or 1000)
        vh = float(vp.get("height") or 800)
        sx = _gauss_clamp(vw * 0.42, vw * 0.12, 24.0, max(40.0, vw - 24.0))
        sy = _gauss_clamp(vh * 0.48, vh * 0.14, 40.0, max(60.0, vh - 40.0))
    except Exception:
        sx = max(0.0, x + random.uniform(-140, -30))
        sy = max(0.0, y + random.uniform(-90, 50))

    # Intermediate aim point near the checkbox, then slight overshoot/correct.
    mid_x = x + random.uniform(-28, 22)
    mid_y = y + random.uniform(-20, 18)
    over_x = x + random.uniform(-5, 7)
    over_y = y + random.uniform(-4, 5)

    try:
        pw_page.mouse.move(sx, sy, steps=random.randint(3, 7))
        human_pause(0.05, 0.16)
    except Exception:
        pass

    pw_page.mouse.move(mid_x, mid_y, steps=random.randint(14, 32))
    human_pause(0.05, 0.16)
    pw_page.mouse.move(over_x, over_y, steps=random.randint(7, 16))
    human_pause(0.04, 0.12)
    pw_page.mouse.move(x, y, steps=random.randint(4, 10))
    # Hover / aim dwell — humans rarely click on the same frame they arrive.
    human_pause(0.14, 0.42)
    try:
        jx = x + random.uniform(-1.3, 1.3)
        jy = y + random.uniform(-1.1, 1.1)
        pw_page.mouse.move(jx, jy, steps=random.randint(1, 3))
        human_pause(0.04, 0.12)
        x, y = jx, jy
    except Exception:
        pass

    # delay = hold time before mouseup (ms)
    try:
        pw_page.mouse.click(x, y, delay=random.randint(55, 175))
    except TypeError:
        pw_page.mouse.click(x, y)
    human_pause(0.18, 0.48)


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

    deduped.sort(key=_box_click_priority)
    return deduped


def _scroll_turnstile_into_view(pw_page: Any) -> None:
    """Scroll Turnstile host into view; small pause for layout settle."""
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
                    if (el) {
                        el.scrollIntoView({block:'center', inline:'center', behavior:'instant'});
                        return true;
                    }
                }
                function walk(root) {
                    if (!root || !root.querySelectorAll) return false;
                    for (const el of root.querySelectorAll('iframe, div.cf-turnstile, [data-sitekey]')) {
                        const src = String(el.src || el.getAttribute('src') || el.className || '');
                        if (/cloudflare|turnstile|cf-turnstile|data-sitekey/i.test(src + el.outerHTML.slice(0,200))) {
                            try {
                                el.scrollIntoView({block:'center', inline:'center', behavior:'instant'});
                                return true;
                            } catch(e) {}
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
        human_pause(0.18, 0.45)
    except Exception:
        pass


def click_turnstile_checkbox(
    pw_page: Any,
    *,
    log: Any | None = None,
    allow_force: bool = False,
) -> bool:
    """Attempt to click the Cloudflare Turnstile checkbox once.

    Strategy (in order):
      1. Mouse click at checkbox coordinates from shadow-piercing bbox
         (Camoufox official pattern for disable_coop) — preferred
      2. Playwright frame locator click without force (real pointer events)
      3. force=True only if allow_force (last resort; more synthetic)

    Returns True if at least one click strategy ran without hard failure.
    """
    log_fn = log if callable(log) else None
    _scroll_turnstile_into_view(pw_page)

    # 1) Coordinate mouse click (most reliable + most human with Camoufox)
    boxes = find_turnstile_boxes(pw_page)
    if boxes:
        tried_pts: set[tuple[int, int]] = set()
        for box in boxes[:2]:
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
                return True
            except Exception as exc:
                if log_fn:
                    log_fn(f"turnstile mouse click failed: {exc}")

    # 2) Frame / locator clicks — prefer non-force (real hit-testing)
    def _try_frame_click(force: bool) -> bool:
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
                try:
                    if fr == pw_page.main_frame and not is_cf:
                        continue
                except Exception:
                    if not is_cf:
                        continue
                if not is_cf:
                    continue
                for sel in (
                    "#checkbox",
                    'input[type="checkbox"]',
                    ".mark",
                    '[role="checkbox"]',
                    "label.cb-lb",
                ):
                    try:
                        loc = fr.locator(sel)
                        if loc.count() <= 0:
                            continue
                        target = loc.first
                        try:
                            if not target.is_visible(timeout=500):
                                continue
                        except Exception:
                            pass
                        human_pause(0.12, 0.32)
                        target.click(timeout=3000, force=force)
                        if log_fn:
                            log_fn(
                                f"turnstile frame click sel={sel!r} "
                                f"force={force} url={url[:80]}"
                            )
                        return True
                    except Exception:
                        continue
        except Exception:
            pass

        try:
            frame = pw_page.frame_locator(
                'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
            )
            for sel in (
                "#checkbox",
                'input[type="checkbox"]',
                ".mark",
                '[role="checkbox"]',
            ):
                try:
                    cb = frame.locator(sel)
                    if cb.count() <= 0:
                        continue
                    human_pause(0.12, 0.32)
                    cb.first.click(timeout=3000, force=force)
                    if log_fn:
                        log_fn(
                            f"turnstile frame_locator click sel={sel!r} force={force}"
                        )
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    if _try_frame_click(force=False):
        return True

    # 3) Locator bounding_box → mouse
    try:
        widget = pw_page.locator(
            'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], '
            "div.cf-turnstile, [data-sitekey]"
        )
        n = widget.count()
        for i in range(min(n, 3)):
            cand = widget.nth(i)
            try:
                if not cand.is_visible(timeout=400):
                    continue
                bb = cand.bounding_box()
                if not bb or bb.get("width", 0) < 12:
                    continue
                box = {
                    "x": bb["x"],
                    "y": bb["y"],
                    "width": bb["width"],
                    "height": bb["height"],
                }
                cx, cy = _checkbox_point_from_box(box)
                mouse_click_xy(pw_page, cx, cy)
                if log_fn:
                    log_fn(f"turnstile locator bbox click ({cx:.0f},{cy:.0f})")
                return True
            except Exception:
                continue
    except Exception:
        pass

    # 4) force click only as last resort
    if allow_force and _try_frame_click(force=True):
        return True

    return False


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


def turnstile_token_value(pw_page: Any) -> str:
    """Full cf-turnstile-response value, or empty string."""
    try:
        token = pw_page.evaluate(
            """() => {
                const el = document.querySelector('input[name="cf-turnstile-response"]');
                return (el && el.value) || '';
            }"""
        )
        return str(token or "").strip()
    except Exception:
        return ""


def turnstile_widget_present(pw_page: Any) -> bool:
    """True when Turnstile input/iframe/widget is in the DOM."""
    try:
        return bool(
            pw_page.evaluate(
                """() => {
                    return !!(
                        document.querySelector('input[name="cf-turnstile-response"]')
                        || document.querySelector(
                            'iframe[src*="turnstile"], iframe[src*="challenges.cloudflare.com"], '
                            + 'div.cf-turnstile, [data-sitekey], script[src*="turnstile"]'
                        )
                    );
                }"""
            )
        )
    except Exception:
        return False


def wait_for_turnstile_widget(
    pw_page: Any,
    *,
    timeout: float = 12.0,
    sleep_fn: Any | None = None,
    should_cancel: Any | None = None,
) -> list[dict[str, Any]]:
    """Poll until a stable Turnstile bbox appears (or timeout)."""
    sleeper = sleep_fn if callable(sleep_fn) else (lambda s: time.sleep(s))
    deadline = time.time() + max(0.5, timeout)
    last: list[dict[str, Any]] = []
    stable_hits = 0
    while time.time() < deadline:
        if callable(should_cancel) and should_cancel():
            break
        boxes = find_turnstile_boxes(pw_page)
        if boxes:
            # Require two consecutive non-empty reads so we don't click a
            # half-laid-out iframe (common bot signal).
            if last and abs(float(boxes[0].get("width") or 0) - float(last[0].get("width") or 0)) < 4:
                stable_hits += 1
            else:
                stable_hits = 1
            last = boxes
            if stable_hits >= 2:
                return boxes
        else:
            stable_hits = 0
            last = []
        sleeper(random.uniform(0.22, 0.45))
    return last


def solve_turnstile_patient(
    pw_page: Any,
    *,
    log: Any | None = None,
    sleep_fn: Any | None = None,
    should_cancel: Any | None = None,
    max_clicks: int = 4,
    timeout: float = 55.0,
    auto_solve_wait: tuple[float, float] = (1.1, 2.8),
    post_click_wait: tuple[float, float] = (2.6, 4.8),
    min_token_len: int = 80,
) -> str:
    """Patient Turnstile solve: auto-solve window → few human clicks → backoff.

    Designed to reduce bot_flag risk vs the old tight re-click loop:
      1. Wait for widget to stabilize
      2. Give managed mode a chance to pass without any click
      3. Click once with humanized mouse path
      4. Wait several seconds for token
      5. Re-click only after cooldown; escalate force only on last attempts

    Returns the response token string. Raises RuntimeError on timeout.
    """
    log_fn = log if callable(log) else None
    sleeper = sleep_fn if callable(sleep_fn) else (lambda s: time.sleep(s))

    def _cancelled() -> bool:
        return bool(callable(should_cancel) and should_cancel())

    def _log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    deadline = time.time() + max(8.0, timeout)
    clicks_done = 0
    # First click uses force only as last resort inside click helper
    force_after = max(1, max_clicks - 1)

    # 0) If already solved, return immediately
    token = turnstile_token_value(pw_page)
    if len(token) >= min_token_len:
        return token

    # 1) Widget ready
    boxes = wait_for_turnstile_widget(
        pw_page,
        timeout=min(14.0, max(4.0, timeout * 0.3)),
        sleep_fn=sleeper,
        should_cancel=should_cancel,
    )
    if boxes and log_fn:
        b0 = boxes[0]
        _log(
            f"turnstile widget ready kind={b0.get('kind')} "
            f"size={float(b0.get('width') or 0):.0f}x{float(b0.get('height') or 0):.0f}"
        )

    # 2) Natural / managed auto-solve window (no click yet)
    auto_lo, auto_hi = auto_solve_wait
    auto_deadline = time.time() + random.uniform(auto_lo, auto_hi)
    while time.time() < min(auto_deadline, deadline):
        if _cancelled():
            raise RuntimeError("Turnstile cancelled")
        token = turnstile_token_value(pw_page)
        if len(token) >= min_token_len:
            _log(f"turnstile auto-solved token_len={len(token)}")
            return token
        sleeper(random.uniform(0.28, 0.55))

    # 3) Click loop with patient post-click waits
    while time.time() < deadline and clicks_done < max_clicks:
        if _cancelled():
            raise RuntimeError("Turnstile cancelled")

        token = turnstile_token_value(pw_page)
        if len(token) >= min_token_len:
            _log(f"turnstile solved token_len={len(token)}")
            return token

        allow_force = clicks_done >= force_after
        _log(
            f"turnstile click attempt {clicks_done + 1}/{max_clicks}"
            + (" (force fallback enabled)" if allow_force else "")
        )
        try:
            ok = click_turnstile_checkbox(
                pw_page, log=log_fn, allow_force=allow_force
            )
        except Exception as exc:
            ok = False
            _log(f"turnstile click error: {exc}")

        clicks_done += 1 if ok else 0
        if not ok:
            # Widget may still be loading; short wait then retry
            sleeper(random.uniform(0.7, 1.3))
            clicks_done += 1  # count failed attempt to avoid infinite spin
            continue

        # Patient wait after a real click — do NOT re-click every second
        post_lo, post_hi = post_click_wait
        # Slightly longer wait after later clicks (challenge may be harder)
        scale = 1.0 + 0.15 * max(0, clicks_done - 1)
        wait_until = time.time() + random.uniform(post_lo, post_hi) * scale
        while time.time() < min(wait_until, deadline):
            if _cancelled():
                raise RuntimeError("Turnstile cancelled")
            token = turnstile_token_value(pw_page)
            if len(token) >= min_token_len:
                # Brief settle pause after success (look less "instant submit")
                sleeper(random.uniform(0.25, 0.7))
                _log(f"turnstile solved after click token_len={len(token)}")
                return token
            sleeper(random.uniform(0.35, 0.7))

        # Between re-clicks: extra human hesitation
        if clicks_done < max_clicks and time.time() < deadline:
            sleeper(random.uniform(0.6, 1.5))

    token = turnstile_token_value(pw_page)
    if len(token) >= min_token_len:
        return token
    raise RuntimeError(
        f"Turnstile token not obtained after {clicks_done} click(s) "
        f"(token_len={len(token)})"
    )


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


# ── OS window placement (background / off-screen mode) ────────────────
#
# Chrome used --window-position / --window-size. Camoufox is Firefox: size
# is a first-class launch option, but real desktop position must be applied
# after the OS window exists (Win32 SetWindowPos / best-effort elsewhere).

_window_place_lock = threading.Lock()


def _win_enum_mozilla_hwnds(*, include_hidden: bool = True) -> set[int]:
    """Top-level Mozilla/Firefox/Camoufox window handles on Windows."""
    if os.name != "nt":
        return set()
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return set()

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: set[int] = set()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            if not include_hidden and not user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            cls = (buf.value or "").lower()
            # Firefox / Camoufox main chrome windows
            if "mozilla" in cls or "firefox" in cls:
                found.add(int(hwnd))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        return set()
    return found


def _win_place_hwnd(
    hwnd: int,
    x: int,
    y: int,
    w: int | None,
    h: int | None,
    *,
    hide: bool = False,
) -> bool:
    """Move (and optionally resize/hide) a window without activating it."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_HIDEWINDOW = 0x0080
    SWP_SHOWWINDOW = 0x0040
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4

    flags = SWP_NOZORDER | SWP_NOACTIVATE
    width = int(w or 0)
    height = int(h or 0)
    if width <= 0 or height <= 0:
        flags |= SWP_NOSIZE
        width, height = 0, 0
    try:
        if hide:
            # Fully hide: no taskbar flash / no visible paint on desktop.
            user32.ShowWindow(wintypes.HWND(hwnd), SW_HIDE)
            flags |= SWP_HIDEWINDOW
        else:
            user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOWNOACTIVATE)
            flags |= SWP_SHOWWINDOW
        ok = user32.SetWindowPos(
            wintypes.HWND(hwnd),
            wintypes.HWND(0),
            int(x),
            int(y),
            width,
            height,
            flags,
        )
        return bool(ok)
    except Exception:
        return False


def _place_new_browser_windows(
    before: set[int],
    *,
    x: int,
    y: int,
    w: int | None,
    h: int | None,
    retries: int = 40,
    delay_s: float = 0.02,
    hide: bool = True,
) -> int:
    """Place any Mozilla windows that appeared after `before` was snapshotted."""
    moved = 0
    for _ in range(max(1, retries)):
        now = _win_enum_mozilla_hwnds(include_hidden=True)
        new_hwnds = now - before
        for hwnd in new_hwnds:
            if _win_place_hwnd(hwnd, x, y, w, h, hide=hide):
                moved += 1
        if moved:
            return moved
        time.sleep(delay_s)
    return moved


def _seed_firefox_xulstore(
    user_data_dir: str,
    *,
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    """Pre-seed Firefox profile so the first chrome window opens off-screen."""
    if not user_data_dir:
        return
    try:
        os.makedirs(user_data_dir, exist_ok=True)
        payload = {
            "chrome://browser/content/browser.xhtml": {
                "main-window": {
                    "screenX": str(int(x)),
                    "screenY": str(int(y)),
                    "width": str(int(w)),
                    "height": str(int(h)),
                    "sizemode": "normal",
                }
            },
            # Older chrome URL still checked by some Firefox builds.
            "chrome://browser/content/browser.xul": {
                "main-window": {
                    "screenX": str(int(x)),
                    "screenY": str(int(y)),
                    "width": str(int(w)),
                    "height": str(int(h)),
                    "sizemode": "normal",
                }
            },
        }
        path = os.path.join(user_data_dir, "xulstore.json")
        Path(path).write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def apply_window_placement(opts: "ChromiumOptions", before_hwnds: set[int] | None = None) -> None:
    """Apply off-screen (or configured) window position after Camoufox launch."""
    if opts is None:
        return
    pos = getattr(opts, "_window_position", None)
    if not pos:
        return
    try:
        x, y = int(pos[0]), int(pos[1])
    except Exception:
        return
    size = getattr(opts, "_window", None)
    w = int(size[0]) if size and len(size) >= 2 else None
    h = int(size[1]) if size and len(size) >= 2 else None

    if os.name == "nt":
        base = before_hwnds if before_hwnds is not None else set()
        # hide=True: no visible desktop window (offscreen mode without flash).
        _place_new_browser_windows(base, x=x, y=y, w=w, h=h, hide=True)
        return

    # Best-effort on non-Windows (often ignored by browsers for the main window).
    # Call sites can still pass size via Camoufox `window=`.


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
        # Real desktop position (x, y); applied post-launch via OS APIs.
        self._window_position: tuple[int, int] | None = None
        # Extra Firefox prefs merged into launch (e.g. reduce bg timer throttling).
        self._firefox_user_prefs: dict[str, Any] = {}

    def auto_port(self):
        return self

    def set_timeouts(self, base: int = 2):
        self._timeout_base = base
        return self

    def set_argument(self, flag: str):
        # Camoufox/Firefox ignores most Chromium flags; translate a useful subset.
        f = (flag or "").strip()
        if f.startswith("--proxy-server="):
            self._proxy = f.split("=", 1)[1].strip() or self._proxy
        elif f.startswith("--window-position="):
            raw = f.split("=", 1)[1].strip().replace(" ", "")
            parts = raw.split(",")
            if len(parts) == 2:
                try:
                    self._window_position = (int(parts[0]), int(parts[1]))
                except Exception:
                    pass
        elif f.startswith("--window-size="):
            raw = f.split("=", 1)[1].strip().replace(" ", "")
            parts = raw.split(",")
            if len(parts) == 2:
                try:
                    self._window = (int(parts[0]), int(parts[1]))
                except Exception:
                    pass
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

    def set_window(self, size: tuple[int, int] | None):
        """Outer window size (width, height) for Camoufox `window=`."""
        if size is None:
            self._window = None
            return self
        try:
            self._window = (int(size[0]), int(size[1]))
        except Exception:
            pass
        return self

    def set_window_position(self, x: int, y: int):
        """Desktop position applied after launch (not a Firefox CLI flag)."""
        try:
            self._window_position = (int(x), int(y))
        except Exception:
            pass
        return self

    def set_firefox_pref(self, key: str, value: Any):
        if key:
            self._firefox_user_prefs[str(key)] = value
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

        # Merge firefox prefs (background-timer relief when window is off-screen).
        prefs = dict(self._firefox_user_prefs) if self._firefox_user_prefs else {}
        if self._window_position is not None:
            # Off-screen / background windows get aggressive timer throttling in
            # Firefox; loosen it so register polling / humanize delays stay sane.
            prefs.setdefault("dom.min_background_timeout_value", 4)
            prefs.setdefault("dom.min_background_timeout_value_without_budget_throttling", 4)
            prefs.setdefault("dom.timeout.background_budget_regeneration_rate", 1000)
            prefs.setdefault("dom.timeout.background_throttling_max_budget", -1)
        if prefs:
            existing = kw.get("firefox_user_prefs")
            if isinstance(existing, dict):
                merged = dict(existing)
                merged.update(prefs)
                kw["firefox_user_prefs"] = merged
            else:
                kw["firefox_user_prefs"] = prefs

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
    is_headless = bool(kw.get("headless"))
    need_place = bool(getattr(opts, "_window_position", None)) and not is_headless

    last_err: Exception | None = None

    for attempt in range(1, 5):
        try:
            # Serialize launch+place so multi-thread workers don't steal each
            # other's newly created HWNDs when placing off-screen windows.
            with _window_place_lock:
                before = (
                    _win_enum_mozilla_hwnds(include_hidden=True) if need_place else set()
                )
                if need_place and user_data_dir:
                    pos = opts._window_position or (-2400, 100)
                    size = opts._window or (1000, 800)
                    _seed_firefox_xulstore(
                        user_data_dir,
                        x=int(pos[0]),
                        y=int(pos[1]),
                        w=int(size[0]),
                        h=int(size[1]),
                    )

                # While NewBrowser blocks, race-hide any new Mozilla windows so
                # offscreen mode barely flashes (headless mode needs no watcher).
                stop_watch = threading.Event()
                watch_thread: threading.Thread | None = None
                if need_place:

                    def _watch():
                        pos = opts._window_position or (-2400, 100)
                        size = opts._window or (1000, 800)
                        while not stop_watch.is_set():
                            _place_new_browser_windows(
                                before,
                                x=int(pos[0]),
                                y=int(pos[1]),
                                w=int(size[0]),
                                h=int(size[1]),
                                retries=1,
                                delay_s=0.0,
                                hide=True,
                            )
                            time.sleep(0.01)

                    watch_thread = threading.Thread(
                        target=_watch, name="camoufox-win-place", daemon=True
                    )
                    watch_thread.start()

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
                        browser_wrap = PatchrightBrowser(
                            getattr(context, "browser", None) or context,
                            context,
                            opts,
                            is_persistent_context=True,
                        )
                    else:
                        browser = NewBrowser(pw, **kw)
                        # Prefer a single shared context (multi-tab like Chromium)
                        context = browser.new_context()
                        if not context.pages:
                            context.new_page()
                        browser_wrap = PatchrightBrowser(
                            browser, context, opts, is_persistent_context=False
                        )
                finally:
                    if watch_thread is not None:
                        stop_watch.set()
                        try:
                            watch_thread.join(timeout=1.0)
                        except Exception:
                            pass

                if need_place:
                    apply_window_placement(opts, before_hwnds=before)
                return browser_wrap
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
