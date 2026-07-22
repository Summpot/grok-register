#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - 核心注册逻辑（CLI 使用）
邮箱、浏览器、grok2api 入池等共享实现。
"""

import threading
import datetime
import time
import os
import sys
import gc
import queue
import secrets
import struct
import random
import re
import string
import json
from pathlib import Path
import traceback

# Windows PowerShell / cmd may run Python with a legacy GBK stdout/stderr
# encoding.  The target pages sometimes contain private-use icon glyphs
# (for example "\ue1be") in button text.  If those strings are included in a
# debug log, a plain print() can raise:
#   UnicodeEncodeError: 'gbk' codec can't encode character ...
# Keep console logging non-fatal and prefer UTF-8 with replacement.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def safe_print(*args, **kwargs):
    """Print without letting terminal encoding errors abort registration."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(arg) for arg in args)
        stream = kwargs.get("file") or sys.stdout
        try:
            stream.write(text.encode("utf-8", "replace").decode("utf-8", "replace") + end)
            if kwargs.get("flush"):
                stream.flush()
        except Exception:
            # Last-resort ASCII fallback; logging must never break the flow.
            stream.write(text.encode("ascii", "replace").decode("ascii") + end)
            if kwargs.get("flush"):
                stream.flush()

from grok_register.browser_adapter import (
    Chromium,
    ChromiumOptions,
    PageDisconnectedError,
    click_turnstile_checkbox,
    find_turnstile_boxes,
    solve_turnstile_patient,
    turnstile_token_len,
    turnstile_token_value,
)
from curl_cffi import requests as curl_requests
import requests as std_requests


from grok_register.paths import PROJECT_ROOT, CONFIG_FILE as _CONFIG_PATH, CRASH_LOG_FILE as _CRASH_PATH, OUTPUT_DIR, TOKEN_JSON, ensure_output_dir
CONFIG_FILE = str(_CONFIG_PATH)
CRASH_LOG_FILE = str(_CRASH_PATH)


def write_crash_log(title, exc_type=None, exc_value=None, exc_tb=None):
    try:
        with open(CRASH_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"{datetime.datetime.now().isoformat()} {title}\n")
            if exc_type is not None:
                traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except Exception:
        pass


def _global_excepthook(exc_type, exc_value, exc_tb):
    write_crash_log("UNHANDLED", exc_type, exc_value, exc_tb)
    try:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    except Exception:
        pass


sys.excepthook = _global_excepthook
if hasattr(threading, "excepthook"):
    def _thread_excepthook(args):
        write_crash_log(f"THREAD {getattr(args.thread, 'name', '')}", args.exc_type, args.exc_value, args.exc_traceback)
    threading.excepthook = _thread_excepthook
MEMORY_CLEANUP_INTERVAL = 5

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/api/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    # When true, pass enableRandomSubdomain to cloudflare_temp_email so each
    # address becomes name@<random>.base-domain (requires Worker
    # RANDOM_SUBDOMAIN_DOMAINS + wildcard MX on the base domain).
    "enable_random_subdomain": False,
    "proxy": "http://127.0.0.1:7890",
    "enable_nsfw": True,
    "register_count": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "grok2api_auto_add_local": False,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    # When true and Device Flow produced Build OAuth, upload that credential to
    # remote grok2api Build pool. Never calls remote Web→Build convert API.
    "grok2api_auto_add_build": False,
    "grok2api_remote_base": "",
    "grok2api_remote_app_key": "",
    # legacy | v3 | auto  (auto: prefer chenyme v3 admin API, fall back to legacy /tokens)
    "grok2api_remote_mode": "auto",
    # v3 admin username/password (Bearer login). app_key kept for legacy.
    "grok2api_remote_username": "admin",
    "grok2api_remote_password": "",
    # v3 web tier: auto | basic | super | heavy
    "grok2api_v3_web_tier": "auto",
    # Local Web SSO → Build OAuth Device Flow (aligned with grok-build device_code).
    # Browser-only verify/approve via the registration page; no HTTP fallback.
    "local_build_device_flow": False,
    "local_build_auth_dir": "./output/build_auths",
    # When true, Build access_token with bot_flag_source=1 is still saved/uploaded
    # and counted as success. Default false: reject as registration failure.
    "allow_bot_flagged": False,
    # Per-attempt telemetry (Turnstile randoms, proxy, domain, bot_flag outcome).
    # JSONL → output/reg_stats.jsonl; analyze: python -m grok_register.reg_stats
    "reg_stats_enabled": True,
    "reg_stats_file": "output/reg_stats.jsonl",
    "yyds_preferred_domains": "",
    "yyds_blocked_domains": "",
    "yyds_domain_selection": "random",
    "max_mail_retry": 3,
    "code_poll_timeout": 60,
    "code_poll_interval": 3,
    "register_threads": 1,
    "thread_start_interval": 0.8,
    "register_browser_background": True,
    # headless = no OS window (Camoufox stealth); offscreen = headed + move off-screen
    "register_browser_background_mode": "headless",
    "register_browser_window_position": "-2400,100",
    "register_browser_window_size": "1000,800",
    "proxy_pool_enabled": True,
    "proxy_pool_file": "all_proxies.txt",
    "proxy_pool_mode": "random",
    "proxy_pool_rotate_each_account": True,
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
_yyds_runtime_blocked_domains = set()


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    pass


class EmailDomainRejected(Exception):
    pass


def split_config_list(value):
    return [x.strip().lower() for x in str(value or "").replace(";", ",").split(",") if x.strip()]


def email_domain(address):
    text = str(address or "").strip().lower()
    return text.rsplit("@", 1)[-1] if "@" in text else ""


def config_int(name, default, minimum=None, maximum=None):
    try:
        value = int(config.get(name, default) or default)
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def get_max_mail_retry():
    return config_int("max_mail_retry", 3, minimum=1, maximum=20)


def get_code_poll_timeout():
    return config_int("code_poll_timeout", 60, minimum=15, maximum=300)


def get_code_poll_interval():
    return config_int("code_poll_interval", 3, minimum=1, maximum=30)


def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception as e:
            safe_print(f"[!] 读取配置失败，已回退默认配置: {e}")
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        safe_print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        safe_print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        safe_print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


if __name__ == "__main__":
    ensure_stable_python_runtime()
    warn_runtime_compatibility()

load_config()

DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    """HTTP(S) proxies for requests/curl.

    Priority: thread-local pool pin > config.proxy
    """
    try:
        from grok_register.proxyutil import get_runtime_proxy

        runtime = (get_runtime_proxy() or "").strip()
    except Exception:
        runtime = ""
    proxy = runtime or str(config.get("proxy", "") or "").strip()
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def assign_thread_proxy(log_callback=None, *, force_new: bool = False) -> str:
    """Assign a proxy for the current register thread.

    When proxy_pool_enabled, takes next from pool (and pins thread-local).
    Otherwise uses config.proxy / existing runtime pin.
    """
    log = log_callback or (lambda m: None)
    try:
        from grok_register.proxyutil import (
            ensure_pool_from_config,
            get_runtime_proxy,
            next_pool_proxy,
            proxy_log_label,
            set_runtime_proxy,
        )
    except Exception as exc:
        log(f"[proxy] proxyutil unavailable: {exc}")
        return str(config.get("proxy", "") or "").strip()

    if config.get("proxy_pool_enabled", False):
        from grok_register.proxyutil import (
            is_pool_exhausted,
            note_pool_exhausted_message,
            pool_size,
        )

        n = ensure_pool_from_config(config)
        if n <= 0 or is_pool_exhausted():
            log(note_pool_exhausted_message("empty or exhausted"))
            # auto stop using pool for rest of run
            try:
                config["proxy_pool_enabled"] = False
            except Exception:
                pass
            p = str(config.get("proxy", "") or "").strip()
            set_runtime_proxy(p or None)
            if p:
                log(f"[proxy] fallback config.proxy={proxy_log_label(p)}")
            else:
                log("[proxy] fallback direct (no proxy)")
            return p
        cur = (get_runtime_proxy() or "").strip()
        if cur and not force_new and not config.get("proxy_pool_rotate_each_account", True):
            return cur
        p = next_pool_proxy(str(config.get("proxy_pool_mode") or "random"))
        if not p:
            log(note_pool_exhausted_message("next_pool empty"))
            try:
                config["proxy_pool_enabled"] = False
            except Exception:
                pass
            fb = str(config.get("proxy", "") or "").strip()
            set_runtime_proxy(fb or None)
            if fb:
                log(f"[proxy] fallback config.proxy={proxy_log_label(fb)}")
            else:
                log("[proxy] fallback direct (no proxy)")
            return fb
        log(f"[proxy] assigned {proxy_log_label(p)} (pool_size={pool_size() or n})")
        return p

    p = str(config.get("proxy", "") or "").strip()
    set_runtime_proxy(p or None)
    return p


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "none") or "none").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def cloudflare_next_default_domain():
    """按配置轮换选择 Cloudflare 临时邮箱域名。"""
    global _cf_domain_index
    domains = [x.strip() for x in str(config.get("defaultDomains", "") or "").split(",") if x.strip()]
    if not domains:
        return ""
    domain = domains[_cf_domain_index % len(domains)]
    _cf_domain_index += 1
    return domain


def cloudflare_is_admin_create_path(path):
    """判断当前创建邮箱路径是否为 cloudflare_temp_email 管理员创建接口。"""
    return str(path or "").rstrip("/").lower() == "/admin/new_address"


