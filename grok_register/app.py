#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
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

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

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

from grok_register.browser_adapter import Chromium, ChromiumOptions, PageDisconnectedError
from curl_cffi import requests as curl_requests
import requests as std_requests


from grok_register.paths import PROJECT_ROOT, CONFIG_FILE as _CONFIG_PATH, CRASH_LOG_FILE as _CRASH_PATH, OUTPUT_DIR, TURNSTILE_DIR, TOKEN_JSON, ensure_output_dir
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

UI_BG = "#242424"
UI_PANEL_BG = "#2b2b2b"
UI_FG = "#f2f2f2"
UI_MUTED_FG = "#b8b8b8"
UI_ENTRY_BG = "#333333"
UI_BUTTON_BG = "#3a3a3a"
UI_ACTIVE_BG = "#4a6078"

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "none",
    "cloudflare_path_domains": "/api/domains",
    "cloudflare_path_accounts": "/api/new_address",
    "cloudflare_path_token": "/api/token",
    "cloudflare_path_messages": "/api/mails",
    "proxy": "http://127.0.0.1:7890",
    "enable_nsfw": True,
    "register_count": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "grok2api_auto_add_local": False,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    # v3 only: upload CPA OAuth (Grok Build) after mint. Default off.
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
    "yyds_preferred_domains": "",
    "yyds_blocked_domains": "",
    "yyds_domain_selection": "random",
    "max_mail_retry": 3,
    "code_poll_timeout": 60,
    "code_poll_interval": 3,
    "register_threads": 1,
    "thread_start_interval": 0.8,
    "cpa_export_enabled": True,
    "register_browser_background": True,
    "register_browser_window_position": "-2400,100",
    "register_browser_window_size": "1000,800",
    "api_reverse_tools": "",
    "cpa_auth_dir": "./output/cpa_auths",
    "cpa_copy_to_hotload": False,
    "cpa_hotload_dir": "",
    "cpa_base_url": "https://cli-chat-proxy.grok.com/v1",
    "cpa_proxy": "",
    "proxy_pool_enabled": True,
    "proxy_pool_file": "all_proxies.txt",
    "proxy_pool_mode": "random",
    "proxy_pool_rotate_each_account": True,
    "cpa_mint_proxy_retries": 3,
    "cpa_headless": False,
    "cpa_force_standalone": True,
    "cpa_mint_timeout_sec": 300,
    "cpa_mint_required": False,
    "cpa_probe_after_write": True,
    "cpa_probe_chat": False,
    "cpa_mint_cookie_inject": True,
    "cpa_mint_browser_reuse": True,
    "cpa_mint_browser_recycle_every": 15,
    "cpa_mint_backend": "protocol",
    "yescaptcha_api_key": "",
    "cpa_protocol_debug": False,
    "sub2api_export_enabled": True,
    "sub2api_export_dir": "./output/sub2api_exports",
    "sub2api_combined_file": "./output/sub2api_exports/sub2api-accounts.json",
    "cpa_cloud_upload_enabled": False,
    "cpa_cloud_api_base": "",
    "cpa_cloud_management_key": "",
    "cpa_cloud_upload_timeout": 30,
    "cpa_cloud_upload_retries": 3,
    "cpa_cloud_upload_require_chat": True,
    "cpa_cloud_upload_chat_timeout": 45,
    "cpa_cloud_upload_chat_rounds": 3,
    "cpa_cloud_upload_chat_interval": 0.2,
    "cpa_cloud_upload_batch_every": 10,
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

