#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Microsoft Graph / Exchange Online catch-all temporary addresses.

Does **not** create users. Workflow:

1. Generate a random local-part on a catch-all domain (e.g. ``tmpxxx@tenant.onmicrosoft.com``).
2. Mail lands in a fixed catch-all mailbox (transport rule / M365 catch-all).
3. Poll that mailbox via Graph and match messages by original recipient.

Requires an app registration (client credentials) with application permission
``Mail.Read`` (admin consent). The app must be able to read the catch-all mailbox.
"""

from __future__ import annotations

import re
import secrets
import string
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import quote


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Headers that may carry the pre-catch-all recipient on some tenants / rules.
_ORIGINAL_TO_HEADERS = (
    "x-original-to",
    "x-forwarded-to",
    "delivered-to",
    "x-delivered-to",
    "envelope-to",
    "x-envelope-to",
    "x-ms-exchange-organization-originalrecipient",
)

HttpGet = Callable[..., Any]
HttpPost = Callable[..., Any]
LogFn = Optional[Callable[[str], None]]
CancelFn = Optional[Callable[[], bool]]
SleepFn = Optional[Callable[[float, CancelFn], None]]
ExtractFn = Callable[[str, str], Optional[str]]


class ExchangeMailError(Exception):
    """Raised for Graph / Exchange catch-all failures."""


class _TokenCache:
    """Thread-safe client-credentials token cache (per tenant+client)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            item = self._tokens.get(key)
            if not item:
                return None
            token, expires_at = item
            if time.time() >= expires_at:
                self._tokens.pop(key, None)
                return None
            return token

    def set(self, key: str, token: str, expires_in: float) -> None:
        ttl = max(30.0, float(expires_in) - 90.0)
        with self._lock:
            self._tokens[key] = (token, time.time() + ttl)

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()


_TOKEN_CACHE = _TokenCache()


def _default_sleep(seconds: float, cancel_callback: CancelFn = None) -> None:
    deadline = time.time() + max(seconds, 0)
    while True:
        if cancel_callback and cancel_callback():
            raise ExchangeMailError("cancelled")
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def generate_mail_username(length: int = 12, prefix: str = "tmp") -> str:
    """Generate a local-part safe for addresses (starts with a letter)."""
    length = max(4, min(int(length or 12), 48))
    prefix = re.sub(r"[^a-z0-9]", "", str(prefix or "tmp").lower()) or "tmp"
    body_len = max(4, length - len(prefix))
    alphabet = string.ascii_lowercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(body_len))
    if not body[0].isalpha():
        body = secrets.choice(string.ascii_lowercase) + body[1:]
    return f"{prefix}{body}"[:64]


def _response_text(resp: Any, limit: int = 400) -> str:
    try:
        text = str(getattr(resp, "text", "") or "")
    except Exception:
        text = ""
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _graph_error_message(resp: Any) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            code = err.get("code") or ""
            message = err.get("message") or ""
            return f"{code}: {message}".strip(": ")
    except Exception:
        pass
    return _response_text(resp)


def _normalize_addr(value: str | None) -> str:
    return str(value or "").strip().lower()


def message_recipient_addresses(msg: dict) -> set[str]:
    """Collect To / Cc / original-recipient style addresses from a Graph message."""
    found: set[str] = set()
    if not isinstance(msg, dict):
        return found

    for key in ("toRecipients", "ccRecipients", "bccRecipients"):
        for item in msg.get(key) or []:
            if not isinstance(item, dict):
                continue
            email_obj = item.get("emailAddress") if isinstance(item.get("emailAddress"), dict) else item
            addr = _normalize_addr(
                (email_obj or {}).get("address") if isinstance(email_obj, dict) else None
            )
            if addr:
                found.add(addr)

    for header in msg.get("internetMessageHeaders") or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip().lower()
        if name not in _ORIGINAL_TO_HEADERS and name not in ("to", "cc"):
            continue
        raw = str(header.get("value") or "")
        for match in re.findall(r"[\w.+\-]+@[\w.\-]+", raw):
            found.add(match.lower())

    return found


