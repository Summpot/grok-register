"""Local Grok Web SSO → Build OAuth conversion via xAI Device Flow.

Aligned with grok-build (`xai-grok-shell` `auth/device_code.rs` +
`auth/config.rs` default OAuth2 client contract):

  1. Validate SSO cookie against accounts.x.ai
  2. POST auth.x.ai/oauth2/device/code  (Grok CLI client_id + scopes + referrer)
  3. Open verification_uri_complete (accounts.x.ai), POST device/verify + approve
     using the session SSO cookie (automation; grok-build does this in a browser)
  4. Poll oauth2/token for access_token + refresh_token

Two modes:
  - http    : pure HTTP with Cookie: sso=… for verify/approve
  - browser : use the registration Camoufox page for verify/approve steps
              (Device start + token poll still HTTP, matching grok-build)
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from curl_cffi import requests as curl_requests
import requests as std_requests

# Frozen OAuth2 client contract — keep in sync with grok-build
# crates/codegen/xai-grok-shell/src/auth/config.rs (default_oauth2_scopes).
SSO_BUILD_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SSO_BUILD_SCOPE = (
    "openid profile email offline_access "
    "grok-cli:access api:access "
    "conversations:read conversations:write "
    "workspaces:read workspaces:write"
)
SSO_BUILD_REFERRER = "grok-build"
# Identity headers sent by grok-build device_code.rs (metrics + provider routing).
SSO_BUILD_CLIENT_VERSION = "0.2.109"
SSO_BUILD_CLIENT_SURFACE = "cli"

SSO_ISSUER = "https://auth.x.ai"
SSO_ACCOUNTS_URL = "https://accounts.x.ai/"
SSO_DEVICE_URL = f"{SSO_ISSUER}/oauth2/device/code"
SSO_VERIFY_URL = f"{SSO_ISSUER}/oauth2/device/verify"
SSO_APPROVE_URL = f"{SSO_ISSUER}/oauth2/device/approve"
SSO_TOKEN_URL = f"{SSO_ISSUER}/oauth2/token"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
MAX_BODY = 2 << 20
DEFAULT_IMPERSONATE = "chrome136"
# device_code.rs: DEFAULT_DEVICE_POLL_INTERVAL_SECS / DEVICE_SLOW_DOWN_INCREMENT
DEFAULT_DEVICE_POLL_INTERVAL_SECS = 5
DEVICE_SLOW_DOWN_INCREMENT_SECS = 5
# device_code.rs: MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS (floor poll window)
MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS = 10 * 60
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"


class SSOBuildError(RuntimeError):
    """Device Flow conversion failed."""

    def __init__(self, message: str, *, status: int | None = None, unauthorized: bool = False):
        super().__init__(message)
        self.status = status
        self.unauthorized = unauthorized


def normalize_sso_token(value: str | None) -> str:
    value = str(value or "").strip()
    if value.lower().startswith("sso="):
        value = value[4:].strip()
    if ";" in value:
        value = value.split(";", 1)[0].strip()
    return value.replace("\r", "").replace("\n", "").replace("\x00", "")


def safe_xai_url(raw: str) -> bool:
    """True for https://*.x.ai (and localhost http for local auth parity)."""
    try:
        parsed = urlparse(str(raw or "").strip())
    except Exception:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return host == "x.ai" or host.endswith(".x.ai")
    # grok-build validate_verification_uri also allows local loopback http
    if parsed.scheme == "http" and host in ("localhost", "127.0.0.1"):
        return True
    return False


def valid_user_code(code: str) -> bool:
    """device_code.rs: user_code must be [A-Za-z0-9-]."""
    text = str(code or "").strip()
    if not text:
        return False
    return all(c.isalnum() or c == "-" for c in text)


def build_verification_uri_complete(
    verification_uri: str,
    user_code: str,
    verification_uri_complete: str = "",
) -> str:
    """Prefer server complete URI; else embed user_code like grok-build TUI."""
    complete = str(verification_uri_complete or "").strip()
    if complete and safe_xai_url(complete):
        return complete
    base = str(verification_uri or "").strip()
    code = str(user_code or "").strip()
    if not base or not code or not safe_xai_url(base):
        return ""
    parsed = urlparse(base)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "user_code" not in query:
        query["user_code"] = code
    return urlunparse(parsed._replace(query=urlencode(query)))


def url_is_auth_bounce(url: str) -> bool:
    text = str(url or "").lower()
    return any(
        marker in text
        for marker in ("sign-in", "sign-up", "/login", "oauth2/sign")
    )


def decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    pad = "=" * (-len(payload) % 4)
    try:
        data = base64.urlsafe_b64decode(payload + pad)
        claims = json.loads(data.decode("utf-8", errors="replace"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def claim_string(claims: dict[str, Any] | None, key: str) -> str:
    if not isinstance(claims, dict):
        return ""
    value = claims.get(key)
    return str(value).strip() if value is not None else ""


def access_token_has_bot_flag(access_token: str | None) -> bool:
    """True when Build access_token JWT has bot_flag_source == 1."""
    claims = decode_jwt_claims(str(access_token or ""))
    if not claims:
        return False
    value = claims.get("bot_flag_source")
    if value is True:
        return True
    if isinstance(value, (int, float)) and int(value) == 1:
        return True
    if isinstance(value, str) and value.strip() in ("1", "true", "True"):
        return True
    return False


def first_value(*values: str | None) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


class _CookieJar:
    def __init__(self, initial: dict[str, str] | None = None):
        self._cookies: dict[str, str] = {}
        if initial:
            for key, value in initial.items():
                self.set(key, value)

    def set(self, name: str, value: str) -> None:
        name = str(name or "").strip()
        value = str(value or "").strip()
        if not name or len(name) > 128 or len(value) > 16384:
            return
        if re.search(r"[\r\n\x00]", name + value):
            return
        self._cookies[name] = value

    def delete(self, name: str) -> None:
        self._cookies.pop(str(name or "").strip(), None)

    def header(self) -> str:
        keys = sorted(self._cookies.keys())
        return "; ".join(f"{k}={self._cookies[k]}" for k in keys)

    def as_dict(self) -> dict[str, str]:
        return dict(self._cookies)

    def capture_from_response(self, response) -> None:
        try:
            jar = getattr(response, "cookies", None)
            if jar is None:
                return
            # curl_cffi / requests cookie jar
            if hasattr(jar, "items"):
                for name, value in jar.items():
                    self.set(name, value)
            for cookie in getattr(jar, "jar", []) or []:
                try:
                    self.set(cookie.name, cookie.value)
                except Exception:
                    pass
        except Exception:
            pass
        # Also parse Set-Cookie headers (some backends expose only headers)
        try:
            headers = getattr(response, "headers", None) or {}
            raw_list = []
            if hasattr(headers, "getlist"):
                raw_list = headers.getlist("Set-Cookie") or headers.getlist("set-cookie") or []
            if not raw_list:
                single = headers.get("Set-Cookie") or headers.get("set-cookie")
                if single:
                    raw_list = [single]
            for raw in raw_list:
                part = str(raw).split(";", 1)[0]
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                if "max-age=0" in str(raw).lower() or "expires=thu, 01 jan 1970" in str(raw).lower():
                    self.delete(name)
                else:
                    self.set(name, value)
        except Exception:
            pass


class SSOBuildFlow:
    """HTTP Device Flow converter aligned with grok-build device_code.rs."""

    def __init__(
        self,
        sso_token: str,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        proxies: dict | None = None,
        impersonate: str = DEFAULT_IMPERSONATE,
        timeout: float = 30.0,
        client_surface: str = SSO_BUILD_CLIENT_SURFACE,
    ):
        token = normalize_sso_token(sso_token)
        if not token:
            raise SSOBuildError("SSO token empty", unauthorized=True)
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.proxies = proxies or {}
        self.impersonate = impersonate
        self.timeout = timeout
        self.client_surface = (client_surface or SSO_BUILD_CLIENT_SURFACE).strip() or "cli"
        self.cookies = _CookieJar({"sso": token, "sso-rw": token})

    def convert(self, *, email: str = "", name: str = "") -> dict[str, Any]:
        status, final_url, _body = self._do("GET", SSO_ACCOUNTS_URL, None)
        if status == 401 or url_is_auth_bounce(final_url):
            raise SSOBuildError("Grok Web SSO rejected", status=status, unauthorized=True)
        if status < 200 or status >= 400:
            raise SSOBuildError(f"validate SSO failed HTTP {status}", status=status)

        device = self._start_device()
        self._verify_and_approve_http(device)
        token = self._poll_token(
            device["device_code"],
            interval=float(device.get("interval") or DEFAULT_DEVICE_POLL_INTERVAL_SECS),
            expires_in=float(device.get("expires_in") or MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS),
        )
        return self._seed_from_token(token, email=email, name=name)

    def convert_with_browser(
        self,
        page,
        *,
        email: str = "",
        name: str = "",
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Use registration browser for verify/approve; HTTP for start + token poll."""
        self._ensure_browser_sso_cookies(page)
        status, final_url, _body = self._do("GET", SSO_ACCOUNTS_URL, None)
        if status == 401 or url_is_auth_bounce(final_url):
            raise SSOBuildError("Grok Web SSO rejected", status=status, unauthorized=True)
        if status < 200 or status >= 400:
            raise SSOBuildError(f"validate SSO failed HTTP {status}", status=status)

        device = self._start_device()
        verify_url = device.get("verification_uri_complete") or ""
        if not safe_xai_url(verify_url):
            raise SSOBuildError("device flow verification URL incomplete")

        if log_callback:
            log_callback(f"[*] Device Flow: browser open verify ({device.get('user_code')})")
        try:
            page.get(verify_url, timeout=45)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] browser open verify failed, HTTP fallback: {exc}")
            self._verify_and_approve_http(device)
            token = self._poll_token(
                device["device_code"],
                interval=float(device.get("interval") or DEFAULT_DEVICE_POLL_INTERVAL_SECS),
                expires_in=float(device.get("expires_in") or MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS),
            )
            return self._seed_from_token(token, email=email, name=name)

        # Pull any new cookies set by the auth pages
        self._import_browser_cookies(page)

        # Two distinct pages — never treat user-code/Continue as Allow:
        #   1) user code page  → Continue  (POST /oauth2/device/verify)
        #   2) consent page    → Allow     (POST /oauth2/device/approve action=allow)
        if not self._page_is_device_done(page):
            ok = self._browser_device_flow_steps(
                page, device["user_code"], log_callback=log_callback
            )
            if not ok:
                if log_callback:
                    log_callback("[*] Device Flow: browser steps missed, HTTP verify+approve")
                self._import_browser_cookies(page)
                self._verify_and_approve_http(device)

        if log_callback:
            log_callback("[*] Device Flow: polling OAuth token")
        token = self._poll_token(
            device["device_code"],
            interval=float(device.get("interval") or DEFAULT_DEVICE_POLL_INTERVAL_SECS),
            expires_in=float(device.get("expires_in") or MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS),
        )
        return self._seed_from_token(token, email=email, name=name)

    # ── steps ──────────────────────────────────────────────────────────

    def _start_device(self) -> dict[str, Any]:
        # Matches grok-build request_device_code form + client headers.
        status, _url, body = self._do(
            "POST",
            SSO_DEVICE_URL,
            {
                "client_id": SSO_BUILD_CLIENT_ID,
                "scope": SSO_BUILD_SCOPE,
                "referrer": SSO_BUILD_REFERRER,
            },
            api_client=True,
        )
        if status == 404:
            raise SSOBuildError(
                "Device-code login is not available for this deployment (HTTP 404)",
                status=status,
            )
        if status < 200 or status >= 300:
            snippet = ""
            try:
                snippet = (
                    body.decode("utf-8", errors="replace")
                    if isinstance(body, (bytes, bytearray))
                    else str(body)
                )[:300]
            except Exception:
                snippet = ""
            raise SSOBuildError(
                f"start Device Flow failed HTTP {status}: {snippet}".rstrip(": "),
                status=status,
            )
        try:
            payload = json.loads(
                body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
            )
        except Exception as exc:
            raise SSOBuildError(f"parse Device Flow response: {exc}") from exc
        device_code = str(payload.get("device_code") or "").strip()
        user_code = str(payload.get("user_code") or "").strip()
        verification_uri = str(payload.get("verification_uri") or "").strip()
        complete = build_verification_uri_complete(
            verification_uri,
            user_code,
            str(payload.get("verification_uri_complete") or ""),
        )
        if not device_code or not user_code:
            raise SSOBuildError("Device Flow response incomplete")
        if not valid_user_code(user_code):
            raise SSOBuildError(
                "Server returned invalid user_code format (expected [A-Z0-9-])"
            )
        if verification_uri and not safe_xai_url(verification_uri):
            raise SSOBuildError(f"Server returned unsupported verification URI: {verification_uri}")
        if not complete or not safe_xai_url(complete):
            raise SSOBuildError("Device Flow verification URL incomplete")
        interval = int(payload.get("interval") or DEFAULT_DEVICE_POLL_INTERVAL_SECS)
        expires_in = int(payload.get("expires_in") or 0)
        if interval <= 0:
            interval = DEFAULT_DEVICE_POLL_INTERVAL_SECS
        if expires_in <= 0:
            expires_in = MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri or complete,
            "verification_uri_complete": complete,
            "interval": interval,
            "expires_in": expires_in,
        }

    def _verify_and_approve_http(self, device: dict[str, Any]) -> None:
        complete = device["verification_uri_complete"]
        status, final_url, body = self._do("GET", complete, None)
        if status < 200 or status >= 400:
            raise SSOBuildError(
                f"open Device Flow verify page failed HTTP {status}", status=status
            )
        if url_is_auth_bounce(final_url):
            raise SSOBuildError(
                f"open Device Flow verify bounced to auth: {final_url}",
                unauthorized=True,
            )

        verify_url = self._extract_form_action(body, "device/verify") or SSO_VERIFY_URL
        if not safe_xai_url(verify_url):
            verify_url = SSO_VERIFY_URL

        status, final_url, body = self._do(
            "POST", verify_url, {"user_code": device["user_code"]}
        )
        if status < 200 or status >= 400:
            raise SSOBuildError(
                f"SSO auto-verify Device Flow failed HTTP {status}", status=status
            )
        if url_is_auth_bounce(final_url):
            raise SSOBuildError(
                f"SSO auto-verify bounced to auth: {final_url}",
                unauthorized=True,
            )
        if self._url_is_device_done(final_url):
            return

        # Some deployments land on consent HTML without "consent" in the URL.
        approve_url = self._extract_form_action(body, "device/approve") or SSO_APPROVE_URL
        if not safe_xai_url(approve_url):
            approve_url = SSO_APPROVE_URL

        status, final_url, _ = self._do(
            "POST",
            approve_url,
            {
                "user_code": device["user_code"],
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
        )
        if status < 200 or status >= 400:
            raise SSOBuildError(
                f"SSO auto-approve Device Flow failed HTTP {status}", status=status
            )
        if url_is_auth_bounce(final_url):
            raise SSOBuildError(
                f"SSO auto-approve Device Flow failed url={final_url}",
                unauthorized=True,
            )

    def _poll_token(
        self,
        device_code: str,
        *,
        interval: float,
        expires_in: float,
    ) -> dict[str, Any]:
        # Match device_code.rs complete_device_code_login:
        # sleep first, then poll until expires_in (floored at 10 minutes).
        interval = max(float(interval or 1), 1.0)
        poll_secs = max(float(expires_in or 0), float(MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS))
        deadline = time.time() + poll_secs
        while True:
            time.sleep(interval)
            if time.time() > deadline:
                raise SSOBuildError("Device code expired / poll timeout")
            status, _url, body = self._do(
                "POST",
                SSO_TOKEN_URL,
                {
                    "grant_type": DEVICE_GRANT_TYPE,
                    "client_id": SSO_BUILD_CLIENT_ID,
                    "device_code": device_code,
                },
                api_client=True,
            )
            try:
                payload = json.loads(
                    body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
                )
            except Exception as exc:
                raise SSOBuildError(f"parse OAuth token: {exc}") from exc
            if 200 <= status < 300 and payload.get("access_token"):
                expires = int(payload.get("expires_in") or 3600)
                if expires <= 0:
                    expires = 3600
                return {
                    "access_token": str(payload.get("access_token") or ""),
                    "refresh_token": str(payload.get("refresh_token") or ""),
                    "id_token": str(payload.get("id_token") or ""),
                    "expires_in": expires,
                    "expires_at": datetime.now(timezone.utc).timestamp() + expires,
                }
            err = str(payload.get("error") or "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += float(DEVICE_SLOW_DOWN_INCREMENT_SECS)
                continue
            if err == "access_denied":
                raise SSOBuildError(
                    "Authorization denied. The user rejected the request.",
                    unauthorized=True,
                )
            if err == "expired_token":
                raise SSOBuildError("Device code expired", unauthorized=True)
            if status >= 400:
                desc = first_value(payload.get("error_description"), err, str(status))
                raise SSOBuildError(f"OAuth token failed: {desc}", status=status)
            raise SSOBuildError(
                f"OAuth token failed: {first_value(payload.get('error_description'), err, str(status))}"
            )

    def _seed_from_token(self, token: dict[str, Any], *, email: str = "", name: str = "") -> dict[str, Any]:
        claims = decode_jwt_claims(first_value(token.get("id_token"), token.get("access_token")))
        user_id = claim_string(claims, "sub")
        claim_email = claim_string(claims, "email")
        team_id = claim_string(claims, "team_id")
        resolved_email = first_value(email, claim_email)
        resolved_name = first_value(resolved_email, name, user_id, "Grok Build account")
        expires_at = float(token.get("expires_at") or 0)
        return {
            "provider": "grok_build",
            "name": resolved_name,
            "email": resolved_email,
            "user_id": user_id,
            "team_id": team_id,
            "client_id": SSO_BUILD_CLIENT_ID,
            "access_token": token.get("access_token") or "",
            "refresh_token": token.get("refresh_token") or "",
            "id_token": token.get("id_token") or "",
            "token_type": "Bearer",
            "expires_in": int(token.get("expires_in") or 3600),
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            if expires_at
            else "",
            "scope": SSO_BUILD_SCOPE,
        }

    # ── browser helpers ────────────────────────────────────────────────

    def _ensure_browser_sso_cookies(self, page) -> None:
        token = self.cookies.as_dict().get("sso") or ""
        if not token:
            return
        cookies = []
        for domain in (".x.ai", "accounts.x.ai", "auth.x.ai"):
            for name in ("sso", "sso-rw"):
                cookies.append(
                    {
                        "name": name,
                        "value": token,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                        "sameSite": "None",
                    }
                )
        try:
            page.set.cookies(cookies)
        except Exception:
            try:
                ctx = getattr(page, "_ctx", None) or getattr(getattr(page, "_p", None), "context", None)
                if ctx is not None:
                    ctx.add_cookies(cookies)
            except Exception:
                pass

    def _import_browser_cookies(self, page) -> None:
        try:
            items = page.cookies(all_domains=True) or []
        except Exception:
            items = []
        for item in items:
            try:
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
                domain = str(item.get("domain") or "").lower()
                if not name or not value:
                    continue
                if "x.ai" not in domain:
                    continue
                self.cookies.set(name, value)
            except Exception:
                continue

    def _page_url(self, page) -> str:
        try:
            return str(getattr(page, "url", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _url_is_device_done(url: str) -> bool:
        text = str(url or "").lower()
        return any(
            marker in text
            for marker in (
                "/oauth2/device/done",
                "/oauth2/device/success",
                "device/done",
                "device/success",
            )
        )

    def _page_is_device_done(self, page) -> bool:
        return self._url_is_device_done(self._page_url(page))

    @staticmethod
    def _extract_form_action(body: bytes | str | None, path_fragment: str) -> str:
        """Pull form action URL containing path_fragment from an HTML body."""
        if body is None:
            return ""
        try:
            text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
        except Exception:
            return ""
        if not text or path_fragment not in text:
            return ""
        # Prefer absolute actions that include the fragment.
        patterns = (
            rf'''action=["'](https?://[^"']*{re.escape(path_fragment)}[^"']*)["']''',
            rf'''action=["']([^"']*{re.escape(path_fragment)}[^"']*)["']''',
        )
        for pat in patterns:
            match = re.search(pat, text, flags=re.I)
            if match:
                return str(match.group(1) or "").strip()
        return ""

    def _browser_page_phase(self, page) -> str:
        """Classify current Device Flow UI.

        Returns: done | consent | user_code | unknown
        """
        if self._page_is_device_done(page):
            return "done"
        try:
            phase = page.run_js(
                """
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const url = (location.href || '').toLowerCase();
if (url.includes('/oauth2/device/done') || url.includes('device/success') || url.includes('device/done')) return 'done';

const forms = Array.from(document.querySelectorAll('form'));
const approveForm = forms.find((f) => {
  const a = (f.getAttribute('action') || '');
  return a.includes('device/approve');
});
if (approveForm) return 'consent';

const verifyForm = forms.find((f) => {
  const a = (f.getAttribute('action') || '');
  return a.includes('device/verify');
});
if (verifyForm) return 'user_code';

const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"]'));
const texts = buttons.map((b) => normalize(b.innerText || b.textContent || b.value || ''));
const hasAllow = texts.some((t) => t === 'allow' || t === '允许');
const hasDeny = texts.some((t) => t === 'deny' || t === '拒绝' || t === '取消');
const hasContinue = texts.some((t) => t === 'continue' || t === '继续' || t === '确认');

const heading = Array.from(document.querySelectorAll('h1,h2')).map(
  (el) => normalize(el.textContent || '')
).join(' | ');
if (/authorize grok build|授权/.test(heading) && hasAllow) return 'consent';
if (hasAllow && hasDeny) return 'consent';
if (hasContinue && !hasAllow) return 'user_code';
// Current provider hosts the code page at accounts.x.ai/oauth2/device?user_code=…
if (
  url.includes('user_code')
  || url.includes('device/verify')
  || url.includes('/oauth2/device/user_code')
  || /\\/oauth2\\/device\\/?(\\?|$)/.test(url)
) {
  return 'user_code';
}
if (url.includes('consent') || url.includes('device/approve')) return 'consent';

// user_code input present but no Allow pair → still on code page
const codeInput = document.querySelector('input[name="user_code"], input[id*="user"], input[autocomplete="one-time-code"]');
if (codeInput && !hasAllow) return 'user_code';
return 'unknown';
"""
            )
        except Exception:
            phase = "unknown"
        phase = str(phase or "unknown").strip().lower()
        if phase in ("done", "consent", "user_code", "unknown"):
            return phase
        return "unknown"

    def _wait_browser_phase(
        self,
        page,
        wanted: set[str] | frozenset[str],
        *,
        timeout: float = 15.0,
        log_callback: Callable[[str], None] | None = None,
        label: str = "",
    ) -> str:
        deadline = time.time() + max(timeout, 1.0)
        last = "unknown"
        while time.time() < deadline:
            last = self._browser_page_phase(page)
            if last in wanted:
                return last
            if last == "done" and "done" not in wanted:
                return last
            url = self._page_url(page).lower()
            if "sign-in" in url or "sign-up" in url:
                if log_callback:
                    log_callback(f"[Debug] Device Flow bounced to auth: {url[:100]}")
                return "auth_bounce"
            time.sleep(0.3)
        if log_callback and label:
            log_callback(
                f"[Debug] wait {label} timeout (phase={last}, url={self._page_url(page)[:100]})"
            )
        return last

    def _browser_device_flow_steps(
        self,
        page,
        user_code: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> bool:
        """Browser: Continue on user-code page, then Allow on consent page."""
        phase = self._wait_browser_phase(
            page,
            {"user_code", "consent", "done"},
            timeout=12.0,
            log_callback=log_callback,
            label="device UI",
        )
        if phase == "done":
            return True
        if phase == "auth_bounce":
            return False

        # Step 1: user code page → Continue (never Allow here)
        if phase == "user_code":
            if log_callback:
                log_callback("[*] Device Flow: user-code page → Continue")
            if not self._browser_submit_continue(
                page, user_code, log_callback=log_callback
            ):
                return False
            phase = self._wait_browser_phase(
                page,
                {"consent", "done"},
                timeout=15.0,
                log_callback=log_callback,
                label="consent after Continue",
            )
            if phase == "done":
                return True
            if phase != "consent":
                return False

        # Step 2: consent page → Allow only
        if phase == "consent":
            if log_callback:
                log_callback("[*] Device Flow: consent page → Allow")
            return self._browser_submit_allow(
                page, user_code, log_callback=log_callback
            )

        if log_callback:
            log_callback(f"[Debug] Device Flow unknown page phase={phase}")
        return False

    def _browser_submit_continue(
        self,
        page,
        user_code: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> bool:
        """Click Continue on the user-code verification page.

        Equivalent to POST /oauth2/device/verify with user_code.
        Must not touch the consent Allow/Deny form.
        """
        user_code_js = json.dumps(str(user_code or ""))
        js = f"""
const userCode = {user_code_js};
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

// Refuse to act on consent form — that is the next page.
const approveForm = document.querySelector('form[action*="device/approve"]');
if (approveForm) {{
  return {{ ok: false, reason: 'already_on_consent' }};
}}

const forms = Array.from(document.querySelectorAll('form'));
let form = forms.find((f) => {{
  const a = (f.getAttribute('action') || '');
  return a.includes('device/verify');
}}) || null;
if (!form) {{
  form = forms.find((f) => f.querySelector('input[name="user_code"]')) || null;
}}

const setField = (root, name, value) => {{
  let input = root.querySelector('input[name="' + name + '"]');
  if (!input) {{
    input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    root.appendChild(input);
  }}
  input.value = value;
}};

if (form) {{
  if (userCode) setField(form, 'user_code', userCode);
  const buttons = Array.from(form.querySelectorAll('button, input[type="submit"]'));
  const cont = buttons.find((b) => {{
    const t = normalize(b.innerText || b.textContent || b.value || '');
    return t === 'continue' || t === '继续' || t === '确认' || t === 'next' || t === '下一步';
  }});
  // Do not click Allow/Deny if somehow present.
  if (cont) {{
    cont.click();
    return {{ ok: true, via: 'continue_click' }};
  }}
  // Single submit on verify form is Continue.
  if (buttons.length === 1) {{
    buttons[0].click();
    return {{ ok: true, via: 'single_submit' }};
  }}
  form.submit();
  return {{ ok: true, via: 'verify_form_submit' }};
}}

// No form: click a page-level Continue button (SPA).
const allBtns = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"]'));
const contBtn = allBtns.find((b) => {{
  const t = normalize(b.innerText || b.textContent || b.value || '');
  return t === 'continue' || t === '继续' || t === '确认' || t === 'next' || t === '下一步';
}});
if (contBtn) {{
  // Ensure any visible user_code field is filled first.
  let codeInput = document.querySelector('input[name="user_code"], input[autocomplete="one-time-code"]');
  if (!codeInput) {{
    codeInput = Array.from(document.querySelectorAll('input')).find((el) => {{
      const id = (el.id || '').toLowerCase();
      const name = (el.name || '').toLowerCase();
      return id.includes('code') || name.includes('code');
    }}) || null;
  }}
  if (codeInput && userCode) {{
    codeInput.focus();
    codeInput.value = userCode;
    codeInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
    codeInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
  }}
  contBtn.click();
  return {{ ok: true, via: 'page_continue_click' }};
}}
return {{ ok: false, reason: 'no_continue_control' }};
"""
        try:
            result = page.run_js(js)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] browser Continue JS failed: {exc}")
            return False

        if isinstance(result, dict) and result.get("reason") == "already_on_consent":
            if log_callback:
                log_callback("[*] Device Flow: already on consent (skip Continue)")
            return True
        if not isinstance(result, dict) or not result.get("ok"):
            if log_callback:
                reason = result.get("reason") if isinstance(result, dict) else result
                log_callback(f"[Debug] browser Continue missed: {reason}")
            return False

        if log_callback:
            log_callback(
                f"[*] Device Flow: Continue submitted ({result.get('via') or 'ok'})"
            )
        # Wait until we leave the pure user-code page.
        phase = self._wait_browser_phase(
            page,
            {"consent", "done"},
            timeout=15.0,
            log_callback=log_callback,
            label="after Continue",
        )
        return phase in ("consent", "done")

    def _browser_submit_allow(
        self,
        page,
        user_code: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> bool:
        """Approve only on the consent form (Deny + Allow). Never on user-code page.

        Real form (auth.x.ai):
          <form action=".../oauth2/device/approve" method="POST">
            <input name="user_code" value="XXXX-XXXX">
            <input name="action" value="">
            <button type="submit">Deny</button>
            <button type="submit">Allow</button>
          </form>
        """
        phase = self._wait_browser_phase(
            page,
            {"consent", "done"},
            timeout=12.0,
            log_callback=log_callback,
            label="consent for Allow",
        )
        if phase == "done":
            return True
        if phase != "consent":
            if log_callback:
                log_callback(
                    f"[Debug] Allow refused: not on consent (phase={phase})"
                )
            return False

        user_code_js = json.dumps(str(user_code or ""))
        js = f"""
const userCode = {user_code_js};
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();

// Hard guard: never act on verify/Continue form.
const verifyForm = document.querySelector('form[action*="device/verify"]');
const approveForm = document.querySelector('form[action*="device/approve"]');
if (!approveForm) {{
  // If only verify form exists, we are still on the previous page.
  if (verifyForm) return {{ ok: false, reason: 'still_on_user_code' }};
}}

const forms = Array.from(document.querySelectorAll('form'));
let form = forms.find((f) => {{
  const action = (f.getAttribute('action') || '');
  return action.includes('device/approve');
}}) || null;

// Fallback: form that has both Allow and Deny submits.
if (!form) {{
  form = forms.find((f) => {{
    const texts = Array.from(f.querySelectorAll('button, input[type="submit"]')).map(
      (b) => normalize(b.innerText || b.textContent || b.value || '')
    );
    const hasAllow = texts.some((t) => t === 'allow' || t === '允许');
    const hasDeny = texts.some((t) => t === 'deny' || t === '拒绝');
    return hasAllow && hasDeny;
  }}) || null;
}}
if (!form) {{
  return {{ ok: false, reason: 'no_approve_form' }};
}}

const setField = (name, value) => {{
  let input = form.querySelector('input[name="' + name + '"]');
  if (!input) {{
    input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    form.appendChild(input);
  }}
  input.value = value;
}};
if (userCode) setField('user_code', userCode);
// Critical: real form ships action="" ; first submit is Deny.
setField('action', 'allow');
setField('principal_type', 'User');
if (!form.querySelector('input[name="principal_id"]')) {{
  setField('principal_id', '');
}}

const buttons = Array.from(form.querySelectorAll('button, input[type="submit"]'));
const allowBtn = buttons.find((b) => {{
  const text = normalize(b.innerText || b.textContent || b.value || '');
  return text === 'allow' || text === '允许' || text === 'approve';
}});
if (allowBtn) {{
  allowBtn.click();
  return {{ ok: true, via: 'allow_click' }};
}}
// Never click the first submit (Deny). Submit after action=allow.
form.submit();
return {{ ok: true, via: 'form_submit' }};
"""
        try:
            result = page.run_js(js)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] browser Allow JS failed: {exc}")
            result = None

        if not isinstance(result, dict) or not result.get("ok"):
            if log_callback:
                reason = result.get("reason") if isinstance(result, dict) else result
                log_callback(f"[Debug] browser Allow missed: {reason}")
            return False

        if log_callback:
            log_callback(f"[*] Device Flow: Allow submitted ({result.get('via') or 'ok'})")

        wait_deadline = time.time() + 15.0
        while time.time() < wait_deadline:
            if self._page_is_device_done(page):
                if log_callback:
                    log_callback("[*] Device Flow: browser reached done")
                return True
            url = self._page_url(page).lower()
            if "sign-in" in url or "sign-up" in url:
                if log_callback:
                    log_callback(f"[Debug] browser Allow bounced to {url[:100]}")
                return False
            # Still on user-code means we never left Continue page — fail.
            phase_now = self._browser_page_phase(page)
            if phase_now == "user_code":
                if log_callback:
                    log_callback("[Debug] browser Allow still on user-code page")
                return False
            time.sleep(0.4)

        if self._page_is_device_done(page):
            return True
        if log_callback:
            log_callback(
                f"[Debug] browser Allow did not reach done "
                f"(phase={self._browser_page_phase(page)}, url={self._page_url(page)[:100]})"
            )
        return False

    # ── low-level HTTP with manual redirects ───────────────────────────

    def _do(
        self,
        method: str,
        endpoint: str,
        form: dict | None,
        *,
        api_client: bool = False,
    ) -> tuple[int, str, bytes]:
        if not safe_xai_url(endpoint):
            raise SSOBuildError(f"unsafe xAI OAuth URL: {endpoint}")
        current_url = endpoint
        current_method = method.upper()
        current_form = form
        for _redirect in range(9):
            if api_client:
                # grok-build device_code.rs: Accept JSON + x-grok-client-* headers
                headers = {
                    "Accept": "application/json",
                    "User-Agent": self.user_agent,
                    "x-grok-client-version": SSO_BUILD_CLIENT_VERSION,
                    "x-grok-client-surface": self.client_surface,
                }
            else:
                headers = {
                    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "User-Agent": self.user_agent,
                }
            cookie = self.cookies.header()
            if cookie:
                headers["Cookie"] = cookie
            data = None
            if current_form is not None:
                data = urlencode(current_form)
                headers["Content-Type"] = "application/x-www-form-urlencoded"

            response = self._request(current_method, current_url, headers=headers, data=data)
            self.cookies.capture_from_response(response)
            status = int(getattr(response, "status_code", 0) or 0)
            try:
                body = response.content if hasattr(response, "content") else (response.text or "").encode("utf-8")
            except Exception:
                body = b""
            if len(body) > MAX_BODY:
                raise SSOBuildError("xAI OAuth response > 2 MiB", status=status)

            if status < 300 or status > 399:
                return status, current_url, body

            location = str((getattr(response, "headers", {}) or {}).get("Location") or "").strip()
            if not location:
                raise SSOBuildError(f"xAI OAuth redirect missing Location (HTTP {status})", status=status)
            next_url = urljoin(current_url, location)
            if not safe_xai_url(next_url):
                raise SSOBuildError(f"xAI OAuth redirected to untrusted host: {next_url}", status=status)
            # Follow redirects as a browser would for form POSTs (switch to GET).
            # accounts.x.ai often returns 307 to /sign-in when SSO is missing.
            if status in (301, 302, 303, 307, 308) and current_method not in ("GET", "HEAD"):
                current_method = "GET"
                current_form = None
            current_url = next_url
        raise SSOBuildError("xAI OAuth too many redirects")

    def _request(self, method: str, url: str, *, headers: dict, data: str | None):
        kwargs = {
            "headers": headers,
            "data": data,
            "timeout": self.timeout,
            "proxies": self.proxies or {},
            "allow_redirects": False,
            "verify": True,
        }
        try:
            kwargs_curl = dict(kwargs)
            kwargs_curl["impersonate"] = self.impersonate
            if method == "GET":
                return curl_requests.get(url, **kwargs_curl)
            return curl_requests.post(url, **kwargs_curl)
        except Exception:
            # Fallback: std requests (no impersonate)
            std_kwargs = {
                "headers": headers,
                "data": data,
                "timeout": self.timeout,
                "proxies": self.proxies or {},
                "allow_redirects": False,
                "verify": True,
            }
            if method == "GET":
                return std_requests.get(url, **std_kwargs)
            return std_requests.post(url, **std_kwargs)


def convert_sso_to_build(
    sso_token: str,
    *,
    email: str = "",
    name: str = "",
    user_agent: str = DEFAULT_USER_AGENT,
    proxies: dict | None = None,
    page=None,
    mode: str = "auto",
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Convert Web SSO → Build OAuth seed dict.

    mode:
      - auto    : browser if page given, else http
      - http    : pure HTTP with SSO cookie auto verify/approve
      - browser : require page for verify/approve
    """
    flow = SSOBuildFlow(
        sso_token,
        user_agent=user_agent,
        proxies=proxies,
    )
    mode = str(mode or "auto").strip().lower()
    if mode == "browser":
        if page is None:
            raise SSOBuildError("browser mode requires an active page")
        return flow.convert_with_browser(page, email=email, name=name, log_callback=log_callback)
    if mode == "auto" and page is not None:
        try:
            return flow.convert_with_browser(page, email=email, name=name, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] browser Device Flow failed, HTTP fallback: {exc}")
            return flow.convert(email=email, name=name)
    return flow.convert(email=email, name=name)


def build_grok2api_import_document(seed: dict[str, Any]) -> dict[str, Any]:
    """chenyme/grok2api Build import document ({accounts: [...]})."""
    return {
        "accounts": [
            {
                "provider": "grok_build",
                "name": seed.get("name") or seed.get("email") or "Grok Build account",
                "client_id": seed.get("client_id") or SSO_BUILD_CLIENT_ID,
                "access_token": seed.get("access_token") or "",
                "refresh_token": seed.get("refresh_token") or "",
                "id_token": seed.get("id_token") or "",
                "token_type": "Bearer",
                "scope": seed.get("scope") or SSO_BUILD_SCOPE,
                "expires_at": seed.get("expires_at") or "",
                "expires_in": int(seed.get("expires_in") or 0),
                "email": seed.get("email") or "",
                "user_id": seed.get("user_id") or "",
                "team_id": seed.get("team_id") or "",
            }
        ]
    }


def save_build_auth(
    seed: dict[str, Any],
    directory: str | Path,
    *,
    email: str = "",
) -> Path:
    """Persist one Build OAuth account as grok2api-compatible import JSON."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    label = first_value(email, seed.get("email"), seed.get("user_id"), "build-auth")
    safe = re.sub(r"[^\w.@+-]+", "_", label)[:80] or "build-auth"
    path = directory / f"{safe}.json"
    doc = build_grok2api_import_document(seed)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
