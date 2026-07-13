"""Resolve outbound proxy for CPA mint HTTP + browser + register pool.

Priority (highest first) for resolve_proxy():
  1. explicit argument
  2. thread-local runtime pin (set_runtime_proxy)
  3. environment https_proxy / HTTPS_PROXY / http_proxy / HTTP_PROXY

Proxy pool (optional):
  - File: one proxy URL per line (http://user:pass@host:port)
  - Modes: random | round_robin
  - next_pool_proxy() advances and pins thread-local
  - 429 / CONNECT failures can disable proxies (moved to *.disabled.txt)
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

_thread = threading.local()
_pool_lock = threading.Lock()
_pool_list: list[str] = []
_pool_index = 0
_pool_file: str | None = None
_pool_mtime: float | None = None
_disabled: set[str] = set()  # normalized full URLs
_disabled_hosts: set[str] = set()  # host:port keys for fuzzy match
_pool_exhausted: bool = False  # auto-disable pool when empty mid-run
_pool_exhausted_logged: bool = False


def set_runtime_proxy(proxy: str | None) -> None:
    """Pin proxy for the *current thread*. Empty clears pin."""
    p = (proxy or "").strip()
    _thread.proxy = p or None


def get_runtime_proxy() -> str | None:
    return getattr(_thread, "proxy", None)


def resolve_proxy(explicit: str | None = None) -> str:
    for cand in (
        (explicit or "").strip(),
        (get_runtime_proxy() or "").strip(),
        (os.environ.get("https_proxy") or "").strip(),
        (os.environ.get("HTTPS_PROXY") or "").strip(),
        (os.environ.get("http_proxy") or "").strip(),
        (os.environ.get("HTTP_PROXY") or "").strip(),
    ):
        if cand:
            return cand
    return ""


def proxy_for_chromium(proxy: str) -> str:
    """Chromium --proxy-server cannot embed user:pass; host:port only."""
    p = (proxy or "").strip()
    if not p:
        return ""
    u = urlparse(p if "://" in p else f"http://{p}")
    host = u.hostname or ""
    if not host:
        return ""
    port = u.port or (443 if (u.scheme or "http") == "https" else 80)
    scheme = u.scheme or "http"
    if scheme.startswith("socks"):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}:{port}"


def proxy_auth_parts(proxy: str) -> tuple[str, str, str, str, int]:
    """Return (scheme, user, password, host, port)."""
    p = (proxy or "").strip()
    if not p:
        return "", "", "", "", 0
    p = p.split()[0].rstrip(")',\"")
    try:
        u = urlparse(p if "://" in p else f"http://{p}")
        host = u.hostname or ""
        try:
            port = int(u.port or (443 if (u.scheme or "http") == "https" else 80))
        except Exception:
            m = re.search(r":(\d{2,5})$", p)
            port = int(m.group(1)) if m else 0
        return (u.scheme or "http"), (u.username or ""), (u.password or ""), host, port
    except Exception:
        return "", "", "", "", 0


def proxy_host_key(proxy: str) -> str:
    """host:port identity (ignores credentials)."""
    _s, _u, _p, host, port = proxy_auth_parts(proxy)
    if not host:
        return ""
    return f"{host}:{port}" if port else host


def proxy_log_label(proxy: str) -> str:
    """Redact userinfo for logs."""
    p = (proxy or "").strip()
    if not p:
        return ""
    try:
        u = urlparse(p if "://" in p else f"http://{p}")
        host = u.hostname or "?"
        port = u.port or ""
        auth = "user:***@" if u.username else ""
        return f"{u.scheme or 'http'}://{auth}{host}{(':' + str(port)) if port else ''}"
    except Exception:
        return "(proxy)"


def _normalize_proxy_line(line: str) -> str:
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return ""
    if "://" not in s:
        s = "http://" + s
    return s


def _disabled_path_for(pool_file: str) -> Path:
    p = Path(pool_file)
    return p.with_name(p.stem + ".disabled" + p.suffix)


def _load_disabled_file(pool_file: str) -> None:
    global _disabled, _disabled_hosts
    path = _disabled_path_for(pool_file)
    disabled: set[str] = set()
    hosts: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            # format: proxy # reason time
            raw = line.split("#", 1)[0].strip()
            p = _normalize_proxy_line(raw)
            if not p:
                continue
            disabled.add(p)
            hk = proxy_host_key(p)
            if hk:
                hosts.add(hk)
    _disabled = disabled
    _disabled_hosts = hosts


def _is_disabled_locked(proxy: str) -> bool:
    p = (proxy or "").strip()
    if not p:
        return True
    if p in _disabled:
        return True
    hk = proxy_host_key(p)
    return bool(hk and hk in _disabled_hosts)


def is_proxy_failure(err: object) -> bool:
    """True if error looks like a dead/overloaded proxy."""
    s = str(err or "").lower()
    if not s:
        return False
    needles = (
        "response 429",
        " 429",
        "http 429",
        "status=429",
        "status 429",
        "connect tunnel failed",
        "proxy connect aborted",
        "proxy connect",
        "tunnel failed",
        "proxy error",
        "failed to connect to proxy",
        "connection refused",
        "could not resolve proxy",
        "proxy connection timed out",
        "recv failure",
        "connection reset",
        "remote end closed connection",
        "ssl connect error",
        "curl: (56)",
        "curl: (7)",
        "curl: (28)",
        "curl: (35)",
        "curl: (97)",
    )
    return any(n in s for n in needles)


def disable_proxy(proxy: str, reason: str = "", *, pool_file: str | None = None) -> bool:
    """Disable a proxy: memory filter + append disabled file + remove from active pool list/file.

    Returns True if newly disabled (or already disabled).
    """
    global _pool_list
    p = _normalize_proxy_line(proxy) or (proxy or "").strip()
    if not p:
        return False
    hk = proxy_host_key(p)
    reason = (reason or "bad").replace("\n", " ")[:200]
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    with _pool_lock:
        already = _is_disabled_locked(p)
        _disabled.add(p)
        if hk:
            _disabled_hosts.add(hk)
        # drop from in-memory pool (match by host:port)
        if _pool_list:
            keep = []
            for item in _pool_list:
                if item == p or (hk and proxy_host_key(item) == hk):
                    continue
                keep.append(item)
            _pool_list = keep
            if not _pool_list:
                global _pool_exhausted
                _pool_exhausted = True

        pf = pool_file or _pool_file
        if pf:
            # append disabled log
            dpath = _disabled_path_for(pf)
            try:
                with open(dpath, "a", encoding="utf-8") as fh:
                    fh.write(f"{p}  # {ts} {reason}\n")
            except Exception:
                pass
            # remove matching lines from active pool file
            try:
                path = Path(pf)
                if path.is_file():
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    new_lines = []
                    removed = 0
                    for line in lines:
                        raw = line.strip()
                        if not raw or raw.startswith("#"):
                            new_lines.append(line)
                            continue
                        np = _normalize_proxy_line(raw)
                        if np == p or (hk and proxy_host_key(np) == hk):
                            removed += 1
                            continue
                        new_lines.append(line)
                    if removed:
                        # backup once per disable burst is heavy; lightweight .bak on first change of process
                        bak = path.with_suffix(path.suffix + ".bak")
                        if not bak.exists():
                            try:
                                bak.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                            except Exception:
                                pass
                        path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
                        # force reload mtime next time
                        global _pool_mtime
                        _pool_mtime = None
            except Exception:
                pass
        return not already


def mark_runtime_proxy_bad(reason: str = "") -> str:
    """Disable current thread proxy if present. Returns disabled proxy or ''."""
    p = (get_runtime_proxy() or "").strip()
    if not p:
        return ""
    if is_proxy_failure(reason) or reason:
        disable_proxy(p, reason=reason or "runtime_bad")
        set_runtime_proxy(None)
        return p
    return ""


def load_proxy_pool(file_path: str, *, force: bool = False) -> list[str]:
    """Load/reload proxy list from file. Returns current active list."""
    global _pool_list, _pool_file, _pool_mtime, _pool_index
    path = str(Path(file_path).expanduser())
    try:
        st = os.stat(path)
        mtime = float(st.st_mtime)
    except OSError:
        with _pool_lock:
            _pool_list = []
            _pool_file = path
            _pool_mtime = None
        return []

    with _pool_lock:
        if (
            not force
            and _pool_file == path
            and _pool_mtime == mtime
            and _pool_list is not None
        ):
            # still reload disabled file lightly? skip if same mtime
            return list(_pool_list)

        _load_disabled_file(path)
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        items = []
        seen = set()
        for line in lines:
            p = _normalize_proxy_line(line)
            if not p or p in seen:
                continue
            u = urlparse(p)
            if not u.hostname:
                continue
            if _is_disabled_locked(p):
                continue
            seen.add(p)
            items.append(p)
        _pool_list = items
        _pool_file = path
        _pool_mtime = mtime
        if _pool_index >= max(len(_pool_list), 1):
            _pool_index = 0
        return list(_pool_list)


def pool_size() -> int:
    with _pool_lock:
        return len(_pool_list)


def disabled_count() -> int:
    with _pool_lock:
        return len(_disabled_hosts) or len(_disabled)


def is_pool_exhausted() -> bool:
    return bool(_pool_exhausted)


def mark_pool_exhausted(reason: str = "") -> None:
    """Stop using proxy pool for the rest of this process; clear thread pin."""
    global _pool_exhausted, _pool_exhausted_logged, _pool_list
    with _pool_lock:
        _pool_exhausted = True
        _pool_list = []
    set_runtime_proxy(None)
    # one-shot log is done by callers via note_pool_exhausted_message()


def note_pool_exhausted_message(reason: str = "") -> str:
    global _pool_exhausted_logged
    msg = (
        "[proxy] pool exhausted"
        + (f" ({reason})" if reason else "")
        + " — auto disable proxy pool, fallback to direct/config.proxy"
    )
    with _pool_lock:
        first = not _pool_exhausted_logged
        _pool_exhausted_logged = True
    return msg if first else (
        "[proxy] pool still empty — continue without proxy pool"
    )


def reset_pool_exhausted() -> None:
    """Allow re-enabling pool after reloading a non-empty file (optional)."""
    global _pool_exhausted, _pool_exhausted_logged
    with _pool_lock:
        _pool_exhausted = False
        _pool_exhausted_logged = False


def pool_enabled_effective(cfg: dict | None) -> bool:
    """Config wants pool AND pool not auto-exhausted."""
    cfg = cfg or {}
    if not cfg.get("proxy_pool_enabled", False):
        return False
    if is_pool_exhausted():
        return False
    return True


def next_pool_proxy(mode: str = "random") -> str:
    """Take next proxy from pool and pin to current thread. Empty if pool empty.

    When the active list becomes empty, auto-mark pool exhausted so callers stop
    using the pool and fall back to direct / config.proxy.
    """
    global _pool_index, _pool_list, _pool_exhausted
    if _pool_exhausted:
        set_runtime_proxy(None)
        return ""
    mode = (mode or "random").strip().lower()
    with _pool_lock:
        # filter any stale disabled
        active = [p for p in _pool_list if not _is_disabled_locked(p)]
        _pool_list = active
        if not _pool_list:
            _pool_exhausted = True
            set_runtime_proxy(None)
            return ""
        if mode == "round_robin":
            proxy = _pool_list[_pool_index % len(_pool_list)]
            _pool_index = (_pool_index + 1) % len(_pool_list)
        else:
            proxy = random.choice(_pool_list)
    set_runtime_proxy(proxy)
    return proxy


def ensure_pool_from_config(cfg: dict | None) -> int:
    """Load pool if enabled in config. Returns pool size.

    If pool was auto-exhausted and file still empty, keep disabled.
    If file has proxies again (manual refill + process restart, or force), size>0.
    """
    global _pool_exhausted
    cfg = cfg or {}
    if not cfg.get("proxy_pool_enabled", False):
        return 0
    if _pool_exhausted:
        # allow recovery if file was refilled this process
        f = str(cfg.get("proxy_pool_file") or "all_proxies.txt").strip()
        if f and not os.path.isabs(f):
            try:
                from grok_register.paths import PROJECT_ROOT
                f = str(Path(PROJECT_ROOT) / f)
            except Exception:
                f = str(Path.cwd() / f)
        items = load_proxy_pool(f, force=True) if f else []
        if items:
            with _pool_lock:
                _pool_exhausted = False
            return len(items)
        return 0
    f = str(cfg.get("proxy_pool_file") or "all_proxies.txt").strip()
    if not f:
        return 0
    if not os.path.isabs(f):
        try:
            from grok_register.paths import PROJECT_ROOT

            f = str(Path(PROJECT_ROOT) / f)
        except Exception:
            f = str(Path.cwd() / f)
    items = load_proxy_pool(f)
    if not items:
        with _pool_lock:
            _pool_exhausted = True
    return len(items)


def write_chromium_proxy_auth_extension(proxy: str, dest_dir: str) -> str | None:
    """Create a MV2 Chrome extension that sets proxy + handles user:pass auth."""
    scheme, user, password, host, port = proxy_auth_parts(proxy)
    if not host or not port:
        return None
    if not user:
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    def esc(s: str) -> str:
        return (
            (s or "")
            .replace("\\", "\\\\")
            .replace("'", "\\'")
            .replace("\n", "")
            .replace("\r", "")
        )

    chrome_scheme = "http"
    if scheme.startswith("socks5"):
        chrome_scheme = "socks5"
    elif scheme.startswith("socks4"):
        chrome_scheme = "socks4"
    elif scheme == "https":
        chrome_scheme = "https"

    background = f"""// generated proxy auth extension