def message_matches_address(msg: dict, email: str) -> bool:
    """True when message is addressed to *email* (catch-all original recipient)."""
    target = _normalize_addr(email)
    if not target:
        return False
    recipients = message_recipient_addresses(msg)
    if target in recipients:
        return True
    # Some rules only leave the original address in preview/body headers.
    hay = " ".join(
        [
            str(msg.get("subject") or ""),
            str(msg.get("bodyPreview") or ""),
            " ".join(recipients),
        ]
    ).lower()
    return target in hay


def message_body_text(msg: dict) -> tuple[str, str]:
    """Return (combined_text, subject) from a Graph message dict."""
    subject = str(msg.get("subject") or "")
    parts: list[str] = []
    preview = msg.get("bodyPreview")
    if isinstance(preview, str) and preview.strip():
        parts.append(preview)
    body = msg.get("body") or {}
    if isinstance(body, dict):
        content = body.get("content") or ""
        if content:
            ctype = str(body.get("contentType") or "").lower()
            if ctype == "html":
                parts.append(re.sub(r"<[^>]+>", " ", content))
            else:
                parts.append(str(content))
    elif isinstance(body, str) and body.strip():
        parts.append(body)
    return "\n".join(parts), subject


class ExchangeMailClient:
    """Catch-all temp addresses via Microsoft Graph (fixed mailbox poll)."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        mailbox: str = "",
        domains: list[str] | str | None = None,
        username_length: int = 12,
        username_prefix: str = "tmp",
        list_top: int = 50,
        graph_base: str = GRAPH_BASE,
        http_get: HttpGet | None = None,
        http_post: HttpPost | None = None,
        sleep_fn: SleepFn = None,
    ) -> None:
        self.tenant_id = str(tenant_id or "").strip()
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.mailbox = str(mailbox or "").strip()
        if isinstance(domains, str):
            domain_list = [x.strip() for x in domains.replace(";", ",").split(",") if x.strip()]
        else:
            domain_list = [str(x).strip() for x in (domains or []) if str(x).strip()]
        self.domains = domain_list
        self.username_length = int(username_length or 12)
        self.username_prefix = str(username_prefix or "tmp")
        self.list_top = max(5, min(int(list_top or 50), 100))
        self.graph_base = str(graph_base or GRAPH_BASE).rstrip("/")
        self._http_get = http_get
        self._http_post = http_post
        self._sleep = sleep_fn or _default_sleep
        self._domain_index = 0
        self._domain_lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        config: dict,
        *,
        http_get: HttpGet | None = None,
        http_post: HttpPost | None = None,
        sleep_fn: SleepFn = None,
    ) -> "ExchangeMailClient":
        domains = str(config.get("exchange_domains", "") or "").strip()
        if not domains:
            domains = str(config.get("defaultDomains", "") or "").strip()
        return cls(
            tenant_id=str(config.get("exchange_tenant_id", "") or ""),
            client_id=str(config.get("exchange_client_id", "") or ""),
            client_secret=str(config.get("exchange_client_secret", "") or ""),
            mailbox=str(config.get("exchange_mailbox", "") or ""),
            domains=domains,
            username_length=int(config.get("exchange_username_length", 12) or 12),
            username_prefix=str(config.get("exchange_username_prefix", "tmp") or "tmp"),
            list_top=int(config.get("exchange_list_top", 50) or 50),
            graph_base=str(config.get("exchange_graph_base", GRAPH_BASE) or GRAPH_BASE),
            http_get=http_get,
            http_post=http_post,
            sleep_fn=sleep_fn,
        )

    def validate(self) -> None:
        missing = []
        if not self.tenant_id:
            missing.append("exchange_tenant_id")
        if not self.client_id:
            missing.append("exchange_client_id")
        if not self.client_secret:
            missing.append("exchange_client_secret")
        if not self.mailbox:
            missing.append("exchange_mailbox")
        if not self.domains:
            missing.append("exchange_domains (or defaultDomains)")
        if missing:
            raise ExchangeMailError(
                "Exchange catch-all 未配置完整，缺少: " + ", ".join(missing)
            )

    # ------------------------------------------------------------------ HTTP
    def _require_http(self) -> None:
        if not self._http_get or not self._http_post:
            raise ExchangeMailError("ExchangeMailClient 缺少 http_get/http_post")

    def _get(self, url: str, **kwargs: Any) -> Any:
        self._require_http()
        kwargs.setdefault("proxies", {})
        kwargs.setdefault("timeout", 30)
        return self._http_get(url, **kwargs)

    def _post(self, url: str, **kwargs: Any) -> Any:
        self._require_http()
        kwargs.setdefault("proxies", {})
        kwargs.setdefault("timeout", 30)
        return self._http_post(url, **kwargs)

    # ------------------------------------------------------------------ Auth
    def get_access_token(self, force_refresh: bool = False) -> str:
        self.validate()
        cache_key = f"{self.tenant_id}:{self.client_id}"
        if not force_refresh:
            cached = _TOKEN_CACHE.get(cache_key)
            if cached:
                return cached
        token_url = TOKEN_URL_TMPL.format(tenant=self.tenant_id)
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
        resp = self._post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxies={},
            timeout=30,
        )
        if getattr(resp, "status_code", 0) >= 400:
            raise ExchangeMailError(
                f"获取 Graph token 失败 HTTP {resp.status_code}: {_response_text(resp)}"
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise ExchangeMailError(f"Graph token 返回非 JSON: {_response_text(resp)}") from exc
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise ExchangeMailError(f"Graph token 响应缺少 access_token: {payload}")
        expires_in = float(payload.get("expires_in") or 3600)
        _TOKEN_CACHE.set(cache_key, token, expires_in)
        return token

    def _request_get(self, url: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {self.get_access_token()}"
        resp = self._get(url, headers=headers, **kwargs)
        if getattr(resp, "status_code", 0) == 401:
            headers["Authorization"] = f"Bearer {self.get_access_token(force_refresh=True)}"
            resp = self._get(url, headers=headers, **kwargs)
        return resp

    # ------------------------------------------------------------------ Domain / address
    def next_domain(self, blocked: Optional[Callable[[str], bool]] = None) -> str:
        if not self.domains:
            raise ExchangeMailError("未配置 exchange_domains / defaultDomains")
        blocked = blocked or (lambda _d: False)
        with self._domain_lock:
            n = len(self.domains)
            for _ in range(n * 2):
                domain = self.domains[self._domain_index % n].lstrip("@").strip().lower()
                self._domain_index += 1
                if domain and not blocked(domain):
                    return domain
        raise ExchangeMailError("所有 Exchange 域名均被 email_blocked_domains 禁用")

    def create_temp_address(
        self,
        *,
        domain: str | None = None,
        blocked: Optional[Callable[[str], bool]] = None,
        log_callback: LogFn = None,
    ) -> tuple[str, str]:
        """Allocate a catch-all address. Returns (address, mailbox_token).

        No Graph user is created — only a random local-part on the catch-all domain.
        ``mailbox_token`` is the configured catch-all mailbox UPN/id used for polling.
        """
        self.validate()
        domain = (domain or self.next_domain(blocked=blocked)).lstrip("@").strip().lower()
        if blocked and blocked(domain):
            raise ExchangeMailError(f"域名已被禁用: {domain}")
        local = generate_mail_username(self.username_length, self.username_prefix)
        address = f"{local}@{domain}"
        if log_callback:
            log_callback(f"[*] Exchange catch-all 地址: {address} → {self.mailbox}")
        return address, self.mailbox

    # ------------------------------------------------------------------ Mail
    def list_messages(self, mailbox: str | None = None, top: int | None = None) -> list[dict]:
        mb = str(mailbox or self.mailbox or "").strip()
        if not mb:
            raise ExchangeMailError("exchange_mailbox 未配置")
        top_n = int(top if top is not None else self.list_top)
        select = (
            "id,subject,body,bodyPreview,toRecipients,ccRecipients,"
            "receivedDateTime,from,internetMessageHeaders"
        )
        # Prefer recent mail; Prefer: outlook.timezone not required.
        url = (
            f"{self.graph_base}/users/{quote(mb)}/messages"
            f"?$top={top_n}&$orderby=receivedDateTime%20desc"
            f"&$select={select}"
        )
        resp = self._request_get(url)
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            raise ExchangeMailError(
                f"拉取 catch-all 邮箱失败 HTTP {status}: {_graph_error_message(resp)}"
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise ExchangeMailError(f"邮件列表非 JSON: {_response_text(resp)}") from exc
        value = data.get("value") if isinstance(data, dict) else None
        return value if isinstance(value, list) else []

    def get_oai_code(
        self,
        mailbox_token: str,
        email: str,
        *,
        timeout: float = 180,
        poll_interval: float = 3,
        log_callback: LogFn = None,
        cancel_callback: CancelFn = None,
        resend_callback: Optional[Callable[[], None]] = None,
        extract_fn: ExtractFn | None = None,
    ) -> str:
        """Poll the catch-all mailbox for a verification code addressed to *email*."""
        if extract_fn is None:
            raise ExchangeMailError("get_oai_code 需要 extract_fn")
        mailbox = str(mailbox_token or self.mailbox or "").strip()
        if not mailbox:
            raise ExchangeMailError("exchange_mailbox 未配置")

        deadline = time.time() + float(timeout)
        seen_attempts: dict[str, int] = {}
        next_resend_at = time.time() + 35
        last_wait_log = 0.0
        email_l = _normalize_addr(email)

        while time.time() < deadline:
            if cancel_callback and cancel_callback():
                raise ExchangeMailError("cancelled")
            now = time.time()
            if log_callback and now - last_wait_log >= 15:
                left = max(0, int(deadline - now))
                log_callback(f"[Debug] Exchange catch-all 等待验证码中，剩余 {left}s ({email_l})")
                last_wait_log = now

            if resend_callback and now >= next_resend_at:
                try:
                    resend_callback()
                    if log_callback:
                        log_callback("[*] 已触发重新发送验证码")
                except Exception as exc:
                    if log_callback:
                        log_callback(f"[Debug] 触发重发验证码失败: {exc}")
                next_resend_at = time.time() + 35

            try:
                messages = self.list_messages(mailbox)
            except ExchangeMailError as exc:
                if log_callback:
                    log_callback(f"[Debug] Exchange 拉取邮件失败: {exc}")
                self._sleep(poll_interval, cancel_callback)
                continue

            if log_callback:
                log_callback(f"[Debug] Exchange catch-all 本轮邮件数量: {len(messages)}")

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or "")
                if not msg_id:
                    continue
                attempt = int(seen_attempts.get(msg_id, 0))
                if attempt >= 5:
                    continue

                if not message_matches_address(msg, email_l):
                    continue

                seen_attempts[msg_id] = attempt + 1
                combined, subject = message_body_text(msg)
                if log_callback:
                    log_callback(f"[Debug] Exchange 匹配到邮件: {subject}")
                found = extract_fn(combined, subject)
                if found:
                    if log_callback:
                        log_callback(f"[*] Exchange 从邮件中提取到验证码: {found}")
                    return found
                if log_callback:
                    log_callback(
                        f"[Debug] 邮件已解析但未提取到验证码 id={msg_id[:12]}… "
                        f"attempt={seen_attempts[msg_id]}"
                    )

            self._sleep(poll_interval, cancel_callback)

        raise ExchangeMailError(f"Exchange catch-all 在 {int(timeout)}s 内未收到验证码邮件")


def clear_token_cache() -> None:
    """Test helper: drop cached Graph tokens."""
    _TOKEN_CACHE.clear()