def cloudflare_enable_random_subdomain():
    """是否在创建地址时请求随机三级域名（name@随机串.基础域）。"""
    value = config.get("enable_random_subdomain", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email 新建地址接口并兼容 admin 创建模式。"""
    path = get_cloudflare_path("cloudflare_path_accounts", "/api/new_address")
    url = f"{api_base}{path}"
    domain = cloudflare_next_default_domain()
    is_admin_create = cloudflare_is_admin_create_path(path)
    use_random_subdomain = cloudflare_enable_random_subdomain()
    if is_admin_create:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        if use_random_subdomain:
            # domain must stay the base domain from defaultDomains / RANDOM_SUBDOMAIN_DOMAINS
            payload["enableRandomSubdomain"] = True
        headers = cloudflare_build_headers(content_type=True)
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
        if use_random_subdomain:
            payload["enableRandomSubdomain"] = True
        headers = {"Content-Type": "application/json"}
    resp = http_post(url, json=payload, headers=headers)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare {path} 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare {path} 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return str(TOKEN_JSON)


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    data = {}
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    pool = data.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token in existing:
        if log_callback:
            log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
        return True
    entry = {"token": token, "tags": ["auto-register"], "note": email}
    pool.append(entry)
    data[pool_name] = pool
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def get_grok2api_remote_api_bases(base):
    """生成 legacy grok2api 管理 API 候选根路径。

    参数:
      - base str: 用户配置的 grok2api 远端地址

    返回:
      - list[str]: 依次尝试的管理 API 根路径
    """
    normalized = str(base or "").strip().rstrip("/")
    if not normalized:
        return []
    lower = normalized.lower()
    candidates = [normalized]
    if lower.endswith("/admin/api"):
        return candidates
    if lower.endswith("/admin"):
        candidates.append(f"{normalized}/api")
    else:
        candidates.append(f"{normalized}/admin/api")
    seen = set()
    unique = []
    for item in candidates:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def get_grok2api_v3_root(base):
    """Normalize site root for chenyme grok2api v3 admin API.

    Accepts:
      - http://host:5003
      - http://host:5003/
      - http://host:5003/api/admin/v1
    """
    normalized = str(base or "").strip().rstrip("/")
    if not normalized:
        return ""
    lower = normalized.lower()
    for suffix in ("/api/admin/v1", "/api/admin", "/admin/api", "/admin"):
        if lower.endswith(suffix):
            return normalized[: -len(suffix)].rstrip("/")
    return normalized


def _grok2api_remote_mode():
    mode = str(config.get("grok2api_remote_mode", "auto") or "auto").strip().lower()
    if mode in ("v3", "chenyme", "go", "new"):
        return "v3"
    if mode in ("legacy", "old", "python", "jiujiu", "v2"):
        return "legacy"
    return "auto"


def _map_pool_to_v3_web_tier(pool_name="", explicit_tier=""):
    tier = str(explicit_tier or config.get("grok2api_v3_web_tier", "auto") or "auto").strip().lower()
    if tier in ("basic", "super", "heavy", "auto"):
        if tier != "auto":
            return tier
    pool = str(pool_name or config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip().lower()
    if pool in ("ssosuper", "super"):
        return "super"
    if pool in ("ssoheavy", "heavy"):
        return "heavy"
    if pool in ("ssobasic", "basic"):
        return "basic"
    return "auto"


# process-local cache: root -> (access_token, expire_epoch)
_grok2api_v3_token_cache = {}


def _grok2api_v3_login(root, username, password, log_callback=None):
    """Login admin and return accessToken. Uses short-lived process cache."""
    import time

    global _grok2api_v3_token_cache
    root = str(root or "").strip().rstrip("/")
    username = str(username or "").strip()
    password = str(password or "").strip()
    if not root or not username or not password:
        raise RuntimeError("grok2api v3 需要 remote_base + username + password")
    now = time.time()
    cached = _grok2api_v3_token_cache.get(root)
    if cached and cached[0] and cached[1] > now + 30:
        return cached[0]
    endpoint = f"{root}/api/admin/v1/auth/login"
    resp = http_post(
        endpoint,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        json={"username": username, "password": password},
        timeout=30,
        proxies={},
    )
    if getattr(resp, "status_code", 0) >= 400:
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            body = ""
        raise RuntimeError(f"v3 login HTTP {resp.status_code}: {body}")
    payload = resp.json() if hasattr(resp, "json") else {}
    data = payload.get("data") if isinstance(payload, dict) else None
    tokens = (data or {}).get("tokens") if isinstance(data, dict) else None
    access = ""
    if isinstance(tokens, dict):
        access = str(tokens.get("accessToken") or tokens.get("access_token") or "").strip()
    if not access and isinstance(data, dict):
        access = str(data.get("accessToken") or data.get("access_token") or "").strip()
    if not access:
        raise RuntimeError("v3 login 响应缺少 accessToken")
    # access tokens are short-lived; cache ~12 minutes
    _grok2api_v3_token_cache[root] = (access, now + 12 * 60)
    if log_callback:
        log_callback(f"[*] grok2api v3 admin 登录成功: {root}")
    return access


def _parse_v3_sse_complete(text):
    """Parse last event: complete data JSON from v3 import SSE stream."""
    if not text:
        return None
    current_event = ""
    last_complete = None
    for raw in str(text).splitlines():
        line = raw.rstrip("\r")
        if not line:
            current_event = ""
            continue
        if line.startswith("event:"):
            current_event = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if current_event == "complete" and data_str:
                try:
                    last_complete = json.loads(data_str)
                except Exception:
                    last_complete = {"raw": data_str}
    return last_complete


def _parse_go_import_sse(text: str) -> dict | None:
    """Parse Go backend SSE stream and return the last complete data payload.

    Go backend emits lines like:
      data: {"created":1,"synced":0,"updated":0}
    Unlike v3 Python backend, Go backend does NOT emit event: complete markers.
    We match any data: line containing 'created' or 'synced'.
    """
    if not text:
        return None
    last_complete: dict | None = None
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped.startswith("data: "):
            try:
                parsed = json.loads(stripped[6:])
                if isinstance(parsed, dict) and ("created" in parsed or "synced" in parsed):
                    last_complete = parsed
            except (json.JSONDecodeError, ValueError):
                continue
    return last_complete


def convert_sso_to_build_local(
    raw_token,
    email="",
    log_callback=None,
    page=None,
) -> dict | None:
    """Run local Web SSO → Build Device Flow after registration.

    Returns the OAuth seed dict on success, or None if disabled / failed.

    Special flags on the returned seed:
      - ``_bot_flagged``: access_token JWT has bot_flag_source=1. By default
        treated as registration failure (nothing saved/imported); when
        ``allow_bot_flagged`` is true, still save/import and mark the flag.
      - ``_remote_build_imported``: Build OAuth uploaded to grok2api.

    When Device Flow succeeds (and not rejected as bot-flagged), callers must
    NOT import Grok Web for this account — only Build.
    """
    if not config.get("local_build_device_flow", False):
        return None
    token = _normalize_sso_token(raw_token)
    if not token:
        if log_callback:
            log_callback("[Debug] local Device Flow: empty SSO, skip")
        return None
    try:
        from grok_register.sso_build import (
            access_token_has_bot_flag,
            convert_sso_to_build,
            save_build_auth,
            SSOBuildError,
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] local Device Flow import failed: {exc}")
        return None

    ua = str(config.get("user_agent", "") or "").strip()
    proxies = get_proxies()
    active_page = page
    if active_page is None:
        try:
            active_page = _get_page()
        except Exception:
            active_page = None
    if active_page is None:
        if log_callback:
            log_callback(
                "[!] 本地 Device Flow 需要注册浏览器 page（仅 browser 模式，无 HTTP 回退）"
            )
        return None

    if log_callback:
        log_callback("[*] 本地 Web→Build Device Flow 开始 (mode=browser)")
    try:
        seed = convert_sso_to_build(
            token,
            email=email,
            user_agent=ua,
            proxies=proxies,
            page=active_page,
            mode="browser",
            log_callback=log_callback,
        )
    except SSOBuildError as exc:
        if log_callback:
            log_callback(f"[!] 本地 Device Flow 失败: {exc}")
        return None
    except Exception as exc:
        if log_callback:
            log_callback(f"[!] 本地 Device Flow 异常: {exc}")
        return None

    seed["_bot_flagged"] = False
    seed["_remote_build_imported"] = False

    # Bot-flagged Build tokens: reject by default; allow when allow_bot_flagged=true.
    if access_token_has_bot_flag(seed.get("access_token")):
        seed["_bot_flagged"] = True
        try:
            from grok_register.reg_stats import safe_jwt_claims, update_attempt

            update_attempt(
                access_token=seed.get("access_token"),
                bot_flagged=True,
                jwt_claims=safe_jwt_claims(seed.get("access_token")),
            )
        except Exception:
            pass
        if not config.get("allow_bot_flagged", False):
            if log_callback:
                log_callback(
                    "[!] access_token 含 bot_flag_source=1，视为注册失败，"
                    "不保存/导入 Grok Build，也不导入 Grok Web"
                    "（可设 allow_bot_flagged=true 强制继续）"
                )
            return seed
        if log_callback:
            log_callback(
                "[!] access_token 含 bot_flag_source=1，但 allow_bot_flagged=true，"
                "继续保存/导入"
            )
    else:
        try:
            from grok_register.reg_stats import update_attempt

            update_attempt(access_token=seed.get("access_token"), bot_flagged=False)
        except Exception:
            pass

    auth_dir = str(config.get("local_build_auth_dir", "") or "").strip()
    if not auth_dir:
        auth_dir = str(OUTPUT_DIR / "build_auths")
    try:
        path = save_build_auth(seed, auth_dir, email=email)
        if log_callback:
            log_callback(
                f"[+] 本地 Build OAuth 已保存: {path} "
                f"(email={seed.get('email') or email or '?'})"
            )
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 保存 Build OAuth 文件失败: {exc}")

    # Device Flow 成功：只导入 Build，不再走 Web 池 / 远端 convert。
    if config.get("grok2api_auto_add_remote", False) and config.get(
        "grok2api_auto_add_build", False
    ):
        try:
            ok = add_build_credential_to_grok2api_remote(
                seed, email=email, log_callback=log_callback
            )
            seed["_remote_build_imported"] = bool(ok)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 远端 Build 导入失败（本地凭据已保存）: {exc}")
    elif config.get("grok2api_auto_add_build", False) and log_callback:
        # Build 开关开着但远端 Web 关着：仍只本地落盘
        log_callback("[*] Device Flow 完成：已跳过 Grok Web 导入（仅本地 Build）")
    return seed


def apply_post_register_pools(
    sso_token,
    *,
    email="",
    log_callback=None,
    page=None,
) -> dict:
    """Device Flow + grok2api pool routing after SSO is obtained.

    Returns a result dict:
      ok: bool — overall registration acceptance
      bot_flagged: bool
      build_seed: dict | None
      skipped_web: bool — True when Device Flow succeeded (Web must not be imported)
    """
    result = {
        "ok": True,
        "bot_flagged": False,
        "build_seed": None,
        "skipped_web": False,
    }
    build_seed = None
    try:
        build_seed = convert_sso_to_build_local(
            sso_token, email=email, log_callback=log_callback, page=page
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] local Device Flow: {exc}")
        build_seed = None
    result["build_seed"] = build_seed

    if build_seed and build_seed.get("_bot_flagged"):
        result["bot_flagged"] = True
        if not config.get("allow_bot_flagged", False):
            result["ok"] = False
            result["skipped_web"] = True
            return result
        # Allowed: treat as Build success (already saved/imported above).
        result["skipped_web"] = True
        if log_callback:
            log_callback(
                "[*] bot_flag_source=1 已允许：跳过 Grok Web 导入（仅 Grok Build）"
            )
        return result

    if build_seed:
        # Device Flow succeeded → Build only, never import Grok Web.
        result["skipped_web"] = True
        if log_callback:
            log_callback("[*] Device Flow 成功：跳过 Grok Web 导入（仅 Grok Build）")
        return result

    # No local Device Flow result → keep legacy Web import path.
    try:
        add_token_to_grok2api_pools(
            sso_token,
            email=email,
            log_callback=log_callback,
            skip_build_convert=False,
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] grok2api: {exc}")
    return result


def add_build_credential_to_grok2api_remote(seed: dict, email="", log_callback=None) -> bool:
    """Upload a local Build OAuth seed to chenyme grok2api v3 accounts/import."""
    if not isinstance(seed, dict):
        return False
    if not (seed.get("access_token") or seed.get("refresh_token")):
        return False
    mode = _grok2api_remote_mode()
    if mode not in ("v3", "auto"):
        if log_callback:
            log_callback("[Debug] 远端 Build 导入仅支持 v3/auto，跳过")
        return False
    try:
        from grok_register.sso_build import build_grok2api_import_document
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Build import document: {exc}")
        return False
    root, _user, _pw = _grok2api_v3_credentials()
    if not root:
        if log_callback:
            log_callback("[Debug] grok2api v3 未配置 remote_base，跳过 Build 导入")
        return False
    document = build_grok2api_import_document(seed)
    file_bytes = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
    label = (email or seed.get("email") or seed.get("user_id") or "build").strip()
    filename = f"build-{label.replace('@', '_').replace('/', '_')[:48]}.json"
    endpoint = f"{root}/api/admin/v1/accounts/import"
    complete = _grok2api_v3_multipart_import(
        endpoint=endpoint,
        file_bytes=file_bytes,
        filename=filename,
        log_callback=log_callback,
        label="build import",
    )
    if complete is False:
        return False
    if isinstance(complete, dict):
        created = complete.get("created", "?")
        updated = complete.get("updated", "?")
        synced = complete.get("synced", complete.get("syncSucceeded", "?"))
        if log_callback:
            log_callback(
                f"[+] 已写入 grok2api v3 Build 池: created={created} updated={updated} "
                f"synced={synced} ({endpoint})"
            )
    else:
        if log_callback:
            log_callback(f"[+] 已写入 grok2api v3 Build 池: {label} ({endpoint})")
    return True


def _grok2api_v3_credentials():
    """Return (root, username, password) for v3 admin API."""
    base = str(config.get("grok2api_remote_base", "") or "").strip()
    root = get_grok2api_v3_root(base)
    username = str(config.get("grok2api_remote_username", "admin") or "admin").strip() or "admin"
    password = str(config.get("grok2api_remote_password", "") or "").strip()
    if not password:
        password = str(config.get("grok2api_remote_app_key", "") or "").strip()
    return root, username, password


def _grok2api_v3_multipart_import(
    *,
    endpoint: str,
    file_bytes: bytes,
    filename: str,
    log_callback=None,
    label: str = "import",
):
    """Login + multipart POST to a v3 admin import endpoint.

    Returns complete payload dict, True on success without body, or False if skipped.
    """
    root, username, password = _grok2api_v3_credentials()
    if not root:
        if log_callback:
            log_callback("[Debug] grok2api v3 未配置 remote_base，跳过")
        return False
    if not password:
        if log_callback:
            log_callback("[Debug] grok2api v3 未配置 remote_password（或 app_key 作密码），跳过")
        return False

    access = _grok2api_v3_login(root, username, password, log_callback=log_callback)

    def _post_import(bearer: str):
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "text/event-stream, application/json",
        }
        files = {"file": (filename, file_bytes, "application/json")}
        # std requests for multipart; curl_cffi needs CurlMime objects
        return std_requests.post(
            endpoint,
            headers=headers,
            files=files,
            timeout=120,
            proxies={},
        )

    try:
        resp = _post_import(access)
    except Exception:
        global _grok2api_v3_token_cache
        _grok2api_v3_token_cache.pop(root, None)
        access = _grok2api_v3_login(root, username, password, log_callback=log_callback)
        resp = _post_import(access)

    status = int(getattr(resp, "status_code", 0) or 0)
    body_text = ""
    try:
        body_text = resp.text or ""
    except Exception:
        body_text = ""

    if status in (401, 403):
        _grok2api_v3_token_cache.pop(root, None)
        access = _grok2api_v3_login(root, username, password, log_callback=log_callback)
        resp = _post_import(access)
        status = int(getattr(resp, "status_code", 0) or 0)
        try:
            body_text = resp.text or ""
        except Exception:
            body_text = ""

    if status >= 400:
        raise RuntimeError(f"v3 {label} HTTP {status}: {body_text[:240]}")

    complete = _parse_v3_sse_complete(body_text)
    if complete is None:
        try:
            payload = resp.json()
            if isinstance(payload, dict):
                complete = payload.get("data") if "data" in payload else payload
        except Exception:
            complete = None
    return complete if complete is not None else True


def add_token_to_grok2api_remote_pool_v3(
    raw_token, email="", log_callback=None, skip_build_convert=False
):
    """Upload one SSO token to chenyme grok2api v3 Grok Web pool.

    API:
      POST {root}/api/admin/v1/auth/login
      POST {root}/api/admin/v1/accounts/web/import  (multipart files/file)

    Does not call remote Web→Build convert. Build credentials are only uploaded
    via local Device Flow + add_build_credential_to_grok2api_remote.
    skip_build_convert is accepted for call-site compatibility and ignored.
    """
    del skip_build_convert  # retained for API compatibility; convert path removed
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    root, _user, _pw = _grok2api_v3_credentials()
    if not root:
        if log_callback:
            log_callback("[Debug] grok2api v3 未配置 remote_base，跳过")
        return False
    tier = _map_pool_to_v3_web_tier()
    name = (email or "").strip() or f"auto-{token[:8]}"
    document = {
        "provider": "grok_web",
        "accounts": [
            {
                "name": name,
                "sso_token": token,
                "tier": tier,
            }
        ],
    }
    file_bytes = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
    filename = f"web-{name.replace('@', '_').replace('/', '_')[:48]}.json"
    endpoint = f"{root}/api/admin/v1/accounts/web/import"
    complete = _grok2api_v3_multipart_import(
        endpoint=endpoint,
        file_bytes=file_bytes,
        filename=filename,
        log_callback=log_callback,
        label="web import",
    )
    if complete is False:
        return False
    if isinstance(complete, dict):
        created = complete.get("created", "?")
        updated = complete.get("updated", "?")
        synced = complete.get("synced", complete.get("syncSucceeded", "?"))
        if log_callback:
            log_callback(
                f"[+] 已写入 grok2api v3 Web 池: created={created} updated={updated} "
                f"synced={synced} ({endpoint})"
            )
    else:
        if log_callback:
            log_callback(f"[+] 已写入 grok2api v3 Web 池: {name} ({endpoint})")
    return True


def add_token_to_grok2api_remote_pool_legacy(raw_token, email="", log_callback=None):
    """Legacy jiujiu/Python grok2api token pool upload (/tokens/add)."""
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_remote_base", "") or "").strip().rstrip("/")
    app_key = str(config.get("grok2api_remote_app_key", "") or "").strip()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    if not base or not app_key:
        if log_callback:
            log_callback("[Debug] grok2api legacy 远端未配置 base/app_key，跳过")
        return False
    headers = {"Content-Type": "application/json"}
    query = {"app_key": app_key}
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    remote_pool = pool_map.get(pool_name, "basic")
    api_bases = get_grok2api_remote_api_bases(base)
    add_errors = []
    add_payload = {"tokens": [token], "pool": remote_pool, "tags": ["auto-register"]}
    for api_base in api_bases:
        endpoint = f"{api_base}/tokens/add"
        try:
            resp_add = http_post(
                endpoint,
                headers=headers,
                params=query,
                json=add_payload,
                timeout=30,
                proxies={},
            )
            resp_add.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池(legacy): {pool_name} ({endpoint})")
            return True
        except Exception as add_exc:
            add_errors.append(f"{endpoint}: {add_exc}")
    if log_callback:
        log_callback(f"[Debug] /tokens/add 写入失败，尝试 /tokens 全量模式: {'; '.join(add_errors)}")

    current = {}
    fallback_base = api_bases[0] if api_bases else base
    for api_base in api_bases or [base]:
        try:
            resp = http_get(f"{api_base}/tokens", headers=headers, params=query, timeout=20, proxies={})
            if resp.status_code == 200:
                payload = resp.json()
                current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
                fallback_base = api_base
                break
        except Exception:
            continue
    if not isinstance(current, dict):
        current = {}
    pool = current.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token not in existing:
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
    current[pool_name] = pool
    save_errors = []
    save_bases = []
    for item in [fallback_base, *(api_bases or [base])]:
        if item and item not in save_bases:
            save_bases.append(item)
    for api_base in save_bases:
        try:
            resp2 = http_post(f"{api_base}/tokens", headers=headers, params=query, json=current, timeout=30, proxies={})
            resp2.raise_for_status()
            if log_callback:
                log_callback(f"[+] 已写入 grok2api 远端池(legacy): {pool_name} ({api_base}/tokens)")
            return True
        except Exception as save_exc:
            save_errors.append(f"{api_base}/tokens: {save_exc}")
    raise RuntimeError(f"grok2api 远端 /tokens 全量模式写入失败: {'; '.join(save_errors)}")


def add_token_to_grok2api_remote_pool(
    raw_token, email="", log_callback=None, skip_build_convert=False
):
    """Upload SSO token to remote grok2api (v3 preferred, legacy fallback)."""
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    mode = _grok2api_remote_mode()
    errors = []
    if mode in ("v3", "auto"):
        try:
            return add_token_to_grok2api_remote_pool_v3(
                raw_token,
                email=email,
                log_callback=log_callback,
                skip_build_convert=skip_build_convert,
            )
        except Exception as exc:
            errors.append(f"v3: {exc}")
            if mode == "v3":
                raise
            if log_callback:
                log_callback(f"[Debug] grok2api v3 导入失败，尝试 legacy: {exc}")
    if mode in ("legacy", "auto"):
        try:
            return add_token_to_grok2api_remote_pool_legacy(
                raw_token, email=email, log_callback=log_callback
            )
        except Exception as exc:
            errors.append(f"legacy: {exc}")
            if mode == "legacy":
                raise
    raise RuntimeError("grok2api 远端写入失败: " + "; ".join(errors) if errors else "未知错误")


def add_token_to_grok2api_pools(
    raw_token, email="", log_callback=None, skip_build_convert=False
):
    if config.get("grok2api_auto_add_local", False):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_auto_add_remote", False):
        try:
            add_token_to_grok2api_remote_pool(
                raw_token,
                email=email,
                log_callback=log_callback,
                skip_build_convert=skip_build_convert,
            )
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


CHROMIUM_SLIM_FLAGS = ()
# Camoufox: fingerprint is handled at engine level; only translate a few
# Chrome-era flags (proxy / window position-size) in the adapter.


def _register_browser_background_enabled() -> bool:
    try:
        return bool(config.get("register_browser_background", True))
    except Exception:
        return True


def _apply_register_background_options(options) -> None:
    """Run registration browser with no desktop window flash.

    Camoufox path: use stealth-patched **headless** so the OS window never
    appears. (Chrome-era code used --window-position off-screen; Firefox has
    no equivalent launch flag, and post-launch SetWindowPos still flashes once.)

    Optional config register_browser_background_mode:
      - "headless" (default): true invisible, recommended for Camoufox
      - "offscreen": headed + move window off-screen (may flash briefly)
    Window size still applied for a stable viewport/fingerprint.
    """
    if not _register_browser_background_enabled():
        return

    size = str(config.get("register_browser_window_size") or "1000,800").strip().replace(
        " ", ""
    )
    if not re.match(r"^\d+,\d+$", size):
        size = "1000,800"
    try:
        sw, sh = (int(x) for x in size.split(","))
    except Exception:
        sw, sh = 1000, 800

    try:
        options.set_window((sw, sh))
    except Exception:
        try:
            options.set_argument(f"--window-size={sw},{sh}")
        except Exception:
            pass

    mode = str(config.get("register_browser_background_mode") or "headless").strip().lower()
    if mode in {"offscreen", "off-screen", "position", "window-position"}:
        # Headed fallback: place off-screen after launch (may flash once).
        pos = str(
            config.get("register_browser_window_position") or "-2400,100"
        ).strip().replace(" ", "")
        if not re.match(r"^-?\d+,-?\d+$", pos):
            pos = "-2400,100"
        try:
            px, py = (int(x) for x in pos.split(","))
        except Exception:
            px, py = -2400, 100
        try:
            options.set_window_position(px, py)
        except Exception:
            try:
                options.set_argument(f"--window-position={px},{py}")
            except Exception:
                pass
        try:
            options.headless(False)
        except Exception:
            pass
        return

    # Default: Camoufox stealth headless — no OS window, no flash.
    try:
        options.headless(True)
    except Exception:
        pass


def create_browser_options(*, unique_profile: bool = False, profile_tag: str = "reg"):
    """Build Camoufox launch options for registration.

    Uses stealth Firefox (Camoufox) with humanize cursor, disable_coop for
    Turnstile iframe clicks, and native Playwright proxy auth (user:pass).

    When config.register_browser_background is true (default), Camoufox runs
    headless (stealth-patched) so no window appears. Set
    register_browser_background_mode to "offscreen" for headed+off-screen.
    """
    options = ChromiumOptions()
    options.set_timeouts(base=2)
    try:
        options.set_humanize(True)
        options.set_disable_coop(True)
        options.set_os("windows")
    except Exception:
        pass

    _apply_register_background_options(options)

    # Outbound proxy (pool pin or config.proxy) — Camoufox accepts user:pass natively
    proxy: str = ""
    try:
        from grok_register.proxyutil import get_runtime_proxy

        proxy = (get_runtime_proxy() or str(config.get("proxy", "") or "")).strip()
        if proxy:
            try:
                options.set_proxy(proxy)
            except Exception:
                pass
            try:
                options.set_geoip(True)
            except Exception:
                pass
    except Exception:
        pass

    if unique_profile:
        import tempfile
        import uuid

        profile_dir = os.path.join(
            tempfile.gettempdir(),
            "grok_reg_camoufox",
            f"{profile_tag}_{os.getpid()}_{uuid.uuid4().hex[:10]}",
        )
        os.makedirs(profile_dir, exist_ok=True)
        try:
            options.set_user_data_path(profile_dir)
        except Exception:
            try:
                options.set_paths(user_data_path=profile_dir)
            except Exception:
                pass
    try:
        options.auto_port()
    except Exception:
        pass
    return options


def _human_pause_cancel(lo: float = 0.18, hi: float = 0.55, cancel_callback=None) -> None:
    """Random pause that still respects cancel_callback."""
    if hi < lo:
        lo, hi = hi, lo
    sleep_with_cancel(random.uniform(lo, hi), cancel_callback)


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def _is_tls_backend_error(exc):
    err = str(exc).lower()
    return (
        "tls connect error" in err
        or "openssl_internal:invalid library" in err
        or "curl: (35)" in err
        or "invalid library (0)" in err
    )


def _to_std_request_kwargs(kwargs):
    std_kwargs = _build_request_kwargs(**kwargs)
    # curl_cffi accepts extra options that requests does not.
    for key in ("impersonate", "default_headers", "http_version"):
        std_kwargs.pop(key, None)
    return std_kwargs


def http_get(url, **kwargs):
    try:
        return curl_requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if _is_tls_backend_error(exc):
            return std_requests.get(url, **_to_std_request_kwargs(kwargs))
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            try:
                return curl_requests.get(url, **_build_request_kwargs(**retry_kwargs))
            except Exception as retry_exc:
                if _is_tls_backend_error(retry_exc):
                    return std_requests.get(url, **_to_std_request_kwargs(retry_kwargs))
                raise
        raise


def http_post(url, **kwargs):
    try:
        return curl_requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if _is_tls_backend_error(exc):
            return std_requests.post(url, **_to_std_request_kwargs(kwargs))
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            try:
                return curl_requests.post(url, **_build_request_kwargs(**retry_kwargs))
            except Exception as retry_exc:
                if _is_tls_backend_error(retry_exc):
                    return std_requests.post(url, **_to_std_request_kwargs(retry_kwargs))
                raise
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("鐢ㄦ埛鍋滄娉ㄥ唽")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鍒涘缓閭澶辫触: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 鑾峰彇token澶辫触: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 鑾峰彇閭欢璇︽儏澶辫触: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    global _yyds_runtime_blocked_domains
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    blocked = set(split_config_list(config.get("yyds_blocked_domains", "")))
    blocked.update(_yyds_runtime_blocked_domains)
    verified = [
        d for d in domains
        if d.get("isVerified") and str(d.get("domain", "")).strip().lower() not in blocked
    ]
    preferred = split_config_list(config.get("yyds_preferred_domains", ""))
    if preferred:
        domain_map = {str(d.get("domain", "")).strip().lower(): d for d in verified}
        for name in preferred:
            if name in domain_map:
                return domain_map[name]["domain"]
    private = [d for d in verified if not d.get("isPublic")]
    if private:
        random.shuffle(private)
        return private[0]["domain"]
    public = [d for d in verified if d.get("isPublic")]
    if public:
        if str(config.get("yyds_domain_selection", "random")).lower() == "random":
            random.shuffle(public)
        return public[0]["domain"]
    if verified:
        return verified[0]["domain"]
    raise Exception(f"YYDS 鏃犲凡楠岃瘉鍩熷悕鍙敤，已排除: {', '.join(sorted(blocked)) or 'none'}")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("鑾峰彇 YYDS token 澶辫触")
    safe_print(f"[*] 宸插垱寤?YYDS 閭: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    last_wait_log = 0
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        now = time.time()
        if log_callback and now - last_wait_log >= 15:
            left = max(0, int(deadline - now))
            log_callback(f"[Debug] YYDS 等待验证码中，剩余 {left}s")
            last_wait_log = now
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 娌℃湁杩斿洖浠讳綍鍙敤鍩熷悕")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 鏃犲凡楠岃瘉鍩熷悕鍙敤")


def get_email_provider():
    return config.get("email_provider", "duckmail")


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("鑾峰彇 DuckMail token 澶辫触")
    return address, token


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 鎷夊彇閭欢鍒楄〃澶辫触: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 鑾峰彇閭欢璇︽儏澶辫触: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 鏀跺埌閭欢: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 浠庨偖浠朵腑鎻愬彇鍒伴獙璇佺爜: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def response_preview(res, limit=200):
    try:
        text = str(res.text or "")
    except Exception:
        text = ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def is_cloudflare_block_response(res):
    try:
        headers = {str(k).lower(): str(v).lower() for k, v in dict(res.headers).items()}
        text = str(res.text or "").lower()
        server = headers.get("server", "")
        content_type = headers.get("content-type", "")
        return (
            res.status_code in (403, 429, 503)
            and (
                "cloudflare" in server
                or "cloudflare" in text
                or "cf-error" in text
                or "__cf_chl" in text
                or "text/html" in content_type
            )
        )
    except Exception:
        return False


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_birth_date 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_birth_date HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "set_tos_accepted 被 accounts.x.ai 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"set_tos_accepted HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] update_nsfw status: {res.status_code}, body: {response_preview(res)}"
            )
        if 200 <= res.status_code < 300:
            return True, "ok"
        if is_cloudflare_block_response(res):
            return (
                False,
                "update_nsfw_settings 被 grok.com 的 Cloudflare 防护拦截，HTTP "
                f"{res.status_code}",
            )
        return False, f"update_nsfw_settings HTTP {res.status_code}: {response_preview(res)}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with curl_requests.Session(impersonate="chrome120", proxies=proxies) as session:
            cookie_parts = [f"sso={token}", f"sso-rw={token}"]
            if cf_clearance:
                cookie_parts.append(f"cf_clearance={cf_clearance}")
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": "; ".join(cookie_parts),
                }
            )
            ok, message = set_tos_accepted(session, log_callback)
            if not ok:
                return False, message
            ok, message = set_birth_date(session, log_callback)
            if not ok:
                return False, message
            ok, message = update_nsfw_settings(session, log_callback)
            if not ok:
                return False, message
            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

# Thread-local browser/page for multi-thread CLI safety.
_tls = threading.local()
_browser_registry_lock = threading.Lock()
_all_thread_browsers = []


def _register_thread_browser(b):
    if b is None:
        return
    with _browser_registry_lock:
        if all(x is not b for x in _all_thread_browsers):
            _all_thread_browsers.append(b)


def _unregister_thread_browser(b):
    if b is None:
        return
    with _browser_registry_lock:
        _all_thread_browsers[:] = [x for x in _all_thread_browsers if x is not b]


def _tls_get_browser():
    return getattr(_tls, "browser", None)


def _tls_set_browser(value):
    old = getattr(_tls, "browser", None)
    if old is not None and old is not value:
        _unregister_thread_browser(old)
    _tls.browser = value
    if value is not None:
        _register_thread_browser(value)
    return value


def _tls_get_page():
    return getattr(_tls, "page", None)


def _tls_set_page(value):
    _tls.page = value
    return value


def _bind_thread_browser_globals():
    """Load thread-local browser/page into module globals (legacy single-thread paths)."""
    global browser, page
    browser = _tls_get_browser()
    page = _tls_get_page()
    return browser, page


def _sync_thread_browser_globals():
    """Mirror module globals into TLS only when safe.

    Never overwrite this thread's TLS browser with another thread's object.
    Multi-thread code should prefer _tls_set_* on local objects; module
    globals are a best-effort legacy mirror for single-thread paths.
    """
    global browser, page
    tls_b = _tls_get_browser()
    tls_p = _tls_get_page()
    if browser is not None and (tls_b is None or browser is tls_b):
        _tls_set_browser(browser)
    elif browser is None and tls_b is None:
        pass
    if page is not None and (tls_p is None or page is tls_p):
        _tls_set_page(page)


def _mirror_thread_browser_globals(b, p):
    """Best-effort module-global mirror; TLS remains source of truth.

    Pass explicit values (including None) — never fall back to another
    thread's module-global state.
    """
    global browser, page
    browser = b
    page = p
    return browser, page


def _is_playwright_thread_error(exc: BaseException) -> bool:
    """True when Playwright sync objects are used off their creating thread."""
    msg = str(exc) if exc is not None else ""
    if "Cannot switch to a different thread" in msg:
        return True
    mod = (getattr(type(exc), "__module__", "") or "").lower()
    if "greenlet" in mod and "thread" in msg.lower():
        return True
    return False


def _ensure_thread_browser(log_callback=None, *, force_restart: bool = False):
    """Return (browser, page) owned by the current thread."""
    if force_restart:
        return restart_browser(log_callback=log_callback)
    browser = _tls_get_browser()
    page = _tls_get_page()
    if browser is None or page is None:
        return start_browser(log_callback=log_callback)
    return browser, page


browser = None
page = None



def start_browser(log_callback=None):
    # TLS is the source of truth. Module globals are only a legacy mirror —
    # never read them back into TLS (that races across register workers and
    # causes: greenlet.error: Cannot switch to a different thread).
    try:
        existing = _tls_get_browser()
    except Exception:
        existing = None
    if existing is not None:
        try:
            stop_browser()
        except Exception:
            pass
    # Ensure this thread has a proxy pin before building Chromium options.
    try:
        assign_thread_proxy(log_callback, force_new=False)
    except Exception:
        pass
    last_exc = None
    for attempt in range(1, 5):
        new_browser = None
        new_page = None
        _auto_port = None
        try:
            try:
                opts = create_browser_options(unique_profile=True, profile_tag="reg")
            except TypeError:
                opts = create_browser_options()
            # Extract the auto-assigned debug port BEFORE Chromium() constructor
            # so we can precisely kill the subprocess if the constructor fails.
            _auto_addr = (
                getattr(opts, 'address', None)
                or getattr(opts, '_address', None)
                or ''
            )
            if ':' in str(_auto_addr):
                try:
                    _auto_port = int(str(_auto_addr).rsplit(':', 1)[-1])
                except (ValueError, IndexError):
                    _auto_port = None
            new_browser = Chromium(opts)
            tabs = new_browser.get_tabs()
            new_page = tabs[-1] if tabs else new_browser.new_tab()
            _tls_set_browser(new_browser)
            _tls_set_page(new_page)
            _mirror_thread_browser_globals(new_browser, new_page)
            if log_callback and getattr(new_browser, "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {new_browser.user_data_path}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return new_browser, new_page
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            try:
                if new_browser is not None:
                    new_browser.quit(del_data=True)
                elif _auto_port is not None:
                    # Chromium() constructor spawned Chrome but failed to
                    # connect. Kill the orphan by its exact debug port so we
                    # never touch other threads' Chrome processes.
                    if _kill_chrome_by_port(_auto_port):
                        if log_callback:
                            log_callback(f"[Debug] 已清理端口 {_auto_port} 上的孤儿 Chrome")
            except Exception:
                pass
            # Clear only this thread's TLS. Do not wipe module globals —
            # another worker may currently mirror its own browser there.
            _tls_set_browser(None)
            _tls_set_page(None)
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    # Quit only THIS thread's browser. Do not funnel through module globals —
    # concurrent workers race on those and can steal each other's objects.
    b = _tls_get_browser()
    p = _tls_get_page()
    _user_data_path = None
    if b is not None:
        try:
            _user_data_path = getattr(b, "user_data_path", None)
        except Exception:
            pass
        try:
            b.quit(del_data=True)
        except TypeError:
            try:
                b.quit()
            except Exception:
                pass
        except Exception:
            try:
                b.quit()
            except Exception:
                pass
        _unregister_thread_browser(b)
    _tls_set_browser(None)
    _tls_set_page(None)
    # Clear module globals only if they still point at this thread's objects.
    global browser, page
    if browser is b:
        browser = None
    if page is p:
        page = None
    # Force-kill any Chrome still alive on this profile / auto port after quit
    if _user_data_path:
        try:
            _kill_chrome_by_profile(str(_user_data_path))
        except Exception:
            pass


def restart_browser(log_callback=None):
    stop_browser()
    return start_browser(log_callback=log_callback)


def prepare_browser_for_next_account(log_callback=None):
    """Clear cookies/storage between accounts; restart on failure.

    When proxy pool rotates each account, restart browser so the new proxy
    (and auth extension) takes effect.
    """
    browser = _tls_get_browser()
    page = _tls_get_page()
    if config.get("proxy_pool_enabled") and config.get("proxy_pool_rotate_each_account", True):
        try:
            assign_thread_proxy(log_callback, force_new=True)
        except Exception:
            pass
        if log_callback:
            log_callback("[proxy] rotate → restart browser for next account")
        restart_browser(log_callback=log_callback)
        return True
    try:
        if page is not None:
            try:
                page.get("about:blank")
            except Exception:
                pass
            for js in (
                "try{localStorage.clear()}catch(e){}",
                "try{sessionStorage.clear()}catch(e){}",
            ):
                try:
                    page.run_js(js)
                except Exception:
                    pass
        if browser is not None:
            try:
                browser.set.cookies.clear()
            except Exception:
                try:
                    cks = browser.cookies()
                    if isinstance(cks, list):
                        for c in cks:
                            try:
                                browser.set.cookies.remove(c)
                            except Exception:
                                pass
                except Exception:
                    pass
        if log_callback:
            log_callback("[*] 浏览器会话已清理，准备下一账号")
        return True
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 会话清理失败，重启浏览器: {exc}")
        if _is_playwright_thread_error(exc):
            # Drop poisoned TLS ref before restart.
            _tls_set_browser(None)
            _tls_set_page(None)
        restart_browser(log_callback=log_callback)
        return False


def shutdown_browser():
    stop_browser()
    # Browsers created on other worker threads are greenlet-bound to those
    # threads (Playwright sync). Workers already call stop_browser() on exit;
    # only best-effort quit leftovers here — never share one Playwright across
    # threads. Orphan process kill covers anything that remains.
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
        from grok_register.browser_adapter import stop_thread_playwright

        stop_thread_playwright()
    except Exception:
        pass


def _kill_chrome_by_port(port: int) -> bool:
    """Kill any Chrome.exe bound to the given debugging port using taskkill /F.

    Lightweight helper for cleaning up orphans that DrissionPage spawned but
    lost track of (e.g. Chromium() constructor failed after subprocess start).
    Returns True if at least one process was killed.
    """
    if os.name != "nt":
        return False
    import subprocess
    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                f'Get-CimInstance Win32_Process -Filter "name=\'chrome.exe\' and CommandLine like \'%--remote-debugging-port={port}%\'" '
                f'| ForEach-Object {{ try {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $true }} catch {{ $false }} }}',
            ],
            text=True, stderr=subprocess.DEVNULL, timeout=10,
        ).strip()
        killed = sum(1 for line in out.splitlines() if line.strip().lower() == "true")
        return killed > 0
    except Exception:
        return False


def _kill_chrome_by_profile(profile_dir: str) -> bool:
    """Kill Chrome processes using a specific user-data-dir."""
    if os.name != "nt" or not profile_dir:
        return False
    import subprocess
    safe_dir = profile_dir.replace("'", "''")
    try:
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-Command",
                f'Get-CimInstance Win32_Process -Filter "name=\'chrome.exe\' and CommandLine like \'%--user-data-dir={safe_dir}%\'" '
                f'| ForEach-Object {{ try {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $true }} catch {{ $false }} }}',
            ],
            text=True, stderr=subprocess.DEVNULL, timeout=10,
        ).strip()
        killed = sum(1 for line in out.splitlines() if line.strip().lower() == "true")
        return killed > 0
    except Exception:
        return False


def kill_orphaned_automation_browsers(log_callback=None, port: int | None = None):
    """Kill leftover Chrome processes started by DrissionPage / this project.

    Only matches automation fingerprints: autoPortData / grok_reg_chrome /
    DrissionPage temp profiles. Normal user Chrome is left alone.
    When port is given, only kill Chrome with that --remote-debugging-port.
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

    _port_arg = f"--remote-debugging-port={port}" if port else ""
    ps_lines = [
        "$patterns = @('autoPortData','grok_reg_chrome','DrissionPage')",
        "$portFilter = " + (f"'%{_port_arg}%'" if port else "$null"),
        "$killed = 0",
        "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | ForEach-Object {",
        "  $cmd = $_.CommandLine",
        "  if (-not $cmd) { return }",
        "  if ($portFilter -and ($cmd -notlike $portFilter)) { return }",
        "  foreach ($pat in $patterns) {",
        "    if ($cmd -match $pat) {",
        "      try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch {}",
        "      break",
        "    }",
        "  }",
        "}",
        "Write-Output $killed",
    ]
    ps = "; ".join(ps_lines)
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

def _get_page():
    _bind_thread_browser_globals()
    return page


def mark_used(email, password=""):
    return None


def mark_error(email="", reason=""):
    return None


def save_cookies_snapshot(page_obj=None, tag="snap", email=""):
    return None


PERF_FLAGS = {
    "fast": False,
    "sleep_scale": 1.0,
    "skip_debug_io": False,
    "cookie_snapshot": False,
    "async_side_effects": True,
    "browser_reuse": True,
    "browser_recycle_every": 25,
}


def configure_perf(
    *,
    fast=False,
    sleep_scale=1.0,
    skip_debug_io=False,
    cookie_snapshot=False,
    async_side_effects=True,
    browser_reuse=True,
    browser_recycle_every=25,
):
    PERF_FLAGS.update(
        {
            "fast": bool(fast),
            "sleep_scale": float(sleep_scale),
            "skip_debug_io": bool(skip_debug_io),
            "cookie_snapshot": bool(cookie_snapshot),
            "async_side_effects": bool(async_side_effects),
            "browser_reuse": bool(browser_reuse),
            "browser_recycle_every": max(1, int(browser_recycle_every)),
        }
    )
    return PERF_FLAGS

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


try:
    from grok_register.tab_pool import TabPool  # type: ignore
except Exception:
    class TabPool:  # pragma: no cover - fallback stub
        @classmethod
        def init(cls, *a, **k):
            return None

        @classmethod
        def get_browser(cls):
            return _tls_get_browser()

        @classmethod
        def get_tab(cls, url=None):
            _bind_thread_browser_globals()
            return page

        @classmethod
        def clear_session(cls, log_callback=None):
            return prepare_browser_for_next_account(log_callback=log_callback)

        @classmethod
        def release_tab(cls):
            stop_browser()

        @classmethod
        def shutdown(cls):
            shutdown_browser()

        @classmethod
        def refresh_tab(cls):
            return restart_browser()


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    if log_callback:
        log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
    stop_browser()
    collected = gc.collect()
    if log_callback:
        log_callback(f"[*] Python GC 已回收对象数: {collected}")


def refresh_active_page():
    # thread-local page/browser (multi-thread safe)
    browser, page = _ensure_thread_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
        _tls_set_page(page)
        _mirror_thread_browser_globals(browser, page)
        return page
    except Exception as exc:
        if _is_playwright_thread_error(exc):
            _tls_set_browser(None)
            _tls_set_page(None)
        browser, page = restart_browser()
        try:
            tabs = browser.get_tabs() if browser is not None else []
            page = tabs[-1] if tabs else (browser.new_tab() if browser is not None else None)
            if page is not None:
                _tls_set_page(page)
            _mirror_thread_browser_globals(browser, page)
        except Exception:
            pass
        return _tls_get_page()


# Labels / selectors for the email signup entry on accounts.x.ai.
# Provider UI copy drifts between locales and A/B variants.
EMAIL_SIGNUP_LABELS = [
    "使用邮箱注册",
    "Sign up with email",
    "Continue with email",
    "Sign up with Email",
    "Continue with Email",
    "Use email",
    "Use Email",
    "Email",
    "邮箱",
    "用邮箱注册",
    "邮箱注册",
]

EMAIL_SIGNUP_SELECTORS = [
    'button:has-text("使用邮箱注册")',
    'a:has-text("使用邮箱注册")',
    '[role="button"]:has-text("使用邮箱注册")',
    'button:has-text("Sign up with email")',
    'button:has-text("Continue with email")',
    'a:has-text("Sign up with email")',
    'a:has-text("Continue with email")',
    'button:has-text("Use email")',
    'button:has-text("Email")',
    'a:has-text("Email")',
    '[role="button"]:has-text("Email")',
    'button:has-text("邮箱")',
    'a:has-text("邮箱")',
]

# NOTE: page.run_js wraps statement bodies as `() => { <code> }`.
# Prefer bare statements with `return` (not IIFE) so the value is not dropped.
# Expression/IIFE form is also supported by browser_adapter._eval_js, but
# statement style matches the rest of this codebase.
_EMAIL_FORM_READY_JS = """
const sels = [
  'input[data-testid="email"]',
  'input[name="email"]',
  'input[type="email"]',
  'input[autocomplete="email"]',
  'input[placeholder*="mail"]',
  'input[placeholder*="Mail"]',
  'input[aria-label*="mail"]',
  'input[aria-label*="Mail"]',
  'input[aria-label*="邮箱"]',
  'input[placeholder*="邮箱"]',
];
for (const sel of sels) {
  let nodes;
  try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
  for (const el of nodes) {
    try {
      const st = window.getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (r.width > 2 && r.height > 2) return true;
    } catch (e) {}
  }
}
return false;
"""

_SIGNUP_PAGE_PROBE_JS = """
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
const lower = (s) => normalize(s).toLowerCase();
const body = lower(document.body && (document.body.innerText || document.body.textContent) || '');
const html = lower(document.documentElement ? document.documentElement.innerHTML.slice(0, 8000) : '');
const challenge = /just a moment|checking your browser|cf-browser-verification|challenge-platform|attention required|enable javascript and cookies/.test(body + ' ' + html)
  || !!(document.querySelector('#challenge-running, #challenge-stage, #cf-challenge-running, .cf-browser-verification, iframe[src*="challenges.cloudflare.com"]'));
const loading = document.readyState !== 'complete'
  || /loading|please wait|正在加载|请稍候/.test(body.slice(0, 200));

const collect = (root, out, depth) => {
  if (!root || depth > 6) return;
  let nodes;
  try {
    nodes = root.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]');
  } catch (e) {
    return;
  }
  for (const el of nodes) {
    let text = '';
    try {
      text = normalize(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '');
    } catch (e) {
      text = '';
    }
    if (!text) continue;
    let visible = false;
    try {
      const st = window.getComputedStyle(el);
      const r = el.getBoundingClientRect();
      visible = st.display !== 'none' && st.visibility !== 'hidden'
        && Number(st.opacity || '1') > 0.05 && r.width > 2 && r.height > 2;
    } catch (e) {
      visible = true;
    }
    out.push({ text: text.slice(0, 80), visible });
  }
  let all;
  try { all = root.querySelectorAll('*'); } catch (e) { return; }
  for (const el of all) {
    try {
      if (el.shadowRoot) collect(el.shadowRoot, out, depth + 1);
    } catch (e) {}
  }
};
const buttons = [];
collect(document, buttons, 0);
const visibleButtons = buttons.filter((b) => b.visible).map((b) => b.text).slice(0, 16);
const emailPatterns = [
  /使用邮箱注册/,
  /用邮箱注册/,
  /邮箱注册/,
  /sign\\s*up\\s*with\\s*e-?mail/i,
  /continue\\s*with\\s*e-?mail/i,
  /use\\s*e-?mail/i,
  /^e-?mail$/i,
  /^邮箱$/,
];
const hasEmailEntry = buttons.some((b) => emailPatterns.some((p) => p.test(b.text)));
return {
  readyState: document.readyState || '',
  challenge: !!challenge,
  loading: !!loading,
  buttonCount: buttons.length,
  visibleButtons,
  hasEmailEntry: !!hasEmailEntry,
  bodySnippet: normalize(document.body && (document.body.innerText || document.body.textContent) || '').slice(0, 220),
  title: document.title || '',
};
"""

_CLICK_EMAIL_SIGNUP_JS = """
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
const patterns = [
  /使用邮箱注册/i,
  /用邮箱注册/i,
  /邮箱注册/i,
  /sign\\s*up\\s*with\\s*e-?mail/i,
  /continue\\s*with\\s*e-?mail/i,
  /use\\s*e-?mail/i,
  /^e-?mail$/i,
  /^邮箱$/,
];
// Prefer explicit email CTAs; avoid matching "email" inside long privacy text.
const isEmailCta = (text) => {
  const t = normalize(text);
  if (!t || t.length > 64) return false;
  return patterns.some((p) => p.test(t));
};
const isVisible = (el) => {
  try {
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity || '1') < 0.05) return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2;
  } catch (e) {
    return true;
  }
};
const collect = (root, out, depth) => {
  if (!root || depth > 6) return;
  let nodes;
  try {
    nodes = root.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]');
  } catch (e) {
    return;
  }
  for (const el of nodes) out.push(el);
  let all;
  try { all = root.querySelectorAll('*'); } catch (e) { return; }
  for (const el of all) {
    try {
      if (el.shadowRoot) collect(el.shadowRoot, out, depth + 1);
    } catch (e) {}
  }
};
const candidates = [];
collect(document, candidates, 0);
for (const el of candidates) {
  let text = '';
  try {
    text = normalize(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '');
  } catch (e) {
    text = '';
  }
  if (!isEmailCta(text) || !isVisible(el)) continue;
  try {
    el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'auto' });
  } catch (e) {}
  try {
    el.click();
    return { ok: true, via: 'js_deep_click', text: text.slice(0, 60) };
  } catch (e) {
    return { ok: false, reason: 'click_threw', text: text.slice(0, 60), error: String(e && e.message || e) };
  }
}
return {
  ok: false,
  reason: 'no_email_cta',
  buttonCount: candidates.length,
  samples: candidates.slice(0, 12).map((el) => {
    try {
      return normalize(el.innerText || el.textContent || el.value || '').slice(0, 40);
    } catch (e) {
      return '';
    }
  }).filter(Boolean),
};
"""


def _signup_email_form_ready(page) -> bool:
    """True when the email input of the signup form is already visible."""
    if page is None:
        return False
    # Prefer Playwright-native probes (works even if run_js wrapping changes).
    try:
        pw = getattr(page, "_p", None)
        if pw is not None:
            for sel in (
                'input[data-testid="email"]',
                'input[name="email"]',
                'input[type="email"]',
                'input[autocomplete="email"]',
            ):
                try:
                    loc = pw.locator(sel)
                    n = loc.count()
                    for i in range(min(n, 4)):
                        if loc.nth(i).is_visible(timeout=200):
                            return True
                except Exception:
                    continue
    except Exception:
        pass
    try:
        return bool(page.run_js(_EMAIL_FORM_READY_JS))
    except Exception:
        return False


def _probe_signup_page(page) -> dict:
    if page is None:
        return {}
    probe: dict = {}
    try:
        raw = page.run_js(_SIGNUP_PAGE_PROBE_JS)
        if isinstance(raw, dict):
            probe = raw
        elif raw is None:
            probe = {"probe_error": "run_js returned None"}
        else:
            probe = {"probe_error": f"unexpected type={type(raw).__name__}"}
    except Exception as exc:
        probe = {"probe_error": str(exc)}

    # Playwright fallback when JS probe is empty / broken (e.g. old IIFE issue).
    if not probe.get("buttonCount") and not probe.get("visibleButtons"):
        try:
            pw = getattr(page, "_p", None)
            if pw is not None:
                texts = []
                for role in ("button", "link"):
                    try:
                        loc = pw.get_by_role(role)
                        count = min(loc.count(), 12)
                        for i in range(count):
                            try:
                                t = (loc.nth(i).inner_text(timeout=400) or "").strip()
                                if t:
                                    texts.append(t[:80])
                            except Exception:
                                continue
                    except Exception:
                        continue
                if texts:
                    probe.setdefault("visibleButtons", texts[:16])
                    probe.setdefault("buttonCount", len(texts))
                    joined = " ".join(texts).lower()
                    probe.setdefault(
                        "hasEmailEntry",
                        any(
                            k in joined
                            for k in (
                                "email",
                                "邮箱",
                                "sign up with email",
                                "continue with email",
                                "use email",
                            )
                        ),
                    )
                try:
                    probe.setdefault("readyState", pw.evaluate("() => document.readyState"))
                    probe.setdefault(
                        "bodySnippet",
                        (pw.evaluate(
                            "() => (document.body && (document.body.innerText || '')) || ''"
                        ) or "")[:220],
                    )
                except Exception:
                    pass
        except Exception as exc:
            probe.setdefault("probe_error", str(exc))
    return probe


def _js_click_email_signup(page) -> dict | None:
    if page is None:
        return None
    try:
        raw = page.run_js(_CLICK_EMAIL_SIGNUP_JS)
        return raw if isinstance(raw, dict) else None
    except Exception as exc:
        return {"ok": False, "reason": f"js_error:{exc}"}


def click_email_signup_button(timeout=28, log_callback=None, cancel_callback=None):
    """Find and click the email signup CTA on accounts.x.ai.

    Hardening vs flaky SPA / CF interstitial:
      - skip if email form already present
      - wait for hydrated auth buttons
      - Playwright role/text/selectors + shadow-DOM JS deep click
      - one mid-timeout reload if page stays empty/challenge
    """
    # thread-local page/browser (multi-thread safe)
    browser, page = _ensure_thread_browser(log_callback=log_callback)
    deadline = time.time() + max(float(timeout or 28), 8.0)
    started = time.time()
    last_status_log = 0.0
    reloaded = 0  # count of mid-timeout reloads
    attempt = 0

    if _signup_email_form_ready(page):
        if log_callback:
            log_callback("[*] 已在邮箱注册表单，跳过入口按钮")
        return True

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        attempt += 1
        page = _tls_get_page() or page
        if page is None:
            browser, page = _ensure_thread_browser(log_callback=log_callback)
        if page is None:
            sleep_with_cancel(0.5, cancel_callback)
            continue

        if _signup_email_form_ready(page):
            if log_callback:
                log_callback("[*] 邮箱输入框已出现（入口可能已自动展开）")
            return True

        probe = _probe_signup_page(page)
        now = time.time()
        btn_count = int(probe.get("buttonCount") or 0)
        empty_shell = btn_count <= 0 and not probe.get("hasEmailEntry")
        if log_callback and (attempt == 1 or now - last_status_log >= 4.0):
            last_status_log = now
            url = ""
            try:
                url = page.url or ""
            except Exception:
                url = ""
            btns = probe.get("visibleButtons") if isinstance(probe, dict) else None
            btn_preview = ""
            if isinstance(btns, list) and btns:
                btn_preview = ", ".join(str(b)[:28] for b in btns[:6])
            err = probe.get("probe_error")
            log_callback(
                f"[Debug] 查找邮箱注册入口 attempt={attempt} "
                f"url={str(url)[:100]} "
                f"ready={probe.get('readyState')!r} "
                f"challenge={probe.get('challenge')} "
                f"buttons={probe.get('buttonCount')} "
                f"has_email_cta={probe.get('hasEmailEntry')} "
                f"visible=[{btn_preview}]"
                + (f" probe_error={err!r}" if err else "")
            )

        # Cloudflare interstitial / empty SPA shell — wait a bit longer.
        if probe.get("challenge") or (
            empty_shell and (probe.get("loading") or not probe.get("readyState"))
        ) or (empty_shell and (now - started) < 6.0):
            sleep_with_cancel(1.0, cancel_callback)
            # Mid-window: hard reload to recover stuck shells (up to 2 times).
            reload_budget = 2
            if (
                reloaded < reload_budget
                and empty_shell
                and (now - started) >= max(float(timeout) * 0.35, 6.0)
            ):
                reloaded += 1
                if log_callback:
                    log_callback(
                        f"[*] 注册页长时间无交互控件，刷新页面重试 "
                        f"({reloaded}/{reload_budget})"
                    )
                try:
                    page.get(SIGNUP_URL)
                    page.wait.doc_loaded()
                    sleep_with_cancel(1.5, cancel_callback)
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] 注册页刷新失败: {exc}")
            continue

        _human_pause_cancel(0.15, 0.4, cancel_callback)
        hit = None

        # 1) Playwright role / text (short timeouts to avoid blowing the budget)
        try:
            hit = page.click_by_text(
                EMAIL_SIGNUP_LABELS, role="button", timeout_ms=1800
            )
        except Exception:
            hit = None
        if not hit:
            try:
                hit = page.click_by_text(
                    EMAIL_SIGNUP_LABELS, role="link", timeout_ms=1200
                )
            except Exception:
                hit = None

        # 2) CSS / has-text selectors
        if not hit:
            try:
                if page.click_first(EMAIL_SIGNUP_SELECTORS, timeout_ms=1800):
                    hit = "selector"
            except Exception:
                pass

        # 3) Shadow-DOM aware JS deep click
        if not hit:
            js_result = _js_click_email_signup(page)
            if isinstance(js_result, dict) and js_result.get("ok"):
                hit = f"js:{js_result.get('via') or 'deep'}:{js_result.get('text') or ''}"
            elif (
                log_callback
                and isinstance(js_result, dict)
                and attempt == 1
                and js_result.get("samples")
            ):
                samples = js_result.get("samples") or []
                log_callback(
                    f"[Debug] JS 未命中邮箱入口，可见候选: "
                    f"{[str(s)[:30] for s in samples[:8]]}"
                )

        if hit:
            if log_callback:
                detail = f": {hit}" if isinstance(hit, str) else ""
                log_callback(f"[*] 已点击「使用邮箱注册」按钮{detail}")
            _human_pause_cancel(0.8, 1.6, cancel_callback)
            # Confirm the email form expands; if not, keep looping a bit.
            form_wait_deadline = min(time.time() + 4.0, deadline)
            while time.time() < form_wait_deadline:
                if _signup_email_form_ready(page):
                    return True
                sleep_with_cancel(0.35, cancel_callback)
            # Click registered even if form probe lags (fill step will re-click).
            return True

        # One proactive reload if UI rendered but still no email CTA.
        if (
            reloaded < 2
            and (now - started) >= max(float(timeout) * 0.55, 10.0)
            and not probe.get("hasEmailEntry")
        ):
            reloaded += 1
            if log_callback:
                log_callback(
                    "[*] 未出现邮箱注册入口，刷新注册页后重试 "
                    f"(visible={probe.get('visibleButtons')!r}, reload={reloaded})"
                )
            try:
                page.get(SIGNUP_URL)
                page.wait.doc_loaded()
                sleep_with_cancel(1.5, cancel_callback)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 注册页刷新失败: {exc}")
            continue

        sleep_with_cancel(0.7, cancel_callback)

    if log_callback:
        try:
            page = _tls_get_page() or page
            probe = _probe_signup_page(page)
            url = page.url if page else "none"
            log_callback(
                f"[Debug] 邮箱入口最终失败 url={url} "
                f"challenge={probe.get('challenge')} "
                f"buttons={probe.get('buttonCount')} "
                f"visible={probe.get('visibleButtons')!r} "
                f"body≈{str(probe.get('bodySnippet') or '')[:160]!r}"
            )
            try:
                page_html = page.html[:400] if page else "no page"
            except Exception:
                page_html = "no page"
            log_callback(f"[Debug] 页面内容片段: {page_html}")
        except Exception as exc:
            log_callback(f"[Debug] 失败诊断异常: {exc}")

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    browser, page = _ensure_thread_browser(log_callback=log_callback)
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser(log_callback=log_callback)
        if log_callback:
            log_callback("[*] 浏览器已启动")

    def _navigate(b, p):
        tab = None
        try:
            tab = b.get_tab(0) if b is not None else None
        except Exception:
            tab = None
        if tab is None:
            tab = b.new_tab(SIGNUP_URL) if b is not None else None
            if tab is not None:
                _tls_set_page(tab)
                _mirror_thread_browser_globals(b, tab)
            return tab
        _tls_set_page(tab)
        tab.get(SIGNUP_URL)
        _mirror_thread_browser_globals(b, tab)
        return tab

    try:
        page = _navigate(browser, page)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        # Cross-thread Playwright objects cannot be recovered in-place.
        if _is_playwright_thread_error(e):
            _tls_set_browser(None)
            _tls_set_page(None)
            browser, page = restart_browser(log_callback=log_callback)
            page = _navigate(browser, page)
        else:
            try:
                page = browser.new_tab(SIGNUP_URL)
                _tls_set_page(page)
                _mirror_thread_browser_globals(browser, page)
            except Exception as e2:
                if log_callback:
                    log_callback(f"[Debug] 创建新标签页异常: {e2}")
                if _is_playwright_thread_error(e2):
                    _tls_set_browser(None)
                    _tls_set_page(None)
                browser, page = restart_browser(log_callback=log_callback)
                # Must use the NEW browser from restart — never the stale local.
                page = browser.new_tab(SIGNUP_URL)
                _tls_set_page(page)
                _mirror_thread_browser_globals(browser, page)

    if page is None:
        browser, page = _ensure_thread_browser(log_callback=log_callback, force_restart=True)
        page = browser.new_tab(SIGNUP_URL)
        _tls_set_page(page)
        _mirror_thread_browser_globals(browser, page)

    page.wait.doc_loaded()
    # SPA shell often paints before auth method buttons hydrate.
    try:
        pw = getattr(page, "_p", None)
        if pw is not None:
            try:
                pw.wait_for_load_state("load", timeout=8000)
            except Exception:
                pass
            try:
                # Best-effort: network settles, but don't fail if long-polls remain.
                pw.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
    except Exception:
        pass
    sleep_with_cancel(1.2, cancel_callback)

    # Brief wait for either email form or any auth CTA to appear.
    hydrate_deadline = time.time() + 8.0
    while time.time() < hydrate_deadline:
        raise_if_cancelled(cancel_callback)
        if _signup_email_form_ready(page):
            break
        probe = _probe_signup_page(page)
        if int(probe.get("buttonCount") or 0) > 0 or probe.get("hasEmailEntry"):
            break
        if probe.get("challenge"):
            sleep_with_cancel(1.0, cancel_callback)
            continue
        sleep_with_cancel(0.4, cancel_callback)

    if log_callback:
        try:
            log_callback(f"[*] 当前URL: {page.url}")
        except Exception:
            log_callback("[*] 当前URL: <unknown>")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(log_callback=None):
    page = refresh_active_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def fill_email_and_submit(timeout=45, log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    browser, page = _ensure_thread_browser(log_callback=log_callback)
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    email_selectors = [
        'input[data-testid="email"]',
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[placeholder*="mail" i]',
        'input[aria-label*="mail" i]',
        'input[aria-label*="邮箱"]',
        'input[placeholder*="邮箱"]',
    ]
    submit_labels = ["注册", "继续", "下一步", "确认", "Sign up", "Continue", "Next", "Submit"]
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        _human_pause_cancel(0.2, 0.5, cancel_callback)

        filled_ok = False
        try:
            filled_ok = page.fill_first(email_selectors, email, human=True)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 原生填写邮箱异常: {exc}")

        if not filled_ok:
            now = time.time()
            if now - last_reclick_time >= 3:
                try:
                    rehit = page.click_by_text(
                        EMAIL_SIGNUP_LABELS,
                        role="button",
                        timeout_ms=1800,
                    )
                    if not rehit:
                        js_hit = _js_click_email_signup(page)
                        if isinstance(js_hit, dict) and js_hit.get("ok"):
                            rehit = f"js:{js_hit.get('text') or 'email'}"
                except Exception:
                    rehit = None
                last_reclick_time = now
                if rehit and log_callback:
                    log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口: {rehit}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                url = page.url if page else ""
                log_callback(f"[Debug] 等待邮箱输入框: url={url}")
            sleep_with_cancel(0.6, cancel_callback)
            continue

        _human_pause_cancel(0.35, 0.8, cancel_callback)

        # Submit via Playwright-native click
        clicked = None
        try:
            clicked = page.click_by_text(submit_labels, role="button")
        except Exception:
            clicked = None
        if not clicked:
            try:
                if page.click_first(
                    [
                        'button[type="submit"]',
                        'button:has-text("注册")',
                        'button:has-text("继续")',
                        'button:has-text("下一步")',
                        'button:has-text("Continue")',
                        'button:has-text("Next")',
                        'button:has-text("Sign up")',
                    ]
                ):
                    clicked = "selector"
            except Exception:
                pass
        if not clicked:
            # Enter key as last native fallback
            try:
                page._p.keyboard.press("Enter")
                clicked = "enter"
            except Exception:
                pass

        if clicked:
            _human_pause_cancel(0.8, 1.4, cancel_callback)
            rejection = ""
            try:
                rejection = page.run_js(
                    r"""
const text = String(document.body?.innerText || document.body?.textContent || '');
const compact = text.replace(/\s+/g, ' ');
const lower = compact.toLowerCase();
if (
  (compact.includes('已被拒绝') && (compact.includes('邮箱域名') || compact.includes('域名'))) ||
  (lower.includes('rejected') && (lower.includes('email') || lower.includes('domain'))) ||
  (lower.includes('email domain') && lower.includes('not allowed'))
) {
  return compact.slice(0, 500);
}
return '';
                    """
                )
            except Exception:
                rejection = ""
            if rejection:
                domain = email_domain(email)
                if domain:
                    _yyds_runtime_blocked_domains.add(domain)
                if log_callback:
                    log_callback(f"[!] 邮箱域名被目标站拒绝，已临时跳过该域名: {domain or email}")
                raise EmailDomainRejected(f"邮箱域名被拒绝: {domain or email}; {rejection}")
            if log_callback:
                detail = f" ({clicked})" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已填写邮箱并提交: {email}{detail}")
            return email, dev_token
        sleep_with_cancel(0.5, cancel_callback)

    raise Exception(f"未找到邮箱输入框或注册按钮，最后页面: url={page.url if page else ''}")


def fill_code_and_submit(
    email,
    dev_token,
    timeout=None,
    poll_interval=None,
    log_callback=None,
    cancel_callback=None,
):
    # thread-local page/browser (multi-thread safe)
    browser, page = _ensure_thread_browser(log_callback=log_callback)

    def _resend_code():
        try:
            page.click_by_text(["重新发送", "再次发送", "Resend"], role="button")
        except Exception:
            pass

    if timeout is None:
        timeout = get_code_poll_timeout()
    if poll_interval is None:
        poll_interval = get_code_poll_interval()
    if log_callback:
        log_callback(f"[*] 等待验证码，最多 {timeout}s；超过即更换邮箱")

    code = get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    deadline = time.time() + timeout
    otp_selectors = [
        'input[data-input-otp="true"]',
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[inputmode="text"]',
    ]

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        _human_pause_cancel(0.2, 0.5, cancel_callback)

        filled_ok = False
        # Prefer single aggregate OTP input
        try:
            filled_ok = page.fill_first(otp_selectors, clean_code, human=True)
        except Exception:
            filled_ok = False

        # Single-char OTP boxes: type char-by-char natively
        if not filled_ok:
            try:
                pw = page._p
                boxes = pw.locator('input[maxlength="1"]')
                n = boxes.count()
                if n >= len(clean_code):
                    for i, ch in enumerate(clean_code):
                        box = boxes.nth(i)
                        _human_pause_cancel(0.08, 0.22, cancel_callback)
                        box.click(timeout=2000)
                        _human_pause_cancel(0.04, 0.12, cancel_callback)
                        box.fill("")
                        box.press_sequentially(ch, delay=random.randint(40, 100))
                    filled_ok = True
            except Exception:
                filled_ok = False

        if not filled_ok:
            sleep_with_cancel(0.5, cancel_callback)
            continue

        _human_pause_cancel(0.35, 0.75, cancel_callback)
        clicked = None
        try:
            clicked = page.click_by_text(
                ["确认邮箱", "继续", "下一步", "Confirm", "Continue", "Next"],
                role="button",
            )
        except Exception:
            clicked = None
        if not clicked:
            try:
                if page.click_first(
                    [
                        'button[type="submit"]',
                        'button:has-text("确认")',
                        'button:has-text("继续")',
                        'button:has-text("下一步")',
                        'button:has-text("Continue")',
                        'button:has-text("Next")',
                    ]
                ):
                    clicked = "selector"
            except Exception:
                clicked = None

        # OTP often auto-submits; treat filled+optional click as success
        if log_callback:
            log_callback(f"[*] 已填写验证码并提交: {code}")
        _human_pause_cancel(1.0, 1.8, cancel_callback)
        return code

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(log_callback=None, cancel_callback=None, timeout=55):
    """Wait for Cloudflare Turnstile with a patient, low-click solve loop.

    Camoufox path: disable_coop + coordinate mouse.click on the widget.
    Unlike the old ~45 rapid re-click loop, this:
      - waits for widget layout to stabilize
      - allows managed auto-solve before any click
      - issues few humanized clicks with multi-second post-click waits
    """
    # thread-local page/browser (multi-thread safe)
    browser, page = _ensure_thread_browser(log_callback=log_callback)
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    pw_page = page._p
    cancel_hit = {"v": False}

    def _log(msg: str) -> None:
        if not log_callback:
            return
        # Surface solver internals with consistent prefix
        if msg.startswith("[") or msg.startswith("turnstile"):
            if msg.startswith("turnstile"):
                log_callback(f"[*] {msg}")
            else:
                log_callback(msg)
        else:
            log_callback(f"[*] {msg}")

    def _sleep(seconds: float) -> None:
        try:
            sleep_with_cancel(seconds, cancel_callback)
        except Exception:
            cancel_hit["v"] = True
            raise

    def _should_cancel() -> bool:
        if cancel_hit["v"]:
            return True
        if not cancel_callback:
            return False
        try:
            raise_if_cancelled(cancel_callback)
            return False
        except Exception:
            cancel_hit["v"] = True
            return True

    # Already solved?
    existing = turnstile_token_value(pw_page)
    if len(existing) >= 80:
        _log(f"Turnstile 已通过，token长度={len(existing)}")
        return existing

    try:
        token = solve_turnstile_patient(
            pw_page,
            log=_log,
            sleep_fn=_sleep,
            should_cancel=_should_cancel,
            max_clicks=4,
            timeout=float(timeout or 55),
            auto_solve_wait=(1.2, 2.9),
            post_click_wait=(2.8, 5.0),
            min_token_len=80,
        )
    except RegistrationCancelled:
        raise
    except Exception as exc:
        if cancel_hit["v"] or "cancelled" in str(exc).lower():
            raise RegistrationCancelled("用户停止注册") from exc
        # One diagnostic snapshot on failure
        try:
            boxes = find_turnstile_boxes(pw_page)
            n = turnstile_token_len(pw_page)
            if boxes:
                b0 = boxes[0]
                _log(
                    f"[Debug] Turnstile 失败时部件 n={len(boxes)} "
                    f"kind={b0.get('kind')} "
                    f"size={float(b0.get('width') or 0):.0f}x{float(b0.get('height') or 0):.0f} "
                    f"token_len={n}"
                )
            else:
                _log(f"[Debug] Turnstile 失败且无 bbox token_len={n} err={exc}")
        except Exception:
            pass
        raise Exception(f"Turnstile 获取 token 失败: {exc}") from exc

    token = str(token or "").strip()
    if len(token) < 80:
        raise Exception("Turnstile 获取 token 失败")
    _log(f"Turnstile 已通过，token长度={len(token)}")
    # Small post-success dwell before caller submits the form
    _human_pause_cancel(0.35, 0.9, cancel_callback)
    return token


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def _cf_token_len(page) -> int:
    """Return current Turnstile response length (0 if absent/empty)."""
    try:
        token = page._p.evaluate(
            """() => {
                var el = document.querySelector('input[name="cf-turnstile-response"]');
                return (el && el.value) || '';
            }"""
        )
        return len(str(token or "").strip())
    except Exception:
        return 0


def _cf_present(page) -> bool:
    try:
        return bool(
            page._p.evaluate(
                """() => {
                    return !!(
                        document.querySelector('input[name="cf-turnstile-response"]')
                        || document.querySelector('iframe[src*="turnstile"], iframe[src*="challenges.cloudflare.com"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]')
                    );
                }"""
            )
        )
    except Exception:
        return False


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    browser, page = _ensure_thread_browser(log_callback=log_callback)
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    last_cf_retry_at = 0.0

    given_sels = [
        'input[data-testid="givenName"]',
        'input[name="givenName"]',
        'input[autocomplete="given-name"]',
        'input[aria-label*="名"]',
    ]
    family_sels = [
        'input[data-testid="familyName"]',
        'input[name="familyName"]',
        'input[autocomplete="family-name"]',
        'input[aria-label*="姓"]',
    ]
    password_sels = [
        'input[data-testid="password"]',
        'input[name="password"]',
        'input[type="password"]',
        'input[autocomplete="new-password"]',
    ]
    submit_labels = ["完成注册", "创建账户", "Sign up", "Create account", "Create Account"]

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)

        if not form_filled_once:
            _human_pause_cancel(0.25, 0.6, cancel_callback)
            ok_g = page.fill_first(given_sels, given_name, human=True)
            if not ok_g:
                sleep_with_cancel(0.5, cancel_callback)
                continue
            _human_pause_cancel(0.2, 0.55, cancel_callback)
            ok_f = page.fill_first(family_sels, family_name, human=True)
            if not ok_f:
                sleep_with_cancel(0.5, cancel_callback)
                continue
            _human_pause_cancel(0.2, 0.55, cancel_callback)
            ok_p = page.fill_first(password_sels, password, human=True)
            if not ok_p:
                if log_callback:
                    log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            form_filled_once = True
            if log_callback:
                log_callback(f"[*] 资料已填写: {given_name} {family_name}")
            try:
                from grok_register.reg_stats import update_attempt

                update_attempt(
                    profile={
                        "given_name": given_name,
                        "family_name": family_name,
                        "password": password,
                    }
                )
            except Exception:
                pass
            # Read form before engaging Turnstile — looks less scripted
            _human_pause_cancel(0.7, 1.6, cancel_callback)

        # Patient Turnstile solve (few human clicks; no token injection)
        if _cf_present(page):
            token_len = _cf_token_len(page)
            if token_len < 80:
                now = time.time()
                # Outer retry only after a full patient solve attempt (~55s)
                if now - last_cf_retry_at >= 8.0:
                    if log_callback:
                        log_callback(
                            f"[*] 等待 Cloudflare 人机验证... token长度={token_len}"
                        )
                        log_callback("[*] 耐心求解 Turnstile（少点击/长等待）...")
                    try:
                        getTurnstileToken(
                            log_callback=log_callback,
                            cancel_callback=cancel_callback,
                            timeout=55,
                        )
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 触发失败: {cf_exc}")
                    last_cf_retry_at = time.time()
                sleep_with_cancel(0.6, cancel_callback)
                continue

        # Brief pause after CF pass before submit
        _human_pause_cancel(0.55, 1.2, cancel_callback)
        submitted = None
        try:
            submitted = page.click_by_text(submit_labels, role="button")
        except Exception:
            submitted = None
        if not submitted:
            try:
                if page.click_first(
                    [
                        'button[type="submit"]',
                        'button:has-text("完成注册")',
                        'button:has-text("创建账户")',
                        'button:has-text("Sign up")',
                        'button:has-text("Create account")',
                    ]
                ):
                    submitted = "selector"
            except Exception:
                submitted = None

        if submitted:
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}

        if log_callback:
            log_callback("[Debug] 未找到提交按钮，继续等待页面稳定...")
        sleep_with_cancel(0.6, cancel_callback)

    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    browser, page = _ensure_thread_browser(log_callback=log_callback)
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    final_no_submit_timeout = 25

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            refresh_active_page()
            if page is None:
                sleep_with_cancel(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试原生点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = "not-final-page"
                try:
                    title_hit = page.run_js(
                        r"""
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
return titleHit;
                        """
                    )
                except Exception:
                    title_hit = False

                if title_hit:
                    if _cf_present(page) and _cf_token_len(page) < 80:
                        token_len = _cf_token_len(page)
                        retried = f"final-page-wait-cf:{token_len}"
                        if log_callback:
                            log_callback(
                                f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}"
                            )
                        # Avoid stacking rapid full solvers on the final page
                        if now - last_cf_retry_at >= 10:
                            if log_callback:
                                log_callback("[*] 最终页耐心求解 Turnstile...")
                            try:
                                getTurnstileToken(
                                    log_callback=log_callback,
                                    cancel_callback=cancel_callback,
                                    timeout=50,
                                )
                            except Exception as cf_exc:
                                if log_callback:
                                    log_callback(f"[Debug] 最终页 Turnstile 失败: {cf_exc}")
                            last_cf_retry_at = time.time()
                    else:
                        clicked = None
                        try:
                            clicked = page.click_by_text(
                                ["完成注册", "创建账户", "Sign up", "Create account"],
                                role="button",
                            )
                        except Exception:
                            clicked = None
                        if not clicked:
                            try:
                                if page.click_first(
                                    [
                                        'button[type="submit"]',
                                        'button:has-text("完成注册")',
                                        'button:has-text("创建账户")',
                                    ]
                                ):
                                    clicked = "selector"
                            except Exception:
                                clicked = None
                        if clicked:
                            retried = "final-page-clicked-submit"
                        else:
                            retried = "final-page-no-submit"

                last_submit_retry = now
                if log_callback and (
                    retried == "final-page-clicked-submit"
                    or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))
                ):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {final_no_submit_timeout}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except AccountRetryNeeded:
            raise
        except Exception:
            pass

        sleep_with_cancel(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


class CliStopController:
    def __init__(self):
        self.stop_requested = False

    def should_stop(self):
        return self.stop_requested

    def stop(self):
        self.stop_requested = True


def cli_log(message):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    safe_print(f"[{timestamp}] {message}", flush=True)


def run_registration_cli(count):
    controller = CliStopController()
    success_count = 0
    fail_count = 0
    retry_count_for_slot = 0
    max_slot_retry = 3
    out_dir = str(ensure_output_dir())
    accounts_output_file = os.path.join(
        out_dir,
        f"accounts_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
    )
    cli_log(f"[*] 终端模式启动，目标数量: {count}")
    cli_log(f"[*] 成功账号将实时保存到: {accounts_output_file}")
    try:
        start_browser(log_callback=cli_log)
        cli_log("[*] 浏览器已启动")
        i = 0
        while i < count:
            if controller.should_stop():
                break
            cli_log(f"--- 开始第 {i + 1}/{count} 个账号 ---")
            attempt_finished = False
            finish_attempt = None
            update_attempt = None
            abandon_attempt = None
            try:
                from grok_register.reg_stats import (
                    abandon_attempt as _abandon_attempt,
                    begin_attempt as _begin_attempt,
                    finish_attempt as _finish_attempt,
                    update_attempt as _update_attempt,
                )
                from grok_register.proxyutil import get_runtime_proxy, resolve_proxy

                finish_attempt = _finish_attempt
                update_attempt = _update_attempt
                abandon_attempt = _abandon_attempt
                _begin_attempt(
                    worker_id="cli",
                    idx=i + 1,
                    user_agent=str(config.get("user_agent", "") or ""),
                    proxy=get_runtime_proxy() or resolve_proxy() or config.get("proxy"),
                )
            except Exception:
                pass

            def _finish(outcome: str, **kwargs) -> None:
                nonlocal attempt_finished
                if attempt_finished or not finish_attempt:
                    return
                try:
                    finish_attempt(outcome, **kwargs)
                    attempt_finished = True
                except Exception:
                    pass

            try:
                email = ""
                dev_token = ""
                code = ""
                mail_ok = False
                max_mail_retry = get_max_mail_retry()
                for mail_try in range(1, max_mail_retry + 1):
                    cli_log(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
                    open_signup_page(
                        log_callback=cli_log, cancel_callback=controller.should_stop
                    )
                    cli_log("[*] 2. 创建邮箱并提交")
                    try:
                        email, dev_token = fill_email_and_submit(
                            log_callback=cli_log, cancel_callback=controller.should_stop
                        )
                    except EmailDomainRejected as domain_exc:
                        if mail_try < max_mail_retry:
                            cli_log(f"[!] 邮箱域名被拒绝，自动更换域名重试: {domain_exc}")
                            restart_browser(log_callback=cli_log)
                            sleep_with_cancel(1, controller.should_stop)
                            continue
                        raise
                    cli_log(f"[*] 邮箱: {email}")
                    if update_attempt:
                        try:
                            update_attempt(email=email)
                        except Exception:
                            pass
                    cli_log(f"[Debug] 邮箱credential(jwt): {dev_token}")
                    try:
                        ensure_output_dir()
                        ensure_output_dir()
                        with open(
                            str(OUTPUT_DIR / "mail_credentials.txt"),
                            "a",
                            encoding="utf-8",
                        ) as f:
                            f.write(f"{email}\t{dev_token}\n")
                    except Exception:
                        pass
                    cli_log("[*] 3. 拉取验证码")
                    try:
                        code = fill_code_and_submit(
                            email,
                            dev_token,
                            log_callback=cli_log,
                            cancel_callback=controller.should_stop,
                        )
                        mail_ok = True
                        break
                    except Exception as mail_exc:
                        msg = str(mail_exc)
                        if ("未收到验证码" in msg or "验证码" in msg or "域名被拒绝" in msg) and mail_try < max_mail_retry:
                            cli_log(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                            restart_browser(log_callback=cli_log)
                            sleep_with_cancel(1, controller.should_stop)
                            continue
                        raise

                if not mail_ok:
                    raise Exception(f"验证码阶段失败，已连续 {max_mail_retry} 次未收到验证码，跳过当前账号")
                cli_log(f"[*] 验证码: {code}")
                cli_log("[*] 4. 填写资料")
                profile = fill_profile_and_submit(
                    log_callback=cli_log, cancel_callback=controller.should_stop
                )
                cli_log(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
                cli_log("[*] 5. 等待 sso cookie")
                sso = wait_for_sso_cookie(
                    log_callback=cli_log, cancel_callback=controller.should_stop
                )
                if config.get("enable_nsfw", True):
                    cli_log("[*] 6. 开启 NSFW")
                    nsfw_ok, nsfw_msg = enable_nsfw_for_token(
                        sso, log_callback=cli_log
                    )
                    if nsfw_ok:
                        cli_log(f"[+] NSFW 开启成功: {nsfw_msg}")
                    else:
                        cli_log(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")
                pool = apply_post_register_pools(sso, email=email, log_callback=cli_log)
                if not pool.get("ok", True):
                    fail_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    _finish(
                        "bot_flag",
                        reason="bot_flag_source=1",
                        bot_flagged=True,
                        access_token=(pool.get("build_seed") or {}).get("access_token"),
                    )
                    cli_log(
                        f"[-] 注册失败: bot_flag_source=1 ({email})，未导入 Web/Build"
                        "（可设 allow_bot_flagged=true 强制继续）"
                    )
                    cli_log(f"[*] 当前统计: 成功 {success_count} | 失败 {fail_count}")
                else:
                    try:
                        line = f"{email}----{profile.get('password','')}----{sso}\n"
                        with open(accounts_output_file, "a", encoding="utf-8") as f:
                            f.write(line)
                    except Exception as file_exc:
                        cli_log(f"[Debug] 保存账号文件失败: {file_exc}")
                    success_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    _finish(
                        "success",
                        reason="bot_flag_allowed" if pool.get("bot_flagged") else "",
                        bot_flagged=bool(pool.get("bot_flagged")),
                        access_token=(pool.get("build_seed") or {}).get("access_token"),
                    )
                    if pool.get("bot_flagged"):
                        cli_log(f"[+] 注册成功(bot标记已允许): {email}")
                    else:
                        cli_log(f"[+] 注册成功: {email}")
                    cli_log(f"[*] 当前统计: 成功 {success_count} | 失败 {fail_count}")
                    if (
                        success_count > 0
                        and success_count % MEMORY_CLEANUP_INTERVAL == 0
                        and i < count
                    ):
                        cleanup_runtime_memory(
                            log_callback=cli_log,
                            reason=f"已成功 {success_count} 个账号，执行定期清理",
                        )
            except RegistrationCancelled:
                cli_log("[!] 注册被停止")
                _finish("cancelled", reason="user_stop")
                break
            except AccountRetryNeeded as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= max_slot_retry:
                    cli_log(
                        f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                    )
                    _finish(
                        "retry",
                        reason=str(exc)[:200],
                        error=str(exc)[:400],
                    )
                else:
                    fail_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    cli_log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
                    _finish(
                        "error",
                        reason="max_retry",
                        error=str(exc)[:400],
                    )
            except Exception as exc:
                fail_count += 1
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[-] 注册失败: {exc}")
                _finish(
                    "error",
                    reason=str(exc)[:200],
                    error=str(exc)[:400],
                )
            finally:
                if not attempt_finished and abandon_attempt:
                    try:
                        abandon_attempt()
                    except Exception:
                        pass
            if controller.should_stop() or i >= count:
                break
            if browser is None:
                start_browser(log_callback=cli_log)
            else:
                restart_browser(log_callback=cli_log)
            sleep_with_cancel(1, controller.should_stop)
    except KeyboardInterrupt:
        controller.stop()
        cli_log("[!] 收到 Ctrl+C，正在停止并清理")
    except Exception as exc:
        cli_log(f"[!] 任务异常: {exc}")
    finally:
        cleanup_runtime_memory(log_callback=cli_log, reason="任务结束")
        cli_log(f"[*] 任务结束。成功 {success_count} | 失败 {fail_count}")


def main_cli():
    load_config()
    count = int(config.get("register_count", 1) or 1)
    cli_log("[*] CLI 已加载配置")
    cli_log(f"[*] 当前邮箱服务商: {config.get('email_provider', 'duckmail')} | 注册数量: {count}")
    cli_log("[*] 输入 start 后开始；按 Ctrl+C 可强制停止")
    try:
        command = input("> ").strip().lower()
    except KeyboardInterrupt:
        cli_log("[!] 已取消")
        return
    if command != "start":
        cli_log("[!] 未输入 start，已退出")
        return
    run_registration_cli(count)


def main():
    """Interactive single-thread CLI (legacy). Prefer: python register_cli.py"""
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("-h", "--help", "help"):
        safe_print(
            "用法:\n"
            "  uv run python register_cli.py --count N --threads T\n"
            "  uv run python -m grok_register.cli --count N --threads T\n"
            "  uv run python -m grok_register.app   # 交互式旧 CLI（输入 start）\n"
        )
        return
    main_cli()


if __name__ == "__main__":
    main()