var config = {{
  mode: "fixed_servers",
  rules: {{
    singleProxy: {{
      scheme: "{chrome_scheme}",
      host: "{esc(host)}",
      port: {int(port)}
    }},
    bypassList: ["localhost", "127.0.0.1"]
  }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function() {{}});
function callbackFn(details) {{
  return {{
    authCredentials: {{
      username: "{esc(user)}",
      password: "{esc(password)}"
    }}
  }};
}}
chrome.webRequest.onAuthRequired.addListener(
  callbackFn,
  {{urls: ["<all_urls>"]}},
  ["blocking"]
);
"""
    manifest = """{
  "version": "1.0.0",
  "manifest_version": 2,
  "name": "GrokRegisterProxyAuth",
  "permissions": [
    "proxy",
    "tabs",
    "unlimitedStorage",
    "storage",
    "<all_urls>",
    "webRequest",
    "webRequestBlocking"
  ],
  "background": {
    "scripts": ["background.js"]
  },
  "minimum_chrome_version": "22.0.0"
}
"""
    (dest / "background.js").write_text(background, encoding="utf-8")
    (dest / "manifest.json").write_text(manifest, encoding="utf-8")
    return str(dest)


def extract_proxy_from_text(text: str) -> str:
    """Best-effort extract proxy URL from log/error text."""
    s = text or ""
    m = re.search(r"https?://[^\s'\"<>]+@\d{1,3}(?:\.\d{1,3}){3}:\d+", s)
    if m:
        return m.group(0).rstrip(")',\"")
    m = re.search(r"proxy=(https?://\S+)", s)
    if m:
        return m.group(1).rstrip(")',\"")
    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})", s)
    if m:
        return "http://" + m.group(1)
    return ""