EXTENSION_PATH = os.path.abspath(
    str(TURNSTILE_DIR)
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    """HTTP(S) proxies for requests/curl.

    Priority: thread-local pool pin > config.proxy
    """
    try:
        from grok_register.cpa_xai.proxyutil import get_runtime_proxy

        runtime = (get_runtime_proxy() or "").strip()
    except Exception:
        runtime = ""
    proxy = runtime or str(config.get("proxy", "") or "").strip()
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def assign_thread_proxy(log_callback=None, *, force_new: bool = False) -> str:
    """Assign a proxy for the current register/mint thread.

    When proxy_pool_enabled, takes next from pool (and pins thread-local).
    Otherwise uses config.proxy / existing runtime pin.
    """
    log = log_callback or (lambda m: None)
    try:
        from grok_register.cpa_xai.proxyutil import (
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
        from grok_register.cpa_xai.proxyutil import (
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
    if is_admin_create:
        payload = {"name": generate_username(10), "enablePrefix": True}
        if domain:
            payload["domain"] = domain
        headers = cloudflare_build_headers(content_type=True)
    else:
        payload = {}
        if domain:
            payload["domain"] = domain
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


def _convert_web_to_build(root: str | None = None, log_callback=None) -> dict | None:
    """Call Go backend API to convert imported web accounts to Build.

    POST /api/admin/v1/accounts/web/convert-to-build
    Payload: {"all": True, "strategy": "missing"} — only converts accounts
    that do not yet have a Build counterpart. Safe to call repeatedly.

    Uses the same admin credentials as v3 import (_grok2api_v3_credentials).
    Returns the parsed SSE complete payload, or None on failure.
    """
    _root, username, password = _grok2api_v3_credentials()
    if root:
        _root = str(root or "").strip().rstrip("/")
    if not _root:
        if log_callback:
            log_callback("[Debug] 未配置 grok2api_remote_base，跳过 Web→Build 转换")
        return None
    try:
        access = _grok2api_v3_login(_root, username, password, log_callback=log_callback)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Web→Build 转换登录失败: {exc}")
        return None
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    convert_url = f"{_root}/api/admin/v1/accounts/web/convert-to-build"
    payload = {"all": True, "strategy": "missing"}
    try:
        resp = http_post(
            convert_url,
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
            },
            json=payload,
            timeout=120,
            verify=False,
            proxies={},
        )
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] Web→Build 转换请求失败: {exc}")
        return None
    status = int(getattr(resp, "status_code", 0) or 0)
    body = ""
    try:
        body = resp.text or ""
    except Exception:
        pass
    if status >= 400:
        if log_callback:
            log_callback(f"[Debug] Web→Build 转换 HTTP {status}: {body[:200]}")
        return None
    result = _parse_go_import_sse(body)
    if result:
        created = int(result.get("created", 0))
        synced = int(result.get("synced", 0))
        if log_callback:
            log_callback(
                f"[+] Web→Build 转换完成: created={created} synced={synced}"
                + (f" updated={result.get('updated')}" if result.get("updated") else "")
            )
    else:
        if log_callback:
            log_callback(f"[*] Web→Build 转换成功，无 SSE 数据（可能无待转换账号）")
    return result


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


def add_token_to_grok2api_remote_pool_v3(raw_token, email="", log_callback=None):
    """Upload one SSO token to chenyme grok2api v3 Grok Web pool.

    API:
      POST {root}/api/admin/v1/auth/login
      POST {root}/api/admin/v1/accounts/web/import  (multipart files/file)
    """
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
    # Auto convert web→build if enabled (Go backend API)
    if config.get("grok2api_auto_add_build", False):
        try:
            _convert_web_to_build(root=root, log_callback=log_callback)
        except Exception as conv_exc:
            if log_callback:
                log_callback(f"[Debug] Web→Build 转换异常（不影响账号导入）: {conv_exc}")
    return True


def cpa_xai_auth_to_v3_build_entry(auth: dict) -> dict:
    """Convert local CPA xai-*.json object to chenyme v3 grok_build import entry."""
    if not isinstance(auth, dict):
        raise ValueError("auth must be a dict")
    access = str(auth.get("access_token") or "").strip()
    refresh = str(auth.get("refresh_token") or "").strip()
    if not access and not refresh:
        raise ValueError("access_token/refresh_token missing")
    email = str(auth.get("email") or "").strip()
    sub = str(auth.get("sub") or "").strip()
    name = email or (f"build-{sub[:8]}" if sub else "grok-build")
    expires_at = str(auth.get("expires_at") or auth.get("expired") or "").strip()
    entry = {
        "provider": "grok_build",
        "name": name,
        "client_id": str(auth.get("client_id") or "b1a00492-073a-47ea-816f-4c329264a828").strip(),
        "access_token": access,
        "refresh_token": refresh,
        "token_type": str(auth.get("token_type") or "Bearer").strip() or "Bearer",
        "email": email,
        "user_id": sub,
        "principal_id": sub,
    }
    if expires_at:
        entry["expires_at"] = expires_at
    if auth.get("expires_in") is not None:
        try:
            entry["expires_in"] = int(auth.get("expires_in"))
        except Exception:
            pass
    id_token = str(auth.get("id_token") or "").strip()
    if id_token:
        entry["id_token"] = id_token
    team_id = str(auth.get("team_id") or "").strip()
    if team_id:
        entry["team_id"] = team_id
    return entry


def load_cpa_xai_auth_file(path: str) -> dict:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("CPA auth JSON must be an object")
    return data


def add_cpa_auth_to_grok2api_v3_build(auth_or_path, log_callback=None):
    """Upload one CPA OAuth auth (Grok Build) to chenyme grok2api v3.

    API:
      POST {root}/api/admin/v1/accounts/import  (multipart OAuth JSON)
    Accepts a path to xai-*.json or an auth dict.
    """
    if isinstance(auth_or_path, (str, Path)):
        auth = load_cpa_xai_auth_file(str(auth_or_path))
        path_hint = str(auth_or_path)
    elif isinstance(auth_or_path, dict):
        auth = auth_or_path
        path_hint = str(auth.get("email") or "inline")
    else:
        raise ValueError("auth_or_path must be path or dict")

    mode = _grok2api_remote_mode()
    if mode == "legacy":
        if log_callback:
            log_callback("[Debug] grok2api legacy 模式不支持 Grok Build 导入，跳过")
        return False

    root, _user, _pw = _grok2api_v3_credentials()
    if not root:
        if log_callback:
            log_callback("[Debug] grok2api v3 Build 导入未配置 remote_base，跳过")
        return False

    entry = cpa_xai_auth_to_v3_build_entry(auth)
    document = {"accounts": [entry]}
    file_bytes = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
    safe_name = (entry.get("email") or entry.get("name") or "build").replace("@", "_").replace("/", "_")[:48]
    filename = f"build-{safe_name}.json"
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
                f"synced={synced} ({Path(path_hint).name if path_hint else filename})"
            )
    else:
        if log_callback:
            log_callback(f"[+] 已写入 grok2api v3 Build 池: {entry.get('email') or entry.get('name')}")
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


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    """Upload SSO token to remote grok2api (v3 preferred, legacy fallback)."""
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    mode = _grok2api_remote_mode()
    errors = []
    if mode in ("v3", "auto"):
        try:
            return add_token_to_grok2api_remote_pool_v3(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            errors.append(f"v3: {exc}")
            if mode == "v3":
                raise
            if log_callback:
                log_callback(f"[Debug] grok2api v3 导入失败，尝试 legacy: {exc}")
    if mode in ("legacy", "auto"):
        try:
            return add_token_to_grok2api_remote_pool_legacy(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            errors.append(f"legacy: {exc}")
            if mode == "legacy":
                raise
    raise RuntimeError("grok2api 远端写入失败: " + "; ".join(errors) if errors else "未知错误")


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None):
    if config.get("grok2api_auto_add_local", False):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_auto_add_remote", False):
        try:
            add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


CHROMIUM_SLIM_FLAGS = ()
# 自动化特征参数全部移除 — 不传递任何 --disable-* / --no-* 启动参数
# 让 Patchright + Chrome 使用最自然的浏览器环境


def create_browser_options(*, unique_profile: bool = False, profile_tag: str = "reg"):
    """Build minimal ChromiumOptions for register / CPA mint.

    No automation-identifying flags, no off-screen window placement.
    Patchright + system Chrome via channel="chrome" for best stealth.
    """
    options = ChromiumOptions()
    options.set_timeouts(base=2)
    # Outbound proxy (pool pin or config.proxy)
    proxy: str = ""
    try:
        from grok_register.cpa_xai.proxyutil import (
            get_runtime_proxy,
            proxy_for_chromium,
            proxy_auth_parts,
            write_chromium_proxy_auth_extension,
        )

        proxy = (get_runtime_proxy() or str(config.get("proxy", "") or "")).strip()
        if proxy:
            scheme, user, password, host, port = proxy_auth_parts(proxy)
            chrome_proxy = proxy_for_chromium(proxy)
            if user and password and host:
                # Authenticated proxy: MV2 extension (Chromium cannot embed user:pass)
                import tempfile
                import uuid as _uuid

                ext_dir = os.path.join(
                    tempfile.gettempdir(),
                    "grok_reg_proxy_ext",
                    _uuid.uuid4().hex[:12],
                )
                ext_path = write_chromium_proxy_auth_extension(proxy, ext_dir)
                if ext_path and os.path.isdir(ext_path):
                    try:
                        options.add_extension(ext_path)
                    except Exception:
                        # fallback: host:port only (may fail auth)
                        if chrome_proxy:
                            try:
                                options.set_argument(f"--proxy-server={chrome_proxy}")
                            except Exception:
                                pass
                elif chrome_proxy:
                    try:
                        options.set_argument(f"--proxy-server={chrome_proxy}")
                    except Exception:
                        pass
            elif chrome_proxy:
                try:
                    options.set_proxy(chrome_proxy)
                except Exception:
                    try:
                        options.set_argument(f"--proxy-server={chrome_proxy}")
                    except Exception:
                        pass
    except Exception:
        pass
    if unique_profile:
        import tempfile
        import uuid

        profile_dir = os.path.join(
            tempfile.gettempdir(),
            "grok_reg_chrome",
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
            options.set_argument(f"--user-data-dir={profile_dir}")
        except Exception:
            pass
    # auto_port MUST be last: set_user_data_path can clear address/port state.
    try:
        options.auto_port()
    except Exception:
        pass
    if os.path.exists(EXTENSION_PATH):
        try:
            options.add_extension(EXTENSION_PATH)
        except Exception:
            pass

    # 使用系统 Google Chrome（而非 Patchright 捆绑的 Chromium）以获得更好的浏览器指纹伪装
    # 参考 Patchright 官方推荐: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright
    try:
        options.set_channel("chrome")
    except Exception:
        _auto_detect_chrome_path(options)

    return options


def _auto_detect_chrome_path(options) -> None:
    """Fallback Chrome/Chromium auto-detection when set_channel() is unavailable."""
    for cand in (
        # Windows
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        # Linux
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if os.path.isfile(cand):
            try:
                options.set_browser_path(cand)
            except Exception:
                pass
            break


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
    """Save module globals browser/page into thread-local storage."""
    global browser, page
    _tls_set_browser(browser)
    _tls_set_page(page)


browser = None
page = None



def setup_light_theme(root):
    try:
        root.option_add("*Background", UI_BG)
        root.option_add("*Foreground", UI_FG)
        root.option_add("*selectBackground", UI_ACTIVE_BG)
        root.option_add("*selectForeground", UI_FG)
        root.option_add("*insertBackground", UI_FG)
        root.option_add("*Entry.Background", UI_ENTRY_BG)
        root.option_add("*Text.Background", UI_ENTRY_BG)
        root.option_add("*Menu.Background", UI_ENTRY_BG)
        root.option_add("*Menu.Foreground", UI_FG)
        style = ttk.Style(root)
        available = set(style.theme_names())
        if "clam" in available:
            style.theme_use("clam")
        elif "default" in available:
            style.theme_use("default")
        root.configure(bg=UI_BG)
        style.configure(".", background=UI_BG, foreground=UI_FG, fieldbackground=UI_ENTRY_BG)
        style.configure("TFrame", background=UI_BG)
        style.configure("TLabelframe", background=UI_BG, foreground=UI_FG)
        style.configure("TLabelframe.Label", background=UI_BG, foreground=UI_FG)
        style.configure("TLabel", background=UI_BG, foreground=UI_FG)
        style.configure("TCheckbutton", background=UI_BG, foreground=UI_FG)
        style.configure("TButton", background=UI_BUTTON_BG, foreground=UI_FG)
        style.configure("TEntry", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
        style.configure("TCombobox", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
        style.configure("TSpinbox", fieldbackground=UI_ENTRY_BG, foreground=UI_FG)
    except Exception:
        pass


def tk_label(parent, text="", **kwargs):
    return tk.Label(parent, text=text, bg=kwargs.pop("bg", UI_BG), fg=kwargs.pop("fg", UI_FG), **kwargs)


def tk_entry(parent, textvariable=None, width=30, **kwargs):
    return tk.Entry(
        parent,
        textvariable=textvariable,
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        insertbackground=UI_FG,
        disabledbackground="#2f2f2f",
        disabledforeground=UI_MUTED_FG,
        highlightthickness=1,
        highlightbackground="#555555",
        relief=tk.SOLID,
        **kwargs,
    )


def tk_button(parent, text="", command=None, state=tk.NORMAL, **kwargs):
    return tk.Button(
        parent,
        text=text,
        command=command,
        state=state,
        bg=UI_BUTTON_BG,
        fg=UI_FG,
        activebackground=UI_ACTIVE_BG,
        activeforeground=UI_FG,
        disabledforeground="#777777",
        relief=tk.RAISED,
        padx=10,
        pady=3,
        **kwargs,
    )


def tk_checkbutton(parent, text="", variable=None, **kwargs):
    return tk.Checkbutton(
        parent,
        text=text,
        variable=variable,
        bg=UI_BG,
        fg=UI_FG,
        activebackground=UI_BG,
        activeforeground=UI_FG,
        selectcolor="#3d7be0",
        **kwargs,
    )


def tk_option_menu(parent, variable, values, width=12):
    menu = tk.OptionMenu(parent, variable, *values)
    menu.configure(
        width=width,
        bg=UI_ENTRY_BG,
        fg=UI_FG,
        activebackground=UI_ACTIVE_BG,
        activeforeground=UI_FG,
        highlightthickness=1,
        highlightbackground="#555555",
        relief=tk.SOLID,
    )
    menu["menu"].configure(bg=UI_ENTRY_BG, fg=UI_FG, activebackground=UI_ACTIVE_BG, activeforeground=UI_FG)
    return menu


def start_browser(log_callback=None):
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
    # Ensure this thread has a proxy pin before building Chromium options.
    try:
        assign_thread_proxy(log_callback, force_new=False)
    except Exception:
        pass
    last_exc = None
    for attempt in range(1, 5):
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
            _auto_port = None
            if ':' in str(_auto_addr):
                try:
                    _auto_port = int(str(_auto_addr).rsplit(':', 1)[-1])
                except (ValueError, IndexError):
                    _auto_port = None
            browser = Chromium(opts)
            tabs = browser.get_tabs()
            page = tabs[-1] if tabs else browser.new_tab()
            _sync_thread_browser_globals()
            if log_callback and getattr(browser, "user_data_path", None):
                log_callback(f"[Debug] 当前浏览器资料目录: {browser.user_data_path}")
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser, page
        except Exception as exc:
            last_exc = exc
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            try:
                if browser is not None:
                    browser.quit(del_data=True)
                elif _auto_port is not None:
                    # Chromium() constructor spawned Chrome but failed to
                    # connect. Kill the orphan by its exact debug port so we
                    # never touch other threads' Chrome processes.
                    if _kill_chrome_by_port(_auto_port):
                        if log_callback:
                            log_callback(f"[Debug] 已清理端口 {_auto_port} 上的孤儿 Chrome")
            except Exception:
                pass
            browser = None
            page = None
            _sync_thread_browser_globals()
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    global browser, page
    _bind_thread_browser_globals()
    _user_data_path = None
    if browser is not None:
        try:
            _user_data_path = getattr(browser, "user_data_path", None)
        except Exception:
            pass
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
    global browser, page
    _bind_thread_browser_globals()
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
        _sync_thread_browser_globals()
        return True
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 会话清理失败，重启浏览器: {exc}")
        restart_browser(log_callback=log_callback)
        return False


def shutdown_browser():
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
        from grok_register.cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
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
    page = _tls_get_page()
    _tls_set_page(page)
    browser = _tls_get_browser()
    _tls_set_browser(browser)
    if page is None or browser is None:
        start_browser()
        page = _tls_get_page()
        _tls_set_page(page)
        browser = _tls_get_browser()
        _tls_set_browser(browser)
    if browser is None:
        restart_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
            _tls_set_page(page)
        else:
            page = browser.new_tab()
            _tls_set_page(page)
    except Exception:
        restart_browser()
    return page


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    page = _tls_get_page()
    _tls_set_page(page)
    browser = _tls_get_browser()
    _tls_set_browser(browser)
    if page is None or browser is None:
        start_browser()
        page = _tls_get_page()
        _tls_set_page(page)
        browser = _tls_get_browser()
        _tls_set_browser(browser)
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
const target = candidates[0]?.node || null;
if (!target) {
    return false;
}
target.click();
return candidates[0].text || true;
        """)

        if clicked:
            if log_callback:
                detail = f": {clicked}" if isinstance(clicked, str) else ""
                log_callback(f"[*] 已点击「使用邮箱注册」按钮{detail}")
            sleep_with_cancel(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        sleep_with_cancel(1, cancel_callback)

    if log_callback:
        page_html = page.html[:500] if page else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    page = _tls_get_page()
    _tls_set_page(page)
    browser = _tls_get_browser()
    _tls_set_browser(browser)
    if page is None or browser is None:
        start_browser()
        page = _tls_get_page()
        _tls_set_page(page)
        browser = _tls_get_browser()
        _tls_set_browser(browser)
    raise_if_cancelled(cancel_callback)
    if browser is None:
        start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = browser.get_tab(0)
        _tls_set_page(page)
        page.get(SIGNUP_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            page = browser.new_tab(SIGNUP_URL)
            _tls_set_page(page)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 创建新标签页异常: {e2}")
            restart_browser()
            page = browser.new_tab(SIGNUP_URL)
            _tls_set_page(page)
    page.wait.doc_loaded()
    sleep_with_cancel(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )


def has_profile_form(log_callback=None):
    refresh_active_page()
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
    page = _tls_get_page()
    _tls_set_page(page)
    browser = _tls_get_browser()
    _tls_set_browser(browser)
    if page is None or browser is None:
        start_browser()
        page = _tls_get_page()
        _tls_set_page(page)
        browser = _tls_get_browser()
        _tls_set_browser(browser)
    raise_if_cancelled(cancel_callback)
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    last_diag_time = 0
    last_reclick_time = 0
    last_snapshot = None
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function describeInput(node) {
    return [
        `type=${node.getAttribute('type') || ''}`,
        `name=${node.getAttribute('name') || ''}`,
        `id=${node.getAttribute('id') || ''}`,
        `placeholder=${node.getAttribute('placeholder') || ''}`,
        `aria=${node.getAttribute('aria-label') || ''}`,
        `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\s+/g, ' ').trim().slice(0, 160);
}
function describeAction(node) {
    return textOf(node).slice(0, 120);
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map(describeAction)
    .filter(Boolean)
    .slice(0, 10);
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) {
    return {
        state: 'not-ready',
        url: location.href,
        title: document.title,
        inputs: visibleInputs,
        buttons: visibleActions,
    };
}
input.focus(); input.click();
const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
const inputType = (input.getAttribute('type') || '').toLowerCase();
const isValid = inputType !== 'email' || input.checkValidity();
if ((input.value || '').trim() !== email || !isValid) {
    return {
        state: 'fill-failed',
        value: input.value || '',
        valid: isValid,
        input: describeInput(input),
        url: location.href,
    };
}
input.blur();
return {
    state: 'filled',
    input: describeInput(input),
    url: location.href,
};
            """,
            email,
        )
        state = filled.get("state") if isinstance(filled, dict) else filled
        if isinstance(filled, dict):
            last_snapshot = filled
        if state == "not-ready":
            now = time.time()
            if now - last_reclick_time >= 3:
                reclicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function nodeText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('href'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function scoreEntry(node) {
    const compact = nodeText(node).replace(/\s+/g, '');
    const lower = compact.toLowerCase();
    if (compact.includes('使用邮箱注册')) return 100;
    if (lower.includes('signupwithemail')) return 95;
    if (lower.includes('continuewithemail')) return 90;
    if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with'))) return 80;
    if (lower === 'email' || lower.includes('邮箱')) return 70;
    return 0;
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
    .filter((item) => item.score > 0)
    .sort((a, b) => b.score - a.score);
if (!candidates.length) return false;
candidates[0].node.click();
return candidates[0].text || true;
                """)
                last_reclick_time = now
                if reclicked and log_callback:
                    detail = f": {reclicked}" if isinstance(reclicked, str) else ""
                    log_callback(f"[Debug] 邮箱输入框未出现，已再次触发邮箱注册入口{detail}")
            if log_callback and now - last_diag_time >= 5:
                last_diag_time = now
                inputs = " | ".join((filled or {}).get("inputs", [])[:6]) if isinstance(filled, dict) else ""
                buttons = " | ".join((filled or {}).get("buttons", [])[:8]) if isinstance(filled, dict) else ""
                url = (filled or {}).get("url", page.url if page else "") if isinstance(filled, dict) else (page.url if page else "")
                log_callback(f"[Debug] 等待邮箱输入框: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if state != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue
        sleep_with_cancel(0.4, cancel_callback)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function textOf(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
        node.getAttribute('placeholder'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
function emailCandidates() {
    const direct = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
        const type = (node.getAttribute('type') || '').toLowerCase();
        if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
        const meta = textOf(node).toLowerCase();
        if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
            direct.push(node);
        }
    }
    return Array.from(new Set(direct));
}
const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !(input.value || '').trim()) return false;
const inputType = (input.getAttribute('type') || '').toLowerCase();
if (inputType === 'email' && !input.checkValidity()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        lower.includes('signup') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('createaccount') ||
        lower.includes('submit')
    );
});
if (submitButton) {
    submitButton.click();
    return textOf(submitButton) || true;
}
const form = input.closest('form');
if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
return 'enter';
            """
        )
        if clicked:
            sleep_with_cancel(1.0, cancel_callback)
            rejection = page.run_js(
                r"""
const text = String(document.body?.innerText || document.body?.textContent || '');
const compact = text.replace(/\s+/g, ' ');
const lower = compact.toLowerCase();
if (
  (compact.includes('已被拒绝') && (compact.includes('邮箱域名') || compact.includes('域名'))) ||
  (lower.includes('rejected') && (lower.includes('email') || lower.includes('domain'))) ||
  lower.includes('email domain') && lower.includes('not allowed')
) {
  return compact.slice(0, 500);
}
return '';
                """
            )
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
    if last_snapshot:
        inputs = " | ".join(last_snapshot.get("inputs", [])[:6])
        buttons = " | ".join(last_snapshot.get("buttons", [])[:8])
        url = last_snapshot.get("url", page.url if page else "")
        raise Exception(
            f"未找到邮箱输入框或注册按钮，最后页面: url={url}; inputs={inputs or 'none'}; buttons={buttons or 'none'}"
        )
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(
    email,
    dev_token,
    timeout=None,
    poll_interval=None,
    log_callback=None,
    cancel_callback=None,
):
    # thread-local page/browser (multi-thread safe)
    page = _tls_get_page()
    _tls_set_page(page)
    browser = _tls_get_browser()
    _tls_set_browser(browser)
    if page is None or browser is None:
        start_browser()
        page = _tls_get_page()
        _tls_set_page(page)
        browser = _tls_get_browser()
        _tls_set_browser(browser)
    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

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

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            sleep_with_cancel(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            sleep_with_cancel(0.5, cancel_callback)
            continue

        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """
        )

        if clicked == "clicked" or clicked == "no-button":
            if log_callback:
                log_callback(f"[*] 已填写验证码并提交: {code}")
            sleep_with_cancel(1.5, cancel_callback)
            return code

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("验证码已获取，但自动填写/提交失败")


def getTurnstileToken(log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    page = _tls_get_page()
    _tls_set_page(page)
    browser = _tls_get_browser()
    _tls_set_browser(browser)
    if page is None or browser is None:
        start_browser()
        page = _tls_get_page()
        _tls_set_page(page)
        browser = _tls_get_browser()
        _tls_set_browser(browser)
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    # Patchright native Turnstile: use frame_locator + locator instead of
    # shadow-DOM traversal.  Patchright already handles navigator.webdriver.
    pw_page = page._p  # underlying Playwright Page

    try:
        pw_page.evaluate(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    for _ in range(0, 30):
        raise_if_cancelled(cancel_callback)
        try:
            # 1. Check if token already present
            token = pw_page.evaluate(
                """() => {
                    var el = document.querySelector('input[name="cf-turnstile-response"]');
                    return (el && el.value) || '';
                }"""
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token
        except Exception:
            pass

        # 2. Try to click the Turnstile checkbox via Patchright native locator
        try:
            frame = pw_page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
            if frame:
                checkbox = frame.locator('#checkbox, input[type="checkbox"], .mark')
                if checkbox.count() > 0:
                    try:
                        checkbox.first.click(timeout=3000)
                    except Exception:
                        # Fallback: JS click inside the frame
                        try:
                            pw_page.evaluate("""
                                () => {
                                    var frames = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]');
                                    for (var f of frames) {
                                        try {
                                            var doc = f.contentDocument || f.contentWindow.document;
                                            var cb = doc.querySelector('#checkbox, input[type="checkbox"], .mark');
                                            if (cb) { cb.click(); return true; }
                                        } catch(e) {}
                                    }
                                    return false;
                                }
                            """)
                        except Exception:
                            pass
        except Exception:
            pass

        # 3. Fallback: JS click on any turnstile element
        try:
            pw_page.evaluate(
                """() => {
                    var nodes = document.querySelectorAll('div,span,iframe');
                    for (var i=0; i<nodes.length; i++) {
                        var txt = (nodes[i].className||'') + ' ' + (nodes[i].id||'') + ' ' + (nodes[i].getAttribute('src')||'');
                        if (txt.toLowerCase().includes('turnstile')) {
                            nodes[i].click(); break;
                        }
                    }
                }"""
            )
        except Exception:
            pass

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


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


def fill_profile_and_submit(timeout=120, log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    page = _tls_get_page()
    _tls_set_page(page)
    browser = _tls_get_browser()
    _tls_set_browser(browser)
    if page is None or browser is None:
        start_browser()
        page = _tls_get_page()
        _tls_set_page(page)
        browser = _tls_get_browser()
        _tls_set_browser(browser)
    given_name, family_name, password = build_profile()
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            filled = page.run_js(
                """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[aria-label*="名"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[aria-label*="姓"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed';

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (submitBtn) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
            """,
                given_name,
                family_name,
                password,
            )

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                    if log_callback:
                        log_callback(
                            f"[*] 资料已填写，立即处理 Cloudflare 人机验证... token长度={token_len}"
                        )
                elif log_callback and now - last_cf_retry_at >= 3:
                    log_callback(f"[*] 等待 Cloudflare 人机验证... token长度={token_len}")
                # 立刻主动点 Turnstile，不再空等 12s
                if now - last_cf_retry_at >= 1.2:
                    if log_callback:
                        log_callback("[*] 主动触发/复用 Turnstile...")
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 回填完成，长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 触发失败: {cf_exc}")
                    last_cf_retry_at = now
                sleep_with_cancel(0.4, cancel_callback)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
            elif filled == "fill-failed" and log_callback:
                log_callback("[Debug] 资料输入失败，重试中...")
                sleep_with_cancel(0.5, cancel_callback)
                continue
            elif filled == "not-ready":
                sleep_with_cancel(0.5, cancel_callback)
                continue

        submit_state = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'no-submit-button:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'submitted';
            """
        )

        if isinstance(submit_state, str) and submit_state.startswith("wait-cloudflare"):
            token_len = submit_state.split(":", 1)[1] if ":" in submit_state else "0"
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if log_callback and now - last_cf_retry_at >= 3:
                log_callback(f"[*] 提交前等待 Cloudflare... token长度={token_len}")
            if now - last_cf_retry_at >= 1.2:
                if log_callback:
                    log_callback("[*] 提交前主动触发 Turnstile...")
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[*] Turnstile 回填完成，长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 触发失败: {cf_exc}")
                last_cf_retry_at = now
            sleep_with_cancel(0.4, cancel_callback)
            continue

        if submit_state == "submitted":
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name}")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if isinstance(submit_state, str) and submit_state.startswith("no-submit-button") and log_callback:
            visible_buttons = submit_state.split(":", 1)[1] if ":" in submit_state else ""
            suffix = f" 可见按钮: {visible_buttons}" if visible_buttons else ""
            log_callback(f"[Debug] 未找到提交按钮，继续等待页面稳定...{suffix}")

        sleep_with_cancel(0.5, cancel_callback)

    raise Exception("最终注册页资料填写失败")


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    # thread-local page/browser (multi-thread safe)
    page = _tls_get_page()
    _tls_set_page(page)
    browser = _tls_get_browser()
    _tls_set_browser(browser)
    if page is None or browser is None:
        start_browser()
        page = _tls_get_page()
        _tls_set_page(page)
        browser = _tls_get_browser()
        _tls_set_browser(browser)
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

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and (retried == "final-page-clicked-submit" or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))):
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
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 2:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 回填完成，长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

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


class GrokRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Grok 注册机")
        self.root.geometry("1180x980")
        self.root.minsize(960, 700)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self.root.report_callback_exception = self.report_callback_exception
        self.setup_ui()

    def report_callback_exception(self, exc_type, exc_value, exc_tb):
        write_crash_log("TK_CALLBACK", exc_type, exc_value, exc_tb)
        try:
            self.log(f"[-] GUI异常: {exc_value}")
        except Exception:
            pass
        try:
            messagebox.showerror("GUI异常", f"{exc_value}\n详情见: {CRASH_LOG_FILE}")
        except Exception:
            pass

    def setup_ui(self):
        load_config()
        main_frame = tk.Frame(self.root, bg=UI_BG, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.grid_columnconfigure(0, weight=1)
        main_frame.grid_rowconfigure(4, weight=1)

        config_frame = tk.LabelFrame(
            main_frame,
            text="配置",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=10,
            pady=10,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        config_frame.grid(row=0, column=0, sticky=tk.EW, pady=(0, 8))
        config_frame.grid_columnconfigure(1, weight=1, minsize=260)
        config_frame.grid_columnconfigure(3, weight=1, minsize=260)

        def add_label(row, column, text):
            tk_label(config_frame, text=text, bg=UI_PANEL_BG).grid(
                row=row,
                column=column,
                sticky=tk.W,
                padx=(0, 6),
                pady=3,
            )

        def add_field(widget, row, column, columnspan=1, sticky=tk.EW):
            widget.grid(
                row=row,
                column=column,
                columnspan=columnspan,
                sticky=sticky,
                padx=(0, 14),
                pady=3,
            )

        add_label(0, 0, "邮箱服务商:")
        self.email_provider_var = tk.StringVar(value=config.get("email_provider", "duckmail"))
        self.email_provider_combo = tk_option_menu(config_frame, self.email_provider_var, ["duckmail", "yyds", "cloudflare"], width=12)
        add_field(self.email_provider_combo, 0, 1, sticky=tk.W)

        add_label(0, 2, "注册数量:")
        self.count_var = tk.StringVar(value=str(config.get("register_count", 1)))
        self.count_spinbox = tk.Spinbox(
            config_frame,
            from_=1,
            to=2500,
            width=8,
            textvariable=self.count_var,
            bg=UI_ENTRY_BG,
            fg=UI_FG,
            insertbackground=UI_FG,
            buttonbackground=UI_BUTTON_BG,
            disabledbackground="#2f2f2f",
            disabledforeground=UI_MUTED_FG,
            relief=tk.SOLID,
        )
        add_field(self.count_spinbox, 0, 3, sticky=tk.W)

        add_label(1, 0, "注册选项:")
        self.nsfw_var = tk.BooleanVar(value=config.get("enable_nsfw", True))
        self.nsfw_check = tk_checkbutton(config_frame, text="注册后开启 NSFW", variable=self.nsfw_var)
        add_field(self.nsfw_check, 1, 1, sticky=tk.W)

        add_label(1, 2, "代理（可选）:")
        self.proxy_var = tk.StringVar(value=config.get("proxy", ""))
        self.proxy_entry = tk_entry(config_frame, textvariable=self.proxy_var, width=34)
        add_field(self.proxy_entry, 1, 3)

        add_label(2, 0, "DuckMail API Key:")
        self.api_key_var = tk.StringVar(value=config.get("duckmail_api_key", ""))
        self.api_key_entry = tk_entry(config_frame, textvariable=self.api_key_var, width=34)
        add_field(self.api_key_entry, 2, 1)

        add_label(2, 2, "Cloudflare 鉴权模式:")
        self.cloudflare_auth_mode_var = tk.StringVar(value=config.get("cloudflare_auth_mode", "none"))
        self.cloudflare_auth_mode_combo = tk_option_menu(
            config_frame, self.cloudflare_auth_mode_var, ["query-key", "bearer", "x-api-key", "x-admin-auth", "none"], width=12
        )
        add_field(self.cloudflare_auth_mode_combo, 2, 3, sticky=tk.W)

        add_label(3, 0, "Cloudflare API Base:")
        self.cloudflare_api_base_var = tk.StringVar(value=config.get("cloudflare_api_base", ""))
        self.cloudflare_api_base_entry = tk_entry(config_frame, textvariable=self.cloudflare_api_base_var, width=72)
        add_field(self.cloudflare_api_base_entry, 3, 1, columnspan=3)

        add_label(4, 0, "Cloudflare API Key:")
        self.cloudflare_api_key_var = tk.StringVar(value=config.get("cloudflare_api_key", ""))
        self.cloudflare_api_key_entry = tk_entry(config_frame, textvariable=self.cloudflare_api_key_var, width=34)
        add_field(self.cloudflare_api_key_entry, 4, 1)

        add_label(4, 2, "CF 路径:")
        self.cloudflare_paths_var = tk.StringVar(
            value=",".join(
                [
                    config.get("cloudflare_path_domains", "/api/domains"),
                    config.get("cloudflare_path_accounts", "/api/new_address"),
                    config.get("cloudflare_path_token", "/api/token"),
                    config.get("cloudflare_path_messages", "/api/mails"),
                ]
            )
        )
        self.cloudflare_paths_entry = tk_entry(config_frame, textvariable=self.cloudflare_paths_var, width=34)
        add_field(self.cloudflare_paths_entry, 4, 3)

        add_label(5, 0, "grok2api 本地入池:")
        self.grok2api_local_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_local", False)))
        self.grok2api_local_auto_check = tk_checkbutton(config_frame, variable=self.grok2api_local_auto_var)
        add_field(self.grok2api_local_auto_check, 5, 1, sticky=tk.W)

        add_label(5, 2, "grok2api 池名:")
        self.grok2api_pool_name_var = tk.StringVar(value=str(config.get("grok2api_pool_name", "ssoBasic")))
        self.grok2api_pool_name_combo = tk_option_menu(
            config_frame, self.grok2api_pool_name_var, ["ssoBasic", "ssoSuper", "auto"], width=12
        )
        add_field(self.grok2api_pool_name_combo, 5, 3, sticky=tk.W)

        add_label(6, 0, "本地 token.json:")
        self.grok2api_local_file_var = tk.StringVar(value=str(config.get("grok2api_local_token_file", "")))
        self.grok2api_local_file_entry = tk_entry(config_frame, textvariable=self.grok2api_local_file_var, width=72)
        add_field(self.grok2api_local_file_entry, 6, 1, columnspan=3)

        add_label(7, 0, "grok2api 远端Web:")
        self.grok2api_remote_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_remote", False)))
        self.grok2api_remote_auto_check = tk_checkbutton(config_frame, variable=self.grok2api_remote_auto_var)
        add_field(self.grok2api_remote_auto_check, 7, 1, sticky=tk.W)
        add_label(7, 2, "远端Build:")
        self.grok2api_build_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_build", False)))
        self.grok2api_build_auto_check = tk_checkbutton(config_frame, variable=self.grok2api_build_auto_var)
        add_field(self.grok2api_build_auto_check, 7, 3, sticky=tk.W)

        add_label(8, 0, "grok2api 远端 Base:")
        self.grok2api_remote_base_var = tk.StringVar(value=str(config.get("grok2api_remote_base", "")))
        self.grok2api_remote_base_entry = tk_entry(config_frame, textvariable=self.grok2api_remote_base_var, width=72)
        add_field(self.grok2api_remote_base_entry, 8, 1, columnspan=3)

        add_label(9, 0, "远端 mode/user:")
        self.grok2api_remote_mode_var = tk.StringVar(value=str(config.get("grok2api_remote_mode", "auto") or "auto"))
        self.grok2api_remote_mode_combo = tk_option_menu(
            config_frame, self.grok2api_remote_mode_var, ["auto", "v3", "legacy"], width=10
        )
        add_field(self.grok2api_remote_mode_combo, 9, 1, sticky=tk.W)
        self.grok2api_remote_user_var = tk.StringVar(value=str(config.get("grok2api_remote_username", "admin") or "admin"))
        self.grok2api_remote_user_entry = tk_entry(config_frame, textvariable=self.grok2api_remote_user_var, width=28)
        add_field(self.grok2api_remote_user_entry, 9, 2, columnspan=2)

        add_label(10, 0, "v3密码/legacy key:")
        self.grok2api_remote_key_var = tk.StringVar(
            value=str(config.get("grok2api_remote_password", "") or config.get("grok2api_remote_app_key", "") or "")
        )
        self.grok2api_remote_key_entry = tk_entry(config_frame, textvariable=self.grok2api_remote_key_var, width=72)
        add_field(self.grok2api_remote_key_entry, 10, 1, columnspan=3)

        cpa_frame = tk.LabelFrame(
            main_frame,
            text="CPA / 高级",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=10,
            pady=8,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        cpa_frame.grid(row=1, column=0, sticky=tk.EW, pady=(0, 8))
        cpa_frame.grid_columnconfigure(1, weight=1, minsize=220)
        cpa_frame.grid_columnconfigure(3, weight=1, minsize=220)

        def cpa_label(row, column, text):
            tk_label(cpa_frame, text=text, bg=UI_PANEL_BG).grid(
                row=row, column=column, sticky=tk.W, padx=(0, 6), pady=2
            )

        def cpa_field(widget, row, column, columnspan=1, sticky=tk.EW):
            widget.grid(
                row=row,
                column=column,
                columnspan=columnspan,
                sticky=sticky,
                padx=(0, 12),
                pady=2,
            )

        cpa_label(0, 0, "注册线程:")
        self.register_threads_var = tk.StringVar(value=str(config.get("register_threads", 1)))
        self.register_threads_spin = tk.Spinbox(
            cpa_frame,
            from_=1,
            to=10,
            width=6,
            textvariable=self.register_threads_var,
            bg=UI_ENTRY_BG,
            fg=UI_FG,
            insertbackground=UI_FG,
            buttonbackground=UI_BUTTON_BG,
            relief=tk.SOLID,
        )
        cpa_field(self.register_threads_spin, 0, 1, sticky=tk.W)

        cpa_label(0, 2, "浏览器后台:")
        self.register_browser_bg_var = tk.BooleanVar(
            value=bool(config.get("register_browser_background", True))
        )
        self.register_browser_bg_check = tk_checkbutton(
            cpa_frame,
            text="不抢前台(屏外)",
            variable=self.register_browser_bg_var,
        )
        cpa_field(self.register_browser_bg_check, 0, 3, sticky=tk.W)

        cpa_label(1, 0, "CPA 导出:")
        self.cpa_export_var = tk.BooleanVar(value=bool(config.get("cpa_export_enabled", True)))
        self.cpa_export_check = tk_checkbutton(
            cpa_frame, text="注册后 mint xai-*.json", variable=self.cpa_export_var
        )
        cpa_field(self.cpa_export_check, 1, 1, sticky=tk.W)

        cpa_label(1, 2, "mint 后端:")
        self.cpa_mint_backend_var = tk.StringVar(
            value=str(config.get("cpa_mint_backend", "protocol") or "protocol")
        )
        self.cpa_mint_backend_combo = tk_option_menu(
            cpa_frame,
            self.cpa_mint_backend_var,
            ["protocol", "browser", "auto"],
            width=12,
        )
        cpa_field(self.cpa_mint_backend_combo, 1, 3, sticky=tk.W)

        cpa_label(2, 0, "云上传:")
        self.cpa_cloud_upload_var = tk.BooleanVar(
            value=bool(config.get("cpa_cloud_upload_enabled", False))
        )
        self.cpa_cloud_upload_check = tk_checkbutton(
            cpa_frame, text="上传到 CPAMP", variable=self.cpa_cloud_upload_var
        )
        cpa_field(self.cpa_cloud_upload_check, 2, 1, sticky=tk.W)

        cpa_label(2, 2, "chat 门禁:")
        self.cpa_chat_gate_var = tk.BooleanVar(
            value=bool(config.get("cpa_cloud_upload_require_chat", True))
        )
        self.cpa_chat_gate_check = tk_checkbutton(
            cpa_frame, text="上传前探测", variable=self.cpa_chat_gate_var
        )
        cpa_field(self.cpa_chat_gate_check, 2, 3, sticky=tk.W)

        cpa_label(3, 0, "门禁轮数:")
        self.cpa_chat_rounds_var = tk.StringVar(
            value=str(config.get("cpa_cloud_upload_chat_rounds", 3))
        )
        self.cpa_chat_rounds_spin = tk.Spinbox(
            cpa_frame,
            from_=1,
            to=10,
            width=6,
            textvariable=self.cpa_chat_rounds_var,
            bg=UI_ENTRY_BG,
            fg=UI_FG,
            insertbackground=UI_FG,
            buttonbackground=UI_BUTTON_BG,
            relief=tk.SOLID,
        )
        cpa_field(self.cpa_chat_rounds_spin, 3, 1, sticky=tk.W)

        cpa_label(3, 2, "CPA auth 目录:")
        self.cpa_auth_dir_var = tk.StringVar(value=str(config.get("cpa_auth_dir", "./output/cpa_auths")))
        self.cpa_auth_dir_entry = tk_entry(cpa_frame, textvariable=self.cpa_auth_dir_var, width=34)
        cpa_field(self.cpa_auth_dir_entry, 3, 3)

        cpa_label(4, 0, "CPAMP API Base:")
        self.cpa_cloud_api_base_var = tk.StringVar(value=str(config.get("cpa_cloud_api_base", "")))
        self.cpa_cloud_api_base_entry = tk_entry(
            cpa_frame, textvariable=self.cpa_cloud_api_base_var, width=72
        )
        cpa_field(self.cpa_cloud_api_base_entry, 4, 1, columnspan=3)

        cpa_label(5, 0, "CPAMP 管理密钥:")
        self.cpa_cloud_key_var = tk.StringVar(value=str(config.get("cpa_cloud_management_key", "")))
        self.cpa_cloud_key_entry = tk_entry(
            cpa_frame, textvariable=self.cpa_cloud_key_var, width=72, show="*"
        )
        cpa_field(self.cpa_cloud_key_entry, 5, 1, columnspan=3)

        cpa_label(6, 0, "YesCaptcha Key:")
        self.yescaptcha_key_var = tk.StringVar(value=str(config.get("yescaptcha_api_key", "")))
        self.yescaptcha_key_entry = tk_entry(
            cpa_frame, textvariable=self.yescaptcha_key_var, width=34, show="*"
        )
        cpa_field(self.yescaptcha_key_entry, 6, 1)

        cpa_label(6, 2, "protocol 调试:")
        self.cpa_protocol_debug_var = tk.BooleanVar(
            value=bool(config.get("cpa_protocol_debug", False))
        )
        self.cpa_protocol_debug_check = tk_checkbutton(
            cpa_frame, text="详细日志", variable=self.cpa_protocol_debug_var
        )
        cpa_field(self.cpa_protocol_debug_check, 6, 3, sticky=tk.W)

        cpa_label(7, 0, "代理池:")
        self.proxy_pool_enabled_var = tk.BooleanVar(
            value=bool(config.get("proxy_pool_enabled", False))
        )
        self.proxy_pool_enabled_check = tk_checkbutton(
            cpa_frame, text="启用 all_proxies", variable=self.proxy_pool_enabled_var
        )
        cpa_field(self.proxy_pool_enabled_check, 7, 1, sticky=tk.W)

        cpa_label(7, 2, "每号换代理:")
        self.proxy_pool_rotate_var = tk.BooleanVar(
            value=bool(config.get("proxy_pool_rotate_each_account", True))
        )
        self.proxy_pool_rotate_check = tk_checkbutton(
            cpa_frame, text="rotate each account", variable=self.proxy_pool_rotate_var
        )
        cpa_field(self.proxy_pool_rotate_check, 7, 3, sticky=tk.W)

        cpa_label(8, 0, "代理池文件:")
        self.proxy_pool_file_var = tk.StringVar(
            value=str(config.get("proxy_pool_file", "all_proxies.txt"))
        )
        self.proxy_pool_file_entry = tk_entry(
            cpa_frame, textvariable=self.proxy_pool_file_var, width=34
        )
        cpa_field(self.proxy_pool_file_entry, 8, 1)

        cpa_label(8, 2, "代理池模式:")
        self.proxy_pool_mode_var = tk.StringVar(
            value=str(config.get("proxy_pool_mode", "random") or "random")
        )
        self.proxy_pool_mode_combo = tk_option_menu(
            cpa_frame, self.proxy_pool_mode_var, ["random", "round_robin"], width=12
        )
        cpa_field(self.proxy_pool_mode_combo, 8, 3, sticky=tk.W)

        btn_frame = tk.Frame(main_frame, bg=UI_BG)
        btn_frame.grid(row=2, column=0, sticky=tk.EW, pady=(0, 6))
        self.start_btn = tk_button(btn_frame, text="开始注册", command=self.start_registration)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = tk_button(btn_frame, text="停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.clear_btn = tk_button(btn_frame, text="清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)

        status_frame = tk.Frame(main_frame, bg=UI_BG)
        status_frame.grid(row=3, column=0, sticky=tk.EW, pady=(0, 6))
        self.status_var = tk.StringVar(value="就绪")
        tk_label(status_frame, text="状态: ").pack(side=tk.LEFT)
        self.status_label = tk.Label(status_frame, textvariable=self.status_var, bg=UI_BG, fg="green")
        self.status_label.pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功: 0 | 失败: 0")
        tk.Label(status_frame, textvariable=self.stats_var, bg=UI_BG, fg=UI_FG).pack(side=tk.RIGHT)
        log_frame = tk.LabelFrame(
            main_frame,
            text="日志",
            bg=UI_PANEL_BG,
            fg=UI_FG,
            padx=5,
            pady=5,
            relief=tk.GROOVE,
            borderwidth=1,
        )
        log_frame.grid(row=4, column=0, sticky=tk.NSEW)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(0, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=18,
            width=60,
            bg="#111111",
            fg="#f5f5f5",
            insertbackground="#f5f5f5",
            selectbackground="#345a8a",
            selectforeground="#ffffff",
            relief=tk.SOLID,
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#555555",
        )
        self.log_text.grid(row=0, column=0, sticky=tk.NSEW)
        self.log("[*] GUI 已就绪，配置已加载")
        self.log(
            f"[*] 当前邮箱服务商: {self.email_provider_var.get()} | 注册数量: {self.count_var.get()} | "
            f"mint={self.cpa_mint_backend_var.get()} | 后台浏览器={bool(self.register_browser_bg_var.get())} | "
            f"云上传={bool(self.cpa_cloud_upload_var.get())} | chat门禁轮数={self.cpa_chat_rounds_var.get()} | "
            f"代理池={bool(self.proxy_pool_enabled_var.get())}"
        )

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        safe_print(line, flush=True)
        self.log_text.insert(tk.END, f"{line}\n")
        self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def update_stats(self):
        self.stats_var.set(f"成功: {self.success_count} | 失败: {self.fail_count}")

    def _set_running_ui(self, running):
        self.is_running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中..." if running else "就绪")
        self.status_label.config(foreground="blue" if running else "green")

    def should_stop(self):
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        config["email_provider"] = self.email_provider_var.get().strip() or "duckmail"
        config["enable_nsfw"] = bool(self.nsfw_var.get())
        config["proxy"] = self.proxy_var.get().strip()
        config["duckmail_api_key"] = self.api_key_var.get().strip()
        config["cloudflare_api_base"] = self.cloudflare_api_base_var.get().strip()
        config["cloudflare_api_key"] = self.cloudflare_api_key_var.get().strip()
        config["cloudflare_auth_mode"] = self.cloudflare_auth_mode_var.get().strip() or "none"
        config["grok2api_auto_add_local"] = bool(self.grok2api_local_auto_var.get())
        config["grok2api_local_token_file"] = self.grok2api_local_file_var.get().strip()
        config["grok2api_pool_name"] = self.grok2api_pool_name_var.get().strip() or "ssoBasic"
        config["grok2api_auto_add_remote"] = bool(self.grok2api_remote_auto_var.get())
        config["grok2api_auto_add_build"] = bool(self.grok2api_build_auto_var.get())
        config["grok2api_remote_base"] = self.grok2api_remote_base_var.get().strip()
        config["grok2api_remote_mode"] = (self.grok2api_remote_mode_var.get() or "auto").strip() or "auto"
        config["grok2api_remote_username"] = (self.grok2api_remote_user_var.get() or "admin").strip() or "admin"
        secret = self.grok2api_remote_key_var.get().strip()
        # v3 uses password; legacy uses app_key. Store both for dual-mode.
        config["grok2api_remote_password"] = secret
        config["grok2api_remote_app_key"] = secret
        raw_paths = [x.strip() for x in self.cloudflare_paths_var.get().split(",") if x.strip()]
        if len(raw_paths) >= 4:
            config["cloudflare_path_domains"] = raw_paths[0] if raw_paths[0].startswith("/") else ("/" + raw_paths[0])
            config["cloudflare_path_accounts"] = raw_paths[1] if raw_paths[1].startswith("/") else ("/" + raw_paths[1])
            config["cloudflare_path_token"] = raw_paths[2] if raw_paths[2].startswith("/") else ("/" + raw_paths[2])
            config["cloudflare_path_messages"] = raw_paths[3] if raw_paths[3].startswith("/") else ("/" + raw_paths[3])
        try:
            config["register_threads"] = max(1, min(10, int(self.register_threads_var.get())))
        except Exception:
            config["register_threads"] = 1
            self.register_threads_var.set("1")
        config["register_browser_background"] = bool(self.register_browser_bg_var.get())
        config["cpa_export_enabled"] = bool(self.cpa_export_var.get())
        backend = (self.cpa_mint_backend_var.get() or "protocol").strip().lower()
        if backend not in ("protocol", "browser", "auto"):
            backend = "protocol"
        config["cpa_mint_backend"] = backend
        config["cpa_cloud_upload_enabled"] = bool(self.cpa_cloud_upload_var.get())
        config["cpa_cloud_upload_require_chat"] = bool(self.cpa_chat_gate_var.get())
        try:
            config["cpa_cloud_upload_chat_rounds"] = max(1, min(10, int(self.cpa_chat_rounds_var.get())))
        except Exception:
            config["cpa_cloud_upload_chat_rounds"] = 3
            self.cpa_chat_rounds_var.set("3")
        config["cpa_auth_dir"] = self.cpa_auth_dir_var.get().strip() or "./output/cpa_auths"
        config["cpa_cloud_api_base"] = self.cpa_cloud_api_base_var.get().strip()
        config["cpa_cloud_management_key"] = self.cpa_cloud_key_var.get().strip()
        config["yescaptcha_api_key"] = self.yescaptcha_key_var.get().strip()
        config["cpa_protocol_debug"] = bool(self.cpa_protocol_debug_var.get())
        config["proxy_pool_enabled"] = bool(self.proxy_pool_enabled_var.get())
        config["proxy_pool_rotate_each_account"] = bool(self.proxy_pool_rotate_var.get())
        config["proxy_pool_file"] = self.proxy_pool_file_var.get().strip() or "all_proxies.txt"
        mode = (self.proxy_pool_mode_var.get() or "round_robin").strip().lower()
        if mode not in ("round_robin", "random"):
            mode = "random"
        config["proxy_pool_mode"] = mode
        save_config()
        if config["email_provider"] == "cloudflare" and not config["cloudflare_api_base"]:
            self.log("[!] Cloudflare 模式需要先填写 Cloudflare API Base")
            return
        try:
            count = int(self.count_var.get())
        except Exception:
            self.log("[!] 注册数量无效")
            return
        config["register_count"] = count
        save_config()
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = str(ensure_output_dir())
        self.accounts_output_file = os.path.join(out_dir, f"accounts_{now}.txt")
        self.update_stats()
        self._set_running_ui(True)
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}")
        self.log(
            f"[*] CPA: export={config.get('cpa_export_enabled')} mint={config.get('cpa_mint_backend')} "
            f"cloud={config.get('cpa_cloud_upload_enabled')} chat_gate={config.get('cpa_cloud_upload_require_chat')} "
            f"rounds={config.get('cpa_cloud_upload_chat_rounds')} bg_browser={config.get('register_browser_background')}"
        )
        if int(config.get("register_threads") or 1) > 1:
            self.log(
                f"[*] 提示: register_threads={config.get('register_threads')} 已写入配置；"
                f"GUI 当前仍顺序执行。高并发请用 CLI: register_cli.py --threads N"
            )
        self.log(f"[*] 成功账号将实时保存到: {self.accounts_output_file}")
        threading.Thread(
            target=self.run_registration,
            args=(count,),
            daemon=True,
        ).start()

    def stop_registration(self):
        self.stop_requested = True
        self.log("[!] 用户停止注册")
        try:
            stop_browser()
            self.log("[*] 已关闭注册浏览器")
        except Exception as exc:
            self.log(f"[Debug] 关闭注册浏览器失败: {exc}")
        try:
            from grok_register.cpa_xai.browser_confirm import shutdown_mint_browsers

            shutdown_mint_browsers()
            self.log("[*] 已关闭 CPA mint 浏览器")
        except Exception as exc:
            self.log(f"[Debug] 关闭 CPA mint 浏览器失败: {exc}")

    def run_registration(self, count):
        try:
            start_browser(log_callback=self.log)
            self.log("[*] 浏览器已启动")
            i = 0
            retry_count_for_slot = 0
            max_slot_retry = 3
            while i < count:
                if self.should_stop():
                    break
                self.log(f"--- 开始第 {i + 1}/{count} 个账号 ---")
                try:
                    email = ""
                    dev_token = ""
                    code = ""
                    mail_ok = False
                    max_mail_retry = get_max_mail_retry()
                    for mail_try in range(1, max_mail_retry + 1):
                        self.log(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
                        open_signup_page(
                            log_callback=self.log, cancel_callback=self.should_stop
                        )
                        self.log("[*] 2. 创建邮箱并提交")
                        try:
                            email, dev_token = fill_email_and_submit(
                                log_callback=self.log, cancel_callback=self.should_stop
                            )
                        except EmailDomainRejected as domain_exc:
                            if mail_try < max_mail_retry:
                                self.log(f"[!] 邮箱域名被拒绝，自动更换域名重试: {domain_exc}")
                                restart_browser(log_callback=self.log)
                                sleep_with_cancel(1, self.should_stop)
                                continue
                            raise
                        self.log(f"[*] 邮箱: {email}")
                        self.log(f"[Debug] 邮箱credential(jwt): {dev_token}")
                        try:
                            ensure_output_dir()
                            with open(
                                str(OUTPUT_DIR / "mail_credentials.txt"),
                                "a",
                                encoding="utf-8",
                            ) as f:
                                f.write(f"{email}\t{dev_token}\n")
                        except Exception:
                            pass
                        self.log("[*] 3. 拉取验证码")
                        try:
                            code = fill_code_and_submit(
                                email,
                                dev_token,
                                log_callback=self.log,
                                cancel_callback=self.should_stop,
                            )
                            mail_ok = True
                            break
                        except Exception as mail_exc:
                            msg = str(mail_exc)
                            if ("未收到验证码" in msg or "验证码" in msg or "域名被拒绝" in msg) and mail_try < max_mail_retry:
                                self.log(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                                restart_browser(log_callback=self.log)
                                sleep_with_cancel(1, self.should_stop)
                                continue
                            raise

                    if not mail_ok:
                        raise Exception(f"验证码阶段失败，已连续 {max_mail_retry} 次未收到验证码，跳过当前账号")
                    self.log(f"[*] 验证码: {code}")
                    self.log("[*] 4. 填写资料")
                    profile = fill_profile_and_submit(
                        log_callback=self.log, cancel_callback=self.should_stop
                    )
                    self.log(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
                    self.log("[*] 5. 等待 sso cookie")
                    sso = wait_for_sso_cookie(
                        log_callback=self.log, cancel_callback=self.should_stop
                    )
                    if config.get("enable_nsfw", True):
                        self.log("[*] 6. 开启 NSFW")
                        nsfw_ok, nsfw_msg = enable_nsfw_for_token(
                            sso, log_callback=self.log
                        )
                        if nsfw_ok:
                            self.log(f"[+] NSFW 开启成功: {nsfw_msg}")
                        else:
                            self.log(f"[!] NSFW 未开启，继续保存账号: {nsfw_msg}")
                    self.results.append({"email": email, "sso": sso, "profile": profile})
                    try:
                        line = f"{email}----{profile.get('password','')}----{sso}\n"
                        with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                            f.write(line)
                    except Exception as file_exc:
                        self.log(f"[Debug] 保存账号文件失败: {file_exc}")
                    add_token_to_grok2api_pools(sso, email=email, log_callback=self.log)
                    run_cpa_and_sub2api_export(
                        email,
                        profile.get("password", "") or "",
                        sso,
                        log_callback=self.log,
                    )
                    self.success_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    self.log(f"[+] 注册成功: {email}")
                    if (
                        self.success_count > 0
                        and self.success_count % MEMORY_CLEANUP_INTERVAL == 0
                        and i < count
                    ):
                        cleanup_runtime_memory(
                            log_callback=self.log,
                            reason=f"已成功 {self.success_count} 个账号，执行定期清理",
                        )
                except RegistrationCancelled:
                    self.log("[!] 注册被用户停止")
                    break
                except AccountRetryNeeded as exc:
                    retry_count_for_slot += 1
                    if retry_count_for_slot <= max_slot_retry:
                        self.log(
                            f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                        )
                    else:
                        self.fail_count += 1
                        self.log(
                            f"[-] 当前账号已达到最大重试次数，跳过: {exc}"
                        )
                        retry_count_for_slot = 0
                        i += 1
                except Exception as exc:
                    self.fail_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    self.log(f"[-] 注册失败: {exc}")
                finally:
                    self.update_stats()
                    if self.should_stop() or i >= count:
                        break
                    if browser is None:
                        start_browser(log_callback=self.log)
                    else:
                        restart_browser(log_callback=self.log)
                    sleep_with_cancel(1, self.should_stop)
        except Exception as exc:
            self.log(f"[!] 任务异常: {exc}")
        finally:
            stop_browser()
            self._set_running_ui(False)
            try:
                flush_queued_cpa_cloud_uploads(config, log_callback=self.log)
            except Exception as upload_exc:
                self.log(f"[cloud-cpa] batch upload exception: {upload_exc}")
            self.log("[*] 任务结束")




def normalize_cpa_cloud_api_base(raw_base):
    base = (raw_base or "").strip().rstrip("/")
    if not base:
        return ""
    if not re.match(r"^https?://", base, re.I):
        base = "http://" + base
    base = re.sub(r"/v0/management/?$", "", base, flags=re.I).rstrip("/")
    return base + "/v0/management"


def get_cpa_cloud_management_key(cfg):
    # Env wins so config.json can omit the secret if desired.
    return (
        os.environ.get("CPA_CLOUD_MANAGEMENT_KEY")
        or os.environ.get("CLI_PROXY_MANAGEMENT_KEY")
        or str(cfg.get("cpa_cloud_management_key") or "")
    ).strip()


# Deferred cloud uploads for GUI sequential registration (flush after batch).
_gui_pending_cloud_lock = threading.Lock()
_gui_pending_cloud_paths: list = []


def queue_cpa_cloud_upload_path(cpa_path: str | None) -> None:
    if not cpa_path:
        return
    pth = os.path.abspath(os.path.expanduser(str(cpa_path)))
    to_flush: list = []
    with _gui_pending_cloud_lock:
        if pth not in _gui_pending_cloud_paths:
            _gui_pending_cloud_paths.append(pth)
        try:
            every = int(config.get("cpa_cloud_upload_batch_every", 10) or 10)
        except Exception:
            every = 10
        every = max(0, min(every, 1000))
        if (
            config.get("cpa_cloud_upload_enabled", False)
            and every > 0
            and len(_gui_pending_cloud_paths) >= every
        ):
            to_flush = _gui_pending_cloud_paths[:every]
            del _gui_pending_cloud_paths[:every]
    if to_flush:
        # mid-batch flush (GUI sequential mint also benefits from every-N upload)
        try:
            # local import-safe: function defined below/above in same module
            flush_queued_cpa_cloud_uploads(config, log_callback=lambda m: print(m, flush=True), paths=to_flush)
        except TypeError:
            # older signature without paths=
            with _gui_pending_cloud_lock:
                _gui_pending_cloud_paths[0:0] = to_flush


def flush_queued_cpa_cloud_uploads(cfg=None, log_callback=None, paths=None) -> dict:
    """Flush GUI-queued CPA files with account-round-robin chat gate.

    paths=None drains all pending; paths=[...] flushes a mid-batch chunk.
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    if not cfg.get("cpa_cloud_upload_enabled", False):
        if paths is None:
            with _gui_pending_cloud_lock:
                _gui_pending_cloud_paths.clear()
        log("[cloud-cpa] batch upload skipped: cpa_cloud_upload_enabled=false")
        return {"ok": True, "skipped": True, "count": 0}
    if paths is None:
        with _gui_pending_cloud_lock:
            paths = list(_gui_pending_cloud_paths)
            _gui_pending_cloud_paths.clear()
    if not paths:
        log("[cloud-cpa] batch upload: no queued CPA files")
        return {"ok": True, "count": 0}

    upload_paths = list(paths)
    skip = 0
    if bool(cfg.get("cpa_cloud_upload_require_chat", True)):
        log(
            f"[cloud-cpa] batch chat gate start: {len(paths)} account(s) "
            f"(round-robin by account)"
        )
        gate = probe_cpa_auth_paths_round_robin(paths, cfg=cfg, log_callback=log)
        upload_paths = list(gate.get("passed") or [])
        for fpath, fres in (gate.get("failed") or {}).items():
            skip += 1
            st = fres.get("status")
            reason = fres.get("reason") or "chat_not_usable"
            log(
                f"[cloud-cpa] skipped {os.path.basename(fpath)}: {reason}"
                + (f" chat_status={st}" if st is not None else "")
            )
        log(
            f"[cloud-cpa] batch chat gate result: pass={len(upload_paths)} "
            f"skip={skip} rounds={gate.get('rounds')}"
        )

    log(f"[cloud-cpa] batch upload start: {len(upload_paths)} file(s) (of {len(paths)} minted)")
    ok = fail = 0
    for path in upload_paths:
        try:
            res = upload_cpa_auth_file_to_cloud(
                path, cfg, log, skip_chat_gate=True
            )
            if res.get("ok"):
                ok += 1
            elif res.get("skipped"):
                skip += 1
            else:
                fail += 1
        except Exception as exc:
            fail += 1
            log(f"[cloud-cpa] upload exception {os.path.basename(path)}: {exc}")
    log(f"[cloud-cpa] batch upload done: ok={ok} skip={skip} fail={fail} total={len(paths)}")
    return {"ok": fail == 0, "count": len(paths), "success": ok, "skip": skip, "fail": fail}


def _probe_cpa_chat_once(cpa_path, cfg=None):
    """Single Free Build POST /responses probe for one CPA auth file."""
    cfg = cfg or config
    path = os.path.abspath(os.path.expanduser(str(cpa_path or "")))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return {"ok": False, "status": 0, "reason": "auth_file_unreadable", "error": str(exc), "path": path}
    if not isinstance(data, dict):
        return {"ok": False, "status": 0, "reason": "auth_file_invalid", "error": "not a JSON object", "path": path}
    token = str(data.get("access_token") or "").strip()
    if not token:
        return {"ok": False, "status": 0, "reason": "missing_access_token", "error": "access_token empty", "path": path}
    base_url = str(
        data.get("base_url")
        or cfg.get("cpa_base_url")
        or "https://cli-chat-proxy.grok.com/v1"
    ).strip()
    proxy = (
        str(cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip()
        or (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()
        or None
    )
    timeout = float(cfg.get("cpa_cloud_upload_chat_timeout", 45) or 45)
    try:
        from grok_register.cpa_xai.probe import probe_mini_response

        probe = probe_mini_response(
            token,
            base_url=base_url,
            timeout=timeout,
            proxy=proxy,
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": 0,
            "reason": "probe_exception",
            "error": str(exc)[:300],
            "path": path,
            "name": os.path.basename(path),
        }
    status = int(probe.get("status") or 0)
    err = str(probe.get("error") or "")
    name = os.path.basename(path)
    if probe.get("ok") and status and 200 <= status < 300:
        return {
            "ok": True,
            "status": status,
            "reason": "chat_ok",
            "probe": probe,
            "path": path,
            "name": name,
        }
    if status == 403 or "permission-denied" in err.lower():
        reason = "chat_403"
    else:
        reason = "chat_probe_failed"
    return {
        "ok": False,
        "status": status or 0,
        "reason": reason,
        "error": err[:300] or str(probe)[:300],
        "probe": probe,
        "path": path,
        "name": name,
    }


def probe_cpa_auth_paths_round_robin(paths, cfg=None, log_callback=None):
    """Probe many CPA files by account-round rotation (not consecutive N hits on one file).

    Round r: try each still-pending account once.
    Any success in any round marks that account as uploadable.
    Returns dict(passed=[...], failed={path: last_result}, rounds=N).
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    try:
        rounds = int(cfg.get("cpa_cloud_upload_chat_rounds", 3) or 3)
    except Exception:
        rounds = 3
    rounds = max(1, min(rounds, 10))
    try:
        interval = float(cfg.get("cpa_cloud_upload_chat_interval", 0.2) or 0)
    except Exception:
        interval = 0.2
    interval = max(0.0, min(interval, 5.0))

    pending = []
    seen = set()
    for raw in paths or []:
        p = os.path.abspath(os.path.expanduser(str(raw or "")))
        if not p or p in seen:
            continue
        seen.add(p)
        if os.path.isfile(p):
            pending.append(p)
        else:
            log(f"[cloud-cpa] chat gate skip missing file: {os.path.basename(p)}")

    passed = []
    failed = {}
    if not pending:
        return {"passed": [], "failed": {}, "rounds": rounds}

    log(
        f"[cloud-cpa] chat gate (account-round-robin): {len(pending)} account(s), "
        f"rounds={rounds}, interval={interval}s"
    )
    for r in range(1, rounds + 1):
        if not pending:
            break
        log(f"[cloud-cpa] chat gate round {r}/{rounds}: probing {len(pending)} pending account(s)")
        still = []
        for path in pending:
            res = _probe_cpa_chat_once(path, cfg)
            name = res.get("name") or os.path.basename(path)
            if res.get("ok"):
                log(
                    f"[cloud-cpa] chat gate round {r}/{rounds}: PASS {name} "
                    f"status={res.get('status')}"
                )
                passed.append(path)
            else:
                log(
                    f"[cloud-cpa] chat gate round {r}/{rounds}: fail {name} "
                    f"status={res.get('status')} reason={res.get('reason')}"
                )
                failed[path] = res
                still.append(path)
            if interval > 0:
                time.sleep(interval)
        pending = still

    for path in pending:
        # still failed after all rounds
        if path not in failed:
            failed[path] = {
                "ok": False,
                "status": 0,
                "reason": "chat_probe_failed",
                "path": path,
                "name": os.path.basename(path),
            }
    # remove passed from failed map
    for path in passed:
        failed.pop(path, None)

    log(
        f"[cloud-cpa] chat gate done: pass={len(passed)} fail={len(failed)} "
        f"rounds={rounds}"
    )
    return {"passed": passed, "failed": failed, "rounds": rounds}


def _probe_cpa_chat_for_upload(cpa_path, cfg=None, log_callback=None):
    """Backward-compatible single-file gate: multi-round on one account.

    Prefer probe_cpa_auth_paths_round_robin for batch uploads.
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    path = os.path.abspath(os.path.expanduser(str(cpa_path or "")))
    result = probe_cpa_auth_paths_round_robin([path], cfg=cfg, log_callback=log)
    if path in result.get("passed") or []:
        return {
            "ok": True,
            "status": 200,
            "reason": "chat_ok",
            "rounds": result.get("rounds"),
            "path": path,
            "name": os.path.basename(path),
        }
    fail = (result.get("failed") or {}).get(path) or {
        "ok": False,
        "status": 0,
        "reason": "chat_probe_failed",
    }
    fail["ok"] = False
    fail["rounds"] = result.get("rounds")
    return fail


def upload_cpa_auth_file_to_cloud(cpa_path, cfg=None, log_callback=None, skip_chat_gate=False):
    """Upload one local CPA/OIDC JSON auth file to CLI Proxy cloud /auth-files.

    When ``cpa_cloud_upload_require_chat`` is true (default) and
    ``skip_chat_gate`` is false, probes chat before upload.
    For batch jobs prefer probe_cpa_auth_paths_round_robin then upload with
    skip_chat_gate=True.
    """
    cfg = cfg or config
    log = log_callback or (lambda m: None)
    if not cfg.get("cpa_cloud_upload_enabled", False):
        return {"ok": False, "skipped": True, "reason": "disabled"}
    path = os.path.abspath(os.path.expanduser(str(cpa_path or "")))
    if not path or not os.path.isfile(path):
        log(f"[cloud-cpa] upload skipped: file not found: {path}")
        return {"ok": False, "error": "file_not_found", "path": path}
    if (not skip_chat_gate) and bool(cfg.get("cpa_cloud_upload_require_chat", True)):
        chat_gate = _probe_cpa_chat_for_upload(path, cfg, log)
        if not chat_gate.get("ok"):
            status = chat_gate.get("status")
            reason = chat_gate.get("reason") or chat_gate.get("error") or "chat_probe_failed"
            rounds = chat_gate.get("rounds") or cfg.get("cpa_cloud_upload_chat_rounds", 3)
            attempts = chat_gate.get("attempts") or []
            log(
                f"[cloud-cpa] skip upload (chat not usable after {rounds} rounds, "
                f"status={status}): {os.path.basename(path)} reason={reason}"
            )
            return {
                "ok": False,
                "skipped": True,
                "reason": "chat_not_usable",
                "chat_status": status,
                "chat_error": chat_gate.get("error"),
                "chat_rounds": rounds,
                "chat_attempts": attempts,
                "path": path,
                "name": os.path.basename(path),
            }
    api_base = normalize_cpa_cloud_api_base(cfg.get("cpa_cloud_api_base") or os.environ.get("CPA_CLOUD_API_BASE") or "")
    if not api_base:
        log("[cloud-cpa] upload skipped: cpa_cloud_api_base is empty")
        return {"ok": False, "error": "missing_api_base", "path": path}
    key = get_cpa_cloud_management_key(cfg)
    if not key:
        log("[cloud-cpa] upload skipped: management key is empty")
        return {"ok": False, "error": "missing_management_key", "path": path}
    url = api_base + "/auth-files"
    timeout = config_int("cpa_cloud_upload_timeout", 30, minimum=5, maximum=180)
    name = os.path.basename(path)
    retries = config_int("cpa_cloud_upload_retries", 3, minimum=1, maximum=10)
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with open(path, "rb") as fh:
                files = {"file": (name, fh, "application/json")}
                headers = {"Authorization": "Bearer " + key}
                res = std_requests.post(url, headers=headers, files=files, timeout=timeout)
            preview = response_preview(res, 300)
            if 200 <= res.status_code < 300:
                try:
                    data = res.json()
                except Exception:
                    data = {"raw": preview}
                uploaded = data.get("uploaded") if isinstance(data, dict) else None
                suffix = f" uploaded={uploaded}" if uploaded is not None else ""
                log(f"[cloud-cpa] uploaded -> {name} status={res.status_code}{suffix}")
                return {"ok": True, "status_code": res.status_code, "path": path, "name": name, "response": data}
            last_error = f"status={res.status_code} body={preview}"
            if attempt < retries and res.status_code in (408, 429, 500, 502, 503, 504):
                log(f"[cloud-cpa] upload retry {attempt}/{retries}: {last_error}")
                time.sleep(min(2 * attempt, 8))
                continue
            log(f"[cloud-cpa] upload failed: {last_error}")
            return {"ok": False, "status_code": res.status_code, "path": path, "name": name, "error": preview}
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                log(f"[cloud-cpa] upload retry {attempt}/{retries}: {exc}")
                time.sleep(min(2 * attempt, 8))
                continue
            log(f"[cloud-cpa] upload exception: {exc}")
            return {"ok": False, "path": path, "name": name, "error": str(exc)}
    return {"ok": False, "path": path, "name": name, "error": last_error or "unknown"}


def run_cpa_and_sub2api_export(email, password, sso, log_callback=None):
    page = _tls_get_page()
    browser = _tls_get_browser()
    """Mint CPA xAI auth and convert it to Sub2API JSON after a successful registration."""
    log = log_callback or (lambda m: None)
    if not config.get("cpa_export_enabled", True):
        log("[cpa] export disabled")
        return {"ok": False, "skipped": True, "reason": "disabled"}
    try:
        from grok_register import cpa_export

        page_obj = page
        cookies = None
        try:
            cookies = cpa_export.export_cookies_from_page(page_obj)
        except Exception:
            cookies = None
        # The registration page is no longer needed once SSO/password/cookies are
        # captured. Close it before CPA mint so each account cleans its browser
        # profile instead of leaving the success page open during OIDC export.
        try:
            stop_browser()
            log("[*] 注册浏览器已关闭并清理痕迹，开始 CPA/Sub2API 导出")
            # Give Chrome a moment to fully release ports / profile locks on Windows.
            time.sleep(0.8)
        except Exception as close_exc:
            log(f"[Debug] 注册浏览器关闭失败: {close_exc}")
        result = cpa_export.export_cpa_xai_for_account(
            email,
            password,
            page=None,
            cookies=cookies,
            sso=sso,
            config=config,
            log_callback=log,
        )
        if result.get("ok"):
            log(f"[cpa] auth -> {result.get('path')}")
            cloud_path = result.get("cpa_path") or result.get("path")
            # Defer cloud upload until whole GUI batch ends (account-round-robin gate).
            if cloud_path and config.get("cpa_cloud_upload_enabled", False):
                queue_cpa_cloud_upload_path(cloud_path)
                log(f"[cloud-cpa] queued for batch upload: {os.path.basename(str(cloud_path))}")
                result["cloud_cpa_upload"] = {"ok": True, "queued": True, "path": cloud_path}
            else:
                result["cloud_cpa_upload"] = {"ok": False, "skipped": True, "reason": "disabled_or_no_path"}
            sub = result.get("sub2api") or {}
            if sub.get("ok"):
                log(f"[sub2api] json -> {sub.get('combined_path') or sub.get('path')}")
            elif result.get("sub2api_error"):
                log(f"[sub2api] export failed: {result.get('sub2api_error')}")
        else:
            log(f"[cpa] auth 未成功: {result.get('error') or result}")
        return result
    except Exception as exc:
        log(f"[cpa/sub2api] export exception: {exc}")
        if config.get("cpa_mint_required", False):
            raise
        return {"ok": False, "error": str(exc)}
    finally:
        # CPA mint uses its own standalone Chromium.  Do not leave that page open
        # after a registration; otherwise it looks like the registrar never
        # closes/cleans up even though the main register browser is stopped.
        try:
            from grok_register.cpa_xai.browser_confirm import shutdown_mint_browsers

            shutdown_mint_browsers()
            log("[cpa] mint browser closed")
        except Exception as cleanup_exc:
            log(f"[cpa] mint browser cleanup skipped: {cleanup_exc}")

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
                try:
                    line = f"{email}----{profile.get('password','')}----{sso}\n"
                    with open(accounts_output_file, "a", encoding="utf-8") as f:
                        f.write(line)
                except Exception as file_exc:
                    cli_log(f"[Debug] 保存账号文件失败: {file_exc}")
                add_token_to_grok2api_pools(sso, email=email, log_callback=cli_log)
                run_cpa_and_sub2api_export(
                    email,
                    profile.get("password", "") or "",
                    sso,
                    log_callback=cli_log,
                )
                success_count += 1
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[+] 注册成功: {email}")
                cli_log(f"[*] 当前统计: 成功 {success_count} | 失败 {fail_count}")
                if success_count > 0 and success_count % MEMORY_CLEANUP_INTERVAL == 0 and i < count:
                    cleanup_runtime_memory(
                        log_callback=cli_log,
                        reason=f"已成功 {success_count} 个账号，执行定期清理",
                    )
            except RegistrationCancelled:
                cli_log("[!] 注册被停止")
                break
            except AccountRetryNeeded as exc:
                retry_count_for_slot += 1
                if retry_count_for_slot <= max_slot_retry:
                    cli_log(
                        f"[!] 当前账号流程卡住，重试第 {retry_count_for_slot}/{max_slot_retry} 次: {exc}"
                    )
                else:
                    fail_count += 1
                    retry_count_for_slot = 0
                    i += 1
                    cli_log(f"[-] 当前账号已达到最大重试次数，跳过: {exc}")
            except Exception as exc:
                fail_count += 1
                retry_count_for_slot = 0
                i += 1
                cli_log(f"[-] 注册失败: {exc}")
            finally:
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
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() in ("start", "cli", "--cli"):
        main_cli()
        return
    root = tk.Tk()
    setup_light_theme(root)
    app = GrokRegisterGUI(root)
    def _on_close():
        try:
            app.stop_requested = True
        except Exception:
            pass
        try:
            stop_browser()
        except Exception:
            pass
        try:
            from grok_register.cpa_xai.browser_confirm import shutdown_mint_browsers

            shutdown_mint_browsers()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

