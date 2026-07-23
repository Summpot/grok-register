"""Local Grok Web SSO → Build OAuth conversion via xAI Device Flow.

Aligned with grok-build (`xai-grok-shell` `auth/device_code.rs` +
`auth/config.rs` default OAuth2 client contract).

Browser-only (HTTP auto verify/approve no longer works against the live IdP):

  1. Validate SSO cookie against accounts.x.ai (page.request / context.request)
  2. POST auth.x.ai/oauth2/device/code  (Grok CLI client_id + scopes + referrer)
  3. Open verification_uri_complete in the registration browser; Continue + Allow
  4. Poll oauth2/token for access_token + refresh_token (page.request)

Device/token HTTP always uses the registration browser's Playwright
APIRequestContext (``page.request`` or ``context.request``) so TLS and
proxy match the browser egress — no separate curl_cffi / socks tunnel.
"""

from __future__ import annotations

import base64
import json
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

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
SSO_TOKEN_URL = f"{SSO_ISSUER}/oauth2/token"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
MAX_BODY = 2 << 20
# device_code.rs: DEFAULT_DEVICE_POLL_INTERVAL_SECS / DEVICE_SLOW_DOWN_INCREMENT
DEFAULT_DEVICE_POLL_INTERVAL_SECS = 5
DEVICE_SLOW_DOWN_INCREMENT_SECS = 5
# device_code.rs: MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS (floor poll window)
MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS = 10 * 60
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# POST /oauth2/device/code rate-limit backoff (HTTP 429 / error=slow_down).
DEVICE_START_MAX_ATTEMPTS = 8
DEVICE_START_BACKOFF_BASE_SECS = float(DEVICE_SLOW_DOWN_INCREMENT_SECS)
DEVICE_START_BACKOFF_MAX_SECS = 60.0
DEVICE_START_BACKOFF_JITTER_SECS = 1.5
# Serialize minting across concurrent workers in this process.
DEVICE_START_MIN_GAP_SECS = 2.0
_device_start_lock = threading.Lock()
_device_start_next_ok_at = 0.0

# Browser verify (Continue) rate-limit: IdP returns
#   /oauth2/device?error=rate_limited
# and clears the user_code input — must backoff, re-type code, Continue again.
DEVICE_VERIFY_MAX_ATTEMPTS = 6
DEVICE_VERIFY_BACKOFF_BASE_SECS = 8.0
DEVICE_VERIFY_BACKOFF_MAX_SECS = 60.0


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
            if jar is not None:
                # curl_cffi / requests cookie jar (tests / legacy adapters)
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
        # Parse Set-Cookie headers (Playwright headers_array + dict backends)
        try:
            raw_list: list[str] = []
            arr = getattr(response, "headers_array", None)
            if callable(arr):
                try:
                    arr = arr()
                except Exception:
                    arr = None
            if not arr:
                arr = getattr(response, "_headers_array", None) or []
            for item in arr or []:
                try:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "")
                        value = str(item.get("value") or "")
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        name, value = str(item[0]), str(item[1])
                    else:
                        continue
                    if name.lower() == "set-cookie" and value:
                        raw_list.append(value)
                except Exception:
                    continue
            headers = getattr(response, "headers", None) or {}
            if hasattr(headers, "getlist"):
                raw_list.extend(
                    headers.getlist("Set-Cookie") or headers.getlist("set-cookie") or []
                )
            if not raw_list:
                single = headers.get("Set-Cookie") or headers.get("set-cookie")
                if single:
                    raw_list = [single]
            for raw in raw_list:
                part = str(raw).split(";", 1)[0]
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                low = str(raw).lower()
                if "max-age=0" in low or "expires=thu, 01 jan 1970" in low:
                    self.delete(name)
                else:
                    self.set(name, value)
        except Exception:
            pass


class _PlaywrightHttpResponse:
    """Normalize Playwright APIResponse to the shape expected by ``_do`` / cookie jar."""

    def __init__(self, api_response: Any):
        self._raw = api_response
        self.status_code = int(getattr(api_response, "status", 0) or 0)
        try:
            body = api_response.body()
        except Exception:
            body = b""
        if isinstance(body, str):
            body = body.encode("utf-8", errors="replace")
        elif not isinstance(body, (bytes, bytearray)):
            body = bytes(body or b"")
        self.content = bytes(body)
        try:
            self.text = self.content.decode("utf-8", errors="replace")
        except Exception:
            self.text = ""
        try:
            self.url = str(getattr(api_response, "url", "") or "")
        except Exception:
            self.url = ""
        try:
            self.headers = dict(getattr(api_response, "headers", None) or {})
        except Exception:
            self.headers = {}
        # Preserve multi Set-Cookie entries (dict headers collapse duplicates).
        arr: list[Any] = []
        try:
            fn = getattr(api_response, "headers_array", None)
            if callable(fn):
                arr = list(fn() or [])
        except Exception:
            arr = []
        self._headers_array = arr
        self.cookies: dict[str, str] = {}

    def headers_array(self) -> list[Any]:
        return list(self._headers_array or [])


class SSOBuildFlow:
    """Browser Device Flow converter aligned with grok-build device_code.rs.

    Device code mint + token poll use Playwright ``page.request`` /
    ``context.request`` (same proxy/TLS as the registration browser);
    verify/approve require a live page (pure HTTP SSO auto-approve is no
    longer accepted by the IdP).
    """

    def __init__(
        self,
        sso_token: str,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        proxies: dict | None = None,
        impersonate: str | None = None,
        timeout: float = 30.0,
        client_surface: str = SSO_BUILD_CLIENT_SURFACE,
    ):
        token = normalize_sso_token(sso_token)
        if not token:
            raise SSOBuildError("SSO token empty", unauthorized=True)
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        # Kept for call-site / logging compatibility; HTTP no longer tunnels via this.
        self.proxies = proxies or {}
        _ = impersonate  # deprecated: was curl_cffi chrome profile
        self.timeout = timeout
        self.client_surface = (client_surface or SSO_BUILD_CLIENT_SURFACE).strip() or "cli"
        self.cookies = _CookieJar({"sso": token, "sso-rw": token})
        self._request_page: Any = None

    def convert_with_browser(
        self,
        page,
        *,
        email: str = "",
        name: str = "",
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Browser for verify/approve; device/token via page.request."""
        if page is None:
            raise SSOBuildError("browser Device Flow requires an active page")

        self._request_page = page
        try:
            return self._convert_with_browser_bound(
                page, email=email, name=name, log_callback=log_callback
            )
        finally:
            self._request_page = None

    def _convert_with_browser_bound(
        self,
        page,
        *,
        email: str = "",
        name: str = "",
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self._ensure_browser_sso_cookies(page)
        status, final_url, _body = self._do("GET", SSO_ACCOUNTS_URL, None)
        if status == 401 or url_is_auth_bounce(final_url):
            raise SSOBuildError("Grok Web SSO rejected", status=status, unauthorized=True)
        if status < 200 or status >= 400:
            raise SSOBuildError(f"validate SSO failed HTTP {status}", status=status)

        device = self._start_device(log_callback=log_callback)
        verify_url = device.get("verification_uri_complete") or ""
        if not safe_xai_url(verify_url):
            raise SSOBuildError("device flow verification URL incomplete")

        if log_callback:
            log_callback(f"[*] Device Flow: browser open verify ({device.get('user_code')})")
        try:
            page.get(verify_url, timeout=45)
        except Exception as exc:
            raise SSOBuildError(f"browser open verify failed: {exc}") from exc

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
                phase = self._browser_page_phase(page)
                url = self._page_url(page)
                diag = self._log_browser_diag(
                    page,
                    expected_user_code=str(device.get("user_code") or ""),
                    log_callback=log_callback,
                    label="final failure",
                    extra={"user_code": device.get("user_code"), "phase": phase},
                )
                err_bits = []
                errors = diag.get("errors") if isinstance(diag.get("errors"), list) else []
                if errors:
                    err_bits.append(f"page_errors={errors[:3]!r}")
                code = diag.get("code_input") if isinstance(diag.get("code_input"), dict) else None
                if code is not None:
                    err_bits.append(
                        f"code_match={code.get('matchesExpected')} "
                        f"code_len={code.get('valueLen')}"
                    )
                detail = (", " + ", ".join(err_bits)) if err_bits else ""
                raise SSOBuildError(
                    f"browser Device Flow steps failed "
                    f"(phase={phase}, url={url[:160]}{detail})"
                )

        if log_callback:
            log_callback("[*] Device Flow: polling OAuth token (page.request)")
        token = self._poll_token(
            device["device_code"],
            interval=float(device.get("interval") or DEFAULT_DEVICE_POLL_INTERVAL_SECS),
            expires_in=float(device.get("expires_in") or MIN_DEVICE_CODE_EXPIRY_FALLBACK_SECS),
        )
        return self._seed_from_token(token, email=email, name=name)

    # ── steps ──────────────────────────────────────────────────────────

    @staticmethod
    def _decode_body_text(body: Any, *, limit: int = 300) -> str:
        try:
            text = (
                body.decode("utf-8", errors="replace")
                if isinstance(body, (bytes, bytearray))
                else str(body or "")
            )
        except Exception:
            return ""
        return text[:limit] if limit > 0 else text

    @classmethod
    def _is_device_start_rate_limited(cls, status: int, body: Any) -> bool:
        """True for HTTP 429 / provider slow_down on device-code mint."""
        if int(status or 0) == 429:
            return True
        text = cls._decode_body_text(body, limit=800).lower()
        if not text:
            return False
        if "slow_down" in text or "too many device code" in text:
            return True
        try:
            payload = json.loads(
                body.decode("utf-8", errors="replace")
                if isinstance(body, (bytes, bytearray))
                else body
            )
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        err = str(payload.get("error") or "").strip().lower()
        return err in ("slow_down", "rate_limit", "rate_limited", "too_many_requests")

    @staticmethod
    def _device_start_backoff_secs(attempt: int) -> float:
        """Exponential backoff with jitter: base * 2^(attempt-1), capped."""
        attempt = max(int(attempt), 1)
        base = float(DEVICE_START_BACKOFF_BASE_SECS)
        delay = base * (2 ** (attempt - 1))
        delay = min(delay, float(DEVICE_START_BACKOFF_MAX_SECS))
        jitter = random.uniform(0.0, float(DEVICE_START_BACKOFF_JITTER_SECS))
        return delay + jitter

    def _wait_device_start_slot(
        self,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Respect process-wide min gap between device-code mints."""
        global _device_start_next_ok_at
        now = time.time()
        wait = float(_device_start_next_ok_at) - now
        if wait > 0.05:
            if log_callback:
                log_callback(
                    f"[*] Device Flow: pacing device-code request "
                    f"({wait:.1f}s min-gap)"
                )
            time.sleep(wait)

    def _mark_device_start_ok(self, *, extra_gap: float = 0.0) -> None:
        global _device_start_next_ok_at
        gap = max(float(DEVICE_START_MIN_GAP_SECS), float(extra_gap or 0.0))
        _device_start_next_ok_at = time.time() + gap

    def _mark_device_start_rate_limited(self, wait_secs: float) -> None:
        """Push next allowed mint past the backoff window for all workers."""
        global _device_start_next_ok_at
        _device_start_next_ok_at = max(
            float(_device_start_next_ok_at),
            time.time() + max(float(wait_secs), float(DEVICE_START_MIN_GAP_SECS)),
        )

    def _start_device(
        self,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Mint device_code with exponential backoff on 429 / slow_down.

        Concurrent workers share a process-wide lock + min gap so parallel
        registrations do not stampede auth.x.ai/oauth2/device/code.
        """
        # Matches grok-build request_device_code form + client headers.
        form = {
            "client_id": SSO_BUILD_CLIENT_ID,
            "scope": SSO_BUILD_SCOPE,
            "referrer": SSO_BUILD_REFERRER,
        }
        max_attempts = max(int(DEVICE_START_MAX_ATTEMPTS), 1)
        last_status = 0
        last_snippet = ""

        with _device_start_lock:
            for attempt in range(1, max_attempts + 1):
                self._wait_device_start_slot(log_callback=log_callback)
                status, _url, body = self._do(
                    "POST",
                    SSO_DEVICE_URL,
                    form,
                    api_client=True,
                )
                last_status = int(status or 0)

                if 200 <= last_status < 300:
                    self._mark_device_start_ok()
                    return self._parse_device_start_payload(body)

                if last_status == 404:
                    raise SSOBuildError(
                        "Device-code login is not available for this deployment (HTTP 404)",
                        status=last_status,
                    )

                last_snippet = self._decode_body_text(body, limit=300)
                if self._is_device_start_rate_limited(last_status, body):
                    if attempt >= max_attempts:
                        break
                    wait = self._device_start_backoff_secs(attempt)
                    # Hold other workers off for this window…
                    self._mark_device_start_rate_limited(wait)
                    if log_callback:
                        log_callback(
                            f"[*] Device Flow: rate limited "
                            f"(HTTP {last_status}/slow_down), "
                            f"backoff {wait:.1f}s "
                            f"(attempt {attempt}/{max_attempts})"
                        )
                    time.sleep(wait)
                    # …then clear our own slot: we already paid the wait
                    # (also avoids double-wait when time.sleep is mocked).
                    global _device_start_next_ok_at
                    _device_start_next_ok_at = time.time()
                    continue

                raise SSOBuildError(
                    f"start Device Flow failed HTTP {last_status}: {last_snippet}".rstrip(
                        ": "
                    ),
                    status=last_status,
                )

        raise SSOBuildError(
            f"start Device Flow failed HTTP {last_status}: {last_snippet}".rstrip(": ")
            + f" (exhausted {max_attempts} attempts with backoff)",
            status=last_status or 429,
        )

    def _parse_device_start_payload(self, body: Any) -> dict[str, Any]:
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

    def _collect_browser_diag(
        self,
        page,
        *,
        expected_user_code: str = "",
    ) -> dict[str, Any]:
        """Snapshot Device Flow page state for Continue/Allow failure diagnosis."""
        diag: dict[str, Any] = {
            "url": self._page_url(page),
            "phase": "unknown",
            "ready_state": "",
            "title": "",
            "forms": [],
            "buttons": [],
            "code_input": None,
            "errors": [],
            "body_snippet": "",
            "js_error": "",
        }
        try:
            diag["phase"] = self._browser_page_phase(page)
        except Exception as exc:
            diag["phase_error"] = str(exc)

        expected_js = json.dumps(str(expected_user_code or ""))
        js = f"""
const expected = {expected_js};
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
const out = {{
  readyState: document.readyState || '',
  title: document.title || '',
  url: location.href || '',
  forms: [],
  buttons: [],
  codeInput: null,
  errors: [],
  bodySnippet: '',
}};

try {{
  out.forms = Array.from(document.querySelectorAll('form')).slice(0, 6).map((f, i) => {{
    const inputs = Array.from(f.querySelectorAll('input, button, select, textarea')).slice(0, 12).map((el) => {{
      const tag = (el.tagName || '').toLowerCase();
      const type = (el.getAttribute('type') || el.type || tag || '').toLowerCase();
      const name = el.getAttribute('name') || el.name || '';
      let value = '';
      try {{ value = String(el.value != null ? el.value : ''); }} catch (e) {{ value = ''; }}
      if (name === 'user_code' && value.length > 4) {{
        value = value.slice(0, 2) + '…' + value.slice(-2) + `(len=${{value.length}})`;
      }} else if (value.length > 40) {{
        value = value.slice(0, 40) + '…';
      }}
      const text = normalize(el.innerText || el.textContent || el.value || '').slice(0, 40);
      return {{
        tag, type, name, value, text,
        disabled: !!(el.disabled || el.getAttribute('disabled') != null),
        visible: !!(el.offsetParent !== null || (el.getClientRects && el.getClientRects().length)),
      }};
    }});
    return {{
      i,
      action: f.getAttribute('action') || '',
      method: (f.getAttribute('method') || 'get').toLowerCase(),
      id: f.id || '',
      inputs,
    }};
  }});

  out.buttons = Array.from(
    document.querySelectorAll('button, input[type="submit"], a[role="button"]')
  ).slice(0, 12).map((b) => {{
    const text = normalize(b.innerText || b.textContent || b.value || '').slice(0, 48);
    return {{
      text,
      type: (b.getAttribute('type') || b.type || '').toLowerCase(),
      disabled: !!(b.disabled || b.getAttribute('disabled') != null || b.getAttribute('aria-disabled') === 'true'),
      ariaBusy: b.getAttribute('aria-busy') || '',
      visible: !!(b.offsetParent !== null || (b.getClientRects && b.getClientRects().length)),
    }};
  }});

  let codeInput = document.querySelector(
    'input[name="user_code"], input[autocomplete="one-time-code"]'
  );
  if (!codeInput) {{
    codeInput = Array.from(document.querySelectorAll('input')).find((el) => {{
      const id = (el.id || '').toLowerCase();
      const name = (el.name || '').toLowerCase();
      const ph = (el.placeholder || '').toLowerCase();
      return id.includes('code') || name.includes('code') || ph.includes('code') || ph.includes('device');
    }}) || null;
  }}
  if (codeInput) {{
    let val = '';
    try {{ val = String(codeInput.value || ''); }} catch (e) {{ val = ''; }}
    const match = expected ? (normalize(val).toUpperCase() === normalize(expected).toUpperCase()) : null;
    out.codeInput = {{
      name: codeInput.name || codeInput.getAttribute('name') || '',
      id: codeInput.id || '',
      valueLen: val.length,
      valuePreview: val ? (val.slice(0, 2) + '…' + val.slice(-2)) : '',
      matchesExpected: match,
      disabled: !!codeInput.disabled,
      readOnly: !!codeInput.readOnly,
    }};
  }}

  const errNodes = Array.from(document.querySelectorAll(
    '[role="alert"], [aria-live], .text-destructive, .text-danger, .text-red-500, ' +
    'p.text-muted, p[class*="danger"], p[class*="error"], [data-testid*="error"]'
  )).slice(0, 8);
  out.errors = errNodes.map((el) => normalize(el.innerText || el.textContent || '')).filter(Boolean).slice(0, 8);

  // Also pick short non-empty paragraphs near the form (validation hints).
  const near = Array.from(document.querySelectorAll('form p, form span, form div[class*="error"]'))
    .map((el) => normalize(el.innerText || el.textContent || ''))
    .filter((t) => t && t.length < 120)
    .slice(0, 6);
  for (const t of near) {{
    if (!out.errors.includes(t)) out.errors.push(t);
  }}
  out.errors = out.errors.slice(0, 8);

  try {{
    const bodyText = normalize(document.body && document.body.innerText ? document.body.innerText : '');
    out.bodySnippet = bodyText.slice(0, 220);
  }} catch (e) {{
    out.bodySnippet = '';
  }}
}} catch (e) {{
  out.jsError = String(e && e.message ? e.message : e);
}}
return out;
"""
        try:
            raw = page.run_js(js)
        except Exception as exc:
            diag["js_error"] = str(exc)
            return diag

        if not isinstance(raw, dict):
            diag["js_error"] = f"unexpected diag payload type={type(raw).__name__}"
            return diag

        diag["ready_state"] = str(raw.get("readyState") or "")
        diag["title"] = str(raw.get("title") or "")[:120]
        if raw.get("url"):
            diag["url"] = str(raw.get("url") or diag["url"])
        diag["forms"] = raw.get("forms") if isinstance(raw.get("forms"), list) else []
        diag["buttons"] = raw.get("buttons") if isinstance(raw.get("buttons"), list) else []
        diag["code_input"] = raw.get("codeInput") if isinstance(raw.get("codeInput"), dict) else None
        diag["errors"] = raw.get("errors") if isinstance(raw.get("errors"), list) else []
        diag["body_snippet"] = str(raw.get("bodySnippet") or "")[:220]
        if raw.get("jsError"):
            diag["js_error"] = str(raw.get("jsError"))
        return diag

    def _format_browser_diag(self, diag: dict[str, Any], *, label: str = "") -> str:
        """One-line-friendly multi-line debug string for logs."""
        parts: list[str] = []
        head = "[Debug] Device Flow diag"
        if label:
            head += f" ({label})"
        parts.append(head)
        parts.append(
            f"  phase={diag.get('phase') or '?'} ready={diag.get('ready_state') or '?'} "
            f"title={str(diag.get('title') or '')[:60]!r}"
        )
        parts.append(f"  url={str(diag.get('url') or '')[:180]}")
        if diag.get("js_error"):
            parts.append(f"  js_error={diag.get('js_error')}")
        code = diag.get("code_input")
        if isinstance(code, dict):
            parts.append(
                "  code_input="
                f"name={code.get('name')!r} len={code.get('valueLen')} "
                f"preview={code.get('valuePreview')!r} "
                f"match_expected={code.get('matchesExpected')} "
                f"disabled={code.get('disabled')} readonly={code.get('readOnly')}"
            )
        else:
            parts.append("  code_input=<none>")
        forms = diag.get("forms") if isinstance(diag.get("forms"), list) else []
        parts.append(f"  forms={len(forms)}")
        for form in forms[:4]:
            if not isinstance(form, dict):
                continue
            inputs = form.get("inputs") if isinstance(form.get("inputs"), list) else []
            names = []
            for inp in inputs[:8]:
                if not isinstance(inp, dict):
                    continue
                names.append(
                    f"{inp.get('name') or inp.get('tag') or '?'}:"
                    f"{inp.get('type') or ''}{'!' if inp.get('disabled') else ''}"
                )
            parts.append(
                f"    form[{form.get('i')}] method={form.get('method')} "
                f"action={str(form.get('action') or '')[:100]!r} "
                f"fields=[{', '.join(names)}]"
            )
        buttons = diag.get("buttons") if isinstance(diag.get("buttons"), list) else []
        if buttons:
            btn_bits = []
            for b in buttons[:8]:
                if not isinstance(b, dict):
                    continue
                flags = []
                if b.get("disabled"):
                    flags.append("disabled")
                if not b.get("visible"):
                    flags.append("hidden")
                if b.get("ariaBusy"):
                    flags.append(f"busy={b.get('ariaBusy')}")
                flag_s = f"({','.join(flags)})" if flags else ""
                btn_bits.append(f"{str(b.get('text') or '')[:24]!r}{flag_s}")
            parts.append(f"  buttons=[{', '.join(btn_bits)}]")
        errors = diag.get("errors") if isinstance(diag.get("errors"), list) else []
        if errors:
            parts.append(f"  errors={errors[:5]!r}")
        snippet = str(diag.get("body_snippet") or "").strip()
        if snippet:
            parts.append(f"  body≈{snippet[:160]!r}")
        return "\n".join(parts)

    def _log_browser_diag(
        self,
        page,
        *,
        expected_user_code: str = "",
        log_callback: Callable[[str], None] | None = None,
        label: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        diag = self._collect_browser_diag(page, expected_user_code=expected_user_code)
        if extra:
            diag = {**diag, **{f"extra_{k}": v for k, v in extra.items()}}
        if log_callback:
            msg = self._format_browser_diag(diag, label=label)
            if extra:
                extra_bits = " ".join(f"{k}={v!r}" for k, v in extra.items())
                msg += f"\n  extra: {extra_bits}"
            # Cookie presence (session jar, not browser) helps SSO bounce cases.
            jar = self.cookies.as_dict()
            has_sso = bool(jar.get("sso") or jar.get("sso-rw"))
            msg += f"\n  http_cookie_jar: sso={has_sso} keys={sorted(jar.keys())[:12]}"
            log_callback(msg)
        return diag

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

    def _browser_page_phase(self, page) -> str:
        """Classify current Device Flow UI.

        Returns: done | consent | user_code | error | unknown
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
const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"], a'));
const texts = buttons.map((b) => normalize(b.innerText || b.textContent || b.value || ''));
const hasRetry = texts.some((t) => t === 'retry' || t === '重试' || t === 'try again' || t === '再试一次');
let bodyText = '';
try {
  bodyText = normalize(document.body && document.body.innerText ? document.body.innerText : '');
} catch (e) {
  bodyText = '';
}
const looksError = /error loading this page|an error occurred|there was an error|error occurred|出错|加载失败|加载.*错误/.test(bodyText)
  || Array.from(document.querySelectorAll('[role="alert"], .text-destructive, .text-danger, [data-testid*="error"]'))
      .some((el) => normalize(el.innerText || el.textContent || '').length > 0);
// Transient provider error page: Retry only (no approve/verify form).
if (hasRetry && (looksError || forms.length === 0)) return 'error';

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

const hasAllow = texts.some((t) => t === 'allow' || t === '允许');
const hasDeny = texts.some((t) => t === 'deny' || t === '拒绝' || t === '取消');
const hasContinue = texts.some((t) => t === 'continue' || t === '继续' || t === '确认');

const heading = Array.from(document.querySelectorAll('h1,h2')).map(
  (el) => normalize(el.textContent || '')
).join(' | ');
if (/authorize grok build|授权/.test(heading) && hasAllow) return 'consent';
if (hasAllow && hasDeny) return 'consent';
if (hasContinue && !hasAllow) return 'user_code';
// Path /oauth2/device/consent?user_code=… is consent, not the code-entry page.
if (url.includes('/device/consent') || url.includes('device/approve')) return 'consent';
// Current provider hosts the code page at accounts.x.ai/oauth2/device?user_code=…
if (
  url.includes('device/verify')
  || url.includes('/oauth2/device/user_code')
  || /\\/oauth2\\/device\\/?(\\?|$)/.test(url)
  || (url.includes('user_code') && !url.includes('consent'))
) {
  return 'user_code';
}
if (url.includes('consent')) return 'consent';

// user_code input present but no Allow pair → still on code page
const codeInput = document.querySelector('input[name="user_code"], input[id*="user"], input[autocomplete="one-time-code"]');
if (codeInput && !hasAllow) return 'user_code';
return 'unknown';
"""
            )
        except Exception:
            phase = "unknown"
        phase = str(phase or "unknown").strip().lower()
        if phase in ("done", "consent", "user_code", "error", "unknown"):
            return phase
        return "unknown"

    def _browser_click_retry(
        self,
        page,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> bool:
        """Click Retry / 重试 on a transient Device Flow error page."""
        js = """
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const isRetry = (t) => t === 'retry' || t === '重试' || t === 'try again' || t === '再试一次';
const nodes = Array.from(document.querySelectorAll(
  'button, input[type="submit"], input[type="button"], a[role="button"], a'
));
const visible = (el) => {
  try {
    if (el.disabled || el.getAttribute('disabled') != null || el.getAttribute('aria-disabled') === 'true') {
      return false;
    }
    return !!(el.offsetParent !== null || (el.getClientRects && el.getClientRects().length));
  } catch (e) {
    return true;
  }
};
let btn = nodes.find((b) => {
  const text = normalize(b.innerText || b.textContent || b.value || '');
  return isRetry(text) && visible(b);
});
if (!btn) {
  btn = nodes.find((b) => {
    const text = normalize(b.innerText || b.textContent || b.value || '');
    return isRetry(text);
  });
}
if (!btn) {
  return { ok: false, reason: 'no_retry_button' };
}
btn.click();
return {
  ok: true,
  via: 'retry_click',
  text: normalize(btn.innerText || btn.textContent || btn.value || '').slice(0, 40),
  url: location.href || '',
};
"""
        try:
            result = page.run_js(js)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] browser Retry JS failed: {exc}")
            return False
        if not isinstance(result, dict) or not result.get("ok"):
            if log_callback:
                reason = result.get("reason") if isinstance(result, dict) else result
                log_callback(f"[Debug] browser Retry missed: {reason}")
            return False
        if log_callback:
            log_callback(
                f"[*] Device Flow: error page → Retry "
                f"({result.get('via') or 'ok'}, text={result.get('text')!r})"
            )
        # Give the SPA a moment to re-fetch after Retry.
        time.sleep(0.6)
        return True

    def _browser_recover_error_page(
        self,
        page,
        *,
        log_callback: Callable[[str], None] | None = None,
        max_clicks: int = 3,
        wait_after: float = 12.0,
        wanted: set[str] | frozenset[str] | None = None,
    ) -> str:
        """If on error phase, click Retry until recovered or budget exhausted.

        Returns the phase after recovery attempts (may still be 'error').
        """
        want = set(wanted or {"user_code", "consent", "done"})
        phase = self._browser_page_phase(page)
        if phase != "error":
            return phase
        for i in range(max(1, max_clicks)):
            if log_callback:
                log_callback(
                    f"[*] Device Flow: page error → Retry "
                    f"(attempt {i + 1}/{max_clicks}, url={self._page_url(page)[:120]})"
                )
            if not self._browser_click_retry(page, log_callback=log_callback):
                break
            phase = self._wait_browser_phase(
                page,
                want | {"error"},
                timeout=wait_after,
                log_callback=log_callback,
                label=f"after Retry #{i + 1}",
            )
            if phase in want:
                return phase
            if phase != "error":
                return phase
        return self._browser_page_phase(page)

    def _page_is_device_rate_limited(self, page) -> bool:
        """True when verify/consent hit IdP rate_limited (URL and/or body)."""
        url = self._page_url(page).lower()
        if "error=rate_limited" in url or "error=slow_down" in url:
            return True
        if "rate_limited" in url or "ratelimited" in url:
            return True
        try:
            hit = page.run_js(
                """
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const url = normalize(location.href || '');
if (url.includes('error=rate_limited') || url.includes('error=slow_down')
    || url.includes('rate_limited') || url.includes('too_many')) return true;
const body = normalize(document.body && (document.body.innerText || document.body.textContent) || '');
if (/rate.?limit|too many (device|request)|try again later|请求过于频繁|操作过于频繁|稍后再试/.test(body)) {
  // Avoid matching the static helper text about "only enter this code…".
  if (/rate.?limit|too many|过于频繁|slow.?down/.test(body)) return true;
}
const params = new URLSearchParams(location.search || '');
const err = normalize(params.get('error') || '');
return err === 'rate_limited' || err === 'slow_down' || err === 'too_many_requests';
"""
            )
            # Strict: only accept real booleans (mock/errant dicts must not count).
            return hit is True
        except Exception:
            return False

    def _device_verify_backoff_secs(self, attempt: int) -> float:
        attempt = max(int(attempt), 1)
        delay = float(DEVICE_VERIFY_BACKOFF_BASE_SECS) * (2 ** (attempt - 1))
        delay = min(delay, float(DEVICE_VERIFY_BACKOFF_MAX_SECS))
        delay += random.uniform(0.0, float(DEVICE_START_BACKOFF_JITTER_SECS))
        return delay

    def _browser_reopen_user_code_page(
        self,
        page,
        user_code: str,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Reload device verify URL so rate_limited error state is cleared."""
        code = str(user_code or "").strip()
        if not code:
            return
        # Prefer same host as current page; fall back to accounts.x.ai.
        current = self._page_url(page)
        base = "https://accounts.x.ai/oauth2/device"
        try:
            parsed = urlparse(current)
            if parsed.scheme in ("http", "https") and parsed.netloc.endswith("x.ai"):
                # Strip query/error and open a clean user_code URL.
                path = parsed.path or "/oauth2/device"
                if "device" not in path:
                    path = "/oauth2/device"
                base = f"{parsed.scheme}://{parsed.netloc}{path.split('?')[0]}"
        except Exception:
            pass
        target = f"{base}?user_code={code}"
        if not safe_xai_url(target):
            target = f"https://accounts.x.ai/oauth2/device?user_code={code}"
        if log_callback:
            log_callback(
                f"[*] Device Flow: reopen user-code page after rate limit "
                f"(code={code!r})"
            )
        try:
            page.get(target, timeout=45)
        except TypeError:
            try:
                page.get(target)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] reopen user-code failed: {exc}")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] reopen user-code failed: {exc}")
        time.sleep(0.5)

    def _wait_browser_phase(
        self,
        page,
        wanted: set[str] | frozenset[str],
        *,
        timeout: float = 15.0,
        log_callback: Callable[[str], None] | None = None,
        label: str = "",
        detect_rate_limit: bool = True,
    ) -> str:
        deadline = time.time() + max(timeout, 1.0)
        last = "unknown"
        while time.time() < deadline:
            if detect_rate_limit and self._page_is_device_rate_limited(page):
                if log_callback and label:
                    log_callback(
                        f"[Debug] wait {label}: rate_limited "
                        f"(url={self._page_url(page)[:120]})"
                    )
                return "rate_limited"
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
            {"user_code", "consent", "done", "error"},
            timeout=12.0,
            log_callback=log_callback,
            label="device UI",
        )
        if phase == "done":
            return True
        if phase == "auth_bounce":
            return False
        if phase == "error":
            phase = self._browser_recover_error_page(
                page,
                log_callback=log_callback,
                max_clicks=3,
                wanted={"user_code", "consent", "done"},
            )
            if phase == "done":
                return True
            if phase == "error":
                if log_callback:
                    log_callback(
                        "[Debug] Device Flow: still error after Retry"
                    )
                    self._log_browser_diag(
                        page,
                        expected_user_code=user_code,
                        log_callback=log_callback,
                        label="stuck on error after Retry",
                    )
                return False

        # Step 1: user code page → Continue (never Allow here)
        if phase == "user_code":
            if log_callback:
                log_callback(
                    f"[*] Device Flow: user-code page → Continue "
                    f"(code={user_code!r}, url={self._page_url(page)[:120]})"
                )
                self._log_browser_diag(
                    page,
                    expected_user_code=user_code,
                    log_callback=log_callback,
                    label="before Continue",
                )
            if not self._browser_submit_continue(
                page, user_code, log_callback=log_callback
            ):
                return False
            phase = self._wait_browser_phase(
                page,
                {"consent", "done", "error"},
                timeout=15.0,
                log_callback=log_callback,
                label="consent after Continue",
            )
            if phase == "done":
                return True
            if phase == "error":
                phase = self._browser_recover_error_page(
                    page,
                    log_callback=log_callback,
                    max_clicks=3,
                    wanted={"consent", "done"},
                )
                if phase == "done":
                    return True
            if phase != "consent":
                if log_callback:
                    log_callback(
                        f"[Debug] Device Flow: after Continue still not consent "
                        f"(phase={phase}, expected user_code={user_code!r})"
                    )
                    self._log_browser_diag(
                        page,
                        expected_user_code=user_code,
                        log_callback=log_callback,
                        label="stuck after Continue",
                        extra={"expected_phase": "consent", "actual_phase": phase},
                    )
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
            self._log_browser_diag(
                page,
                expected_user_code=user_code,
                log_callback=log_callback,
                label="unknown phase",
                extra={"phase": phase},
            )
        return False

    def _browser_click_continue_js(
        self,
        page,
        user_code: str,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        """Fill user_code (React-safe) and click Continue once."""
        user_code_js = json.dumps(str(user_code or ""))
        js = f"""
const userCode = {user_code_js};
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const snapshotButtons = () => Array.from(
  document.querySelectorAll('button, input[type="submit"], a[role="button"]')
).slice(0, 10).map((b) => ({{
  text: normalize(b.innerText || b.textContent || b.value || '').slice(0, 40),
  disabled: !!(b.disabled || b.getAttribute('disabled') != null),
  type: (b.getAttribute('type') || b.type || '').toLowerCase(),
}}));
const formMeta = (f) => f ? {{
  action: f.getAttribute('action') || '',
  method: (f.getAttribute('method') || 'get').toLowerCase(),
  inputNames: Array.from(f.querySelectorAll('input')).map((i) => i.name || '').filter(Boolean).slice(0, 8),
}} : null;

// React-controlled inputs ignore naive .value= ; use native setter.
const setNativeValue = (input, value) => {{
  if (!input) return '';
  try {{ input.focus(); }} catch (e) {{}}
  try {{
    const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
    const desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
    if (desc && desc.set) desc.set.call(input, value);
    else input.value = value;
  }} catch (e) {{
    input.value = value;
  }}
  try {{
    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
    input.dispatchEvent(new KeyboardEvent('keyup', {{ bubbles: true, key: '0' }}));
  }} catch (e) {{}}
  // Force-enable Continue if the SPA still thinks the field is empty.
  try {{
    const form = input.form || input.closest('form');
    if (form) {{
      Array.from(form.querySelectorAll('button, input[type="submit"]')).forEach((b) => {{
        const t = normalize(b.innerText || b.textContent || b.value || '');
        if (t === 'continue' || t === '继续' || t === '确认' || t === 'next' || t === '下一步') {{
          b.disabled = false;
          b.removeAttribute('disabled');
          b.removeAttribute('aria-disabled');
        }}
      }});
    }}
  }} catch (e) {{}}
  return String(input.value || '');
}};

// Refuse to act on consent form — that is the next page.
const approveForm = document.querySelector('form[action*="device/approve"]');
if (approveForm) {{
  return {{ ok: false, reason: 'already_on_consent', form: formMeta(approveForm), buttons: snapshotButtons(), url: location.href }};
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
  return setNativeValue(input, value);
}};

if (form) {{
  let filled = '';
  if (userCode) filled = setField(form, 'user_code', userCode);
  const codeEl = form.querySelector('input[name="user_code"]');
  const codeValue = codeEl ? String(codeEl.value || '') : filled;
  const buttons = Array.from(form.querySelectorAll('button, input[type="submit"]'));
  const buttonTexts = buttons.map((b) => normalize(b.innerText || b.textContent || b.value || ''));
  const cont = buttons.find((b) => {{
    const t = normalize(b.innerText || b.textContent || b.value || '');
    return t === 'continue' || t === '继续' || t === '确认' || t === 'next' || t === '下一步';
  }});
  // Do not click Allow/Deny if somehow present.
  if (cont) {{
    // After refill, Continue may still report disabled for one frame.
    if (userCode && codeValue) {{
      cont.disabled = false;
      cont.removeAttribute('disabled');
      cont.removeAttribute('aria-disabled');
    }}
    const disabled = !!(cont.disabled || cont.getAttribute('disabled') != null);
    if (disabled) {{
      return {{
        ok: false,
        reason: 'continue_disabled',
        via: 'continue_click',
        form: formMeta(form),
        codeValueLen: codeValue.length,
        codeMatches: codeValue.toUpperCase() === String(userCode || '').toUpperCase(),
        buttonTexts,
        buttons: snapshotButtons(),
        url: location.href,
      }};
    }}
    cont.click();
    return {{
      ok: true,
      via: 'continue_click',
      form: formMeta(form),
      codeValueLen: codeValue.length,
      codeMatches: codeValue.toUpperCase() === String(userCode || '').toUpperCase(),
      buttonTexts,
      url: location.href,
    }};
  }}
  // Single submit on verify form is Continue.
  if (buttons.length === 1) {{
    if (buttons[0].disabled && userCode && codeValue) {{
      buttons[0].disabled = false;
      buttons[0].removeAttribute('disabled');
    }}
    if (buttons[0].disabled) {{
      return {{
        ok: false,
        reason: 'single_submit_disabled',
        form: formMeta(form),
        buttonTexts,
        buttons: snapshotButtons(),
        url: location.href,
      }};
    }}
    buttons[0].click();
    return {{
      ok: true,
      via: 'single_submit',
      form: formMeta(form),
      codeValueLen: codeValue.length,
      codeMatches: codeValue.toUpperCase() === String(userCode || '').toUpperCase(),
      buttonTexts,
      url: location.href,
    }};
  }}
  try {{
    form.submit();
    return {{
      ok: true,
      via: 'verify_form_submit',
      form: formMeta(form),
      codeValueLen: codeValue.length,
      codeMatches: codeValue.toUpperCase() === String(userCode || '').toUpperCase(),
      buttonTexts,
      url: location.href,
    }};
  }} catch (e) {{
    return {{
      ok: false,
      reason: 'form_submit_threw',
      error: String(e && e.message ? e.message : e),
      form: formMeta(form),
      buttonTexts,
      buttons: snapshotButtons(),
      url: location.href,
    }};
  }}
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
  let codeValue = '';
  if (codeInput && userCode) {{
    codeValue = setNativeValue(codeInput, userCode);
  }}
  if (userCode && codeValue) {{
    contBtn.disabled = false;
    contBtn.removeAttribute('disabled');
    contBtn.removeAttribute('aria-disabled');
  }}
  if (contBtn.disabled || contBtn.getAttribute('disabled') != null) {{
    return {{
      ok: false,
      reason: 'page_continue_disabled',
      via: 'page_continue_click',
      codeValueLen: codeValue.length,
      formCount: forms.length,
      buttons: snapshotButtons(),
      url: location.href,
    }};
  }}
  contBtn.click();
  return {{
    ok: true,
    via: 'page_continue_click',
    codeValueLen: codeValue.length,
    codeMatches: codeValue.toUpperCase() === String(userCode || '').toUpperCase(),
    formCount: forms.length,
    buttons: snapshotButtons(),
    url: location.href,
  }};
}}
return {{
  ok: false,
  reason: 'no_continue_control',
  formCount: forms.length,
  formActions: forms.map((f) => f.getAttribute('action') || '').slice(0, 4),
  buttons: snapshotButtons(),
  url: location.href,
}};
"""
        try:
            result = page.run_js(js)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] browser Continue JS failed: {exc}")
                self._log_browser_diag(
                    page,
                    expected_user_code=user_code,
                    log_callback=log_callback,
                    label="Continue JS exception",
                    extra={"error": str(exc)},
                )
            return None
        return result if isinstance(result, dict) else None

    def _browser_submit_continue(
        self,
        page,
        user_code: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> bool:
        """Click Continue on the user-code verification page.

        Equivalent to POST /oauth2/device/verify with user_code.
        Must not touch the consent Allow/Deny form.

        When the IdP returns error=rate_limited it clears the code field;
        backoff, re-open the verify URL, re-type user_code, and Continue again.
        """
        max_attempts = max(int(DEVICE_VERIFY_MAX_ATTEMPTS), 1)
        last_result: dict[str, Any] | None = None
        last_phase = "unknown"
        url_before = self._page_url(page)

        for attempt in range(1, max_attempts + 1):
            # Already past verify?
            if self._page_is_device_done(page):
                return True
            phase_now = self._browser_page_phase(page)
            if phase_now == "consent":
                if log_callback:
                    log_callback("[*] Device Flow: already on consent (skip Continue)")
                return True

            rate_limited = self._page_is_device_rate_limited(page)
            if rate_limited or attempt > 1:
                if rate_limited:
                    wait = self._device_verify_backoff_secs(attempt)
                    # Share cooldown with device-code mint so workers don't pile on.
                    self._mark_device_start_rate_limited(wait)
                    if log_callback:
                        log_callback(
                            f"[*] Device Flow: verify rate_limited → backoff "
                            f"{wait:.1f}s then re-enter user_code "
                            f"(attempt {attempt}/{max_attempts}, "
                            f"code={user_code!r}, url={self._page_url(page)[:120]})"
                        )
                    time.sleep(wait)
                    self._browser_reopen_user_code_page(
                        page, user_code, log_callback=log_callback
                    )
                elif attempt > 1:
                    # Non-rate-limit retry: short pause + re-fill in place.
                    time.sleep(0.8)

            # Ensure user_code field is ready before clicking.
            phase_ready = self._wait_browser_phase(
                page,
                {"user_code", "consent", "done", "error"},
                timeout=10.0,
                log_callback=log_callback,
                label="user-code for Continue",
                detect_rate_limit=False,
            )
            if phase_ready == "done":
                return True
            if phase_ready == "consent":
                if log_callback:
                    log_callback("[*] Device Flow: already on consent (skip Continue)")
                return True
            if phase_ready == "error":
                recovered = self._browser_recover_error_page(
                    page,
                    log_callback=log_callback,
                    max_clicks=2,
                    wanted={"user_code", "consent", "done"},
                )
                if recovered == "done":
                    return True
                if recovered == "consent":
                    return True
                if recovered != "user_code":
                    last_phase = recovered
                    continue

            result = self._browser_click_continue_js(
                page, user_code, log_callback=log_callback
            )
            last_result = result

            if isinstance(result, dict) and result.get("reason") == "already_on_consent":
                if log_callback:
                    log_callback("[*] Device Flow: already on consent (skip Continue)")
                return True
            if not isinstance(result, dict) or not result.get("ok"):
                reason = result.get("reason") if isinstance(result, dict) else result
                # Disabled Continue with empty/partial code often follows rate_limit;
                # treat as retryable.
                retryable = str(reason or "") in (
                    "continue_disabled",
                    "single_submit_disabled",
                    "page_continue_disabled",
                ) or self._page_is_device_rate_limited(page)
                if retryable and attempt < max_attempts:
                    if log_callback:
                        log_callback(
                            f"[Debug] Continue not ready ({reason!r}); "
                            f"will re-enter user_code "
                            f"(attempt {attempt}/{max_attempts})"
                        )
                    # Force rate-limit style recovery next loop.
                    if not self._page_is_device_rate_limited(page):
                        # Soft backoff even without explicit error flag.
                        soft = min(
                            self._device_verify_backoff_secs(attempt) * 0.5,
                            15.0,
                        )
                        time.sleep(soft)
                        self._browser_reopen_user_code_page(
                            page, user_code, log_callback=log_callback
                        )
                    continue
                if log_callback:
                    via = result.get("via") if isinstance(result, dict) else None
                    log_callback(
                        f"[Debug] browser Continue missed: reason={reason!r} via={via!r} "
                        f"raw={result!r}"
                    )
                    self._log_browser_diag(
                        page,
                        expected_user_code=user_code,
                        log_callback=log_callback,
                        label="Continue click missed",
                        extra={
                            "reason": reason,
                            "via": via,
                            "attempt": attempt,
                            "result": result if isinstance(result, dict) else str(result),
                        },
                    )
                return False

            via = str(result.get("via") or "ok")
            if log_callback:
                log_callback(
                    f"[*] Device Flow: Continue submitted (via={via}, "
                    f"code_len={result.get('codeValueLen')}, "
                    f"code_match={result.get('codeMatches')}, "
                    f"attempt={attempt}, "
                    f"url={(result.get('url') or self._page_url(page))[:100]})"
                )
            url_before = self._page_url(page)
            # Wait until we leave the pure user-code page.
            phase = self._wait_browser_phase(
                page,
                {"consent", "done", "error"},
                timeout=15.0,
                log_callback=log_callback,
                label="after Continue",
            )
            last_phase = phase
            if phase == "rate_limited":
                if attempt >= max_attempts:
                    break
                # Loop: backoff + re-enter user_code.
                continue
            if phase == "error":
                phase = self._browser_recover_error_page(
                    page,
                    log_callback=log_callback,
                    max_clicks=3,
                    wanted={"consent", "done", "user_code"},
                )
                last_phase = phase
                if phase == "rate_limited" or self._page_is_device_rate_limited(page):
                    continue
                if phase == "user_code":
                    # Error recovery landed back on code page — re-Continue.
                    continue
            if phase in ("consent", "done"):
                if log_callback:
                    log_callback(
                        f"[*] Device Flow: left user-code page → phase={phase} "
                        f"(was url={url_before[:100]})"
                    )
                return True

            # Still on user_code without rate_limit flag: check once more.
            if self._page_is_device_rate_limited(page):
                continue
            if attempt < max_attempts:
                # Empty input after submit often means the IdP bounced us —
                # re-open and retry once more with backoff.
                if log_callback:
                    log_callback(
                        f"[Debug] Continue still on user-code "
                        f"(phase={phase}); re-enter user_code "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                wait = min(self._device_verify_backoff_secs(attempt) * 0.4, 12.0)
                time.sleep(wait)
                self._browser_reopen_user_code_page(
                    page, user_code, log_callback=log_callback
                )
                continue
            break

        if log_callback:
            log_callback(
                f"[Debug] browser Continue did not leave user-code page "
                f"(phase_after={last_phase}, attempts={max_attempts}, "
                f"url_before={url_before[:120]}, url_after={self._page_url(page)[:120]})"
            )
            self._log_browser_diag(
                page,
                expected_user_code=user_code,
                log_callback=log_callback,
                label="still on user-code after Continue",
                extra={
                    "phase_after": last_phase,
                    "attempts": max_attempts,
                    "url_before": url_before,
                    "rate_limited": self._page_is_device_rate_limited(page),
                    "submit_result": {
                        k: last_result.get(k)
                        for k in (
                            "via",
                            "codeValueLen",
                            "codeMatches",
                            "buttonTexts",
                            "form",
                            "reason",
                        )
                        if isinstance(last_result, dict) and k in last_result
                    },
                },
            )
        return False
    def _browser_click_allow_js(
        self,
        page,
        user_code: str,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        """Run the Allow form submit JS once. Returns result dict or None."""
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
            return None
        return result if isinstance(result, dict) else None

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

        After Allow the IdP sometimes shows a transient error page with Retry;
        click Retry and re-Allow if consent reappears.
        """
        max_allow_attempts = 4  # initial + retries after error page recovery
        for attempt in range(max_allow_attempts):
            phase = self._wait_browser_phase(
                page,
                {"consent", "done", "error"},
                timeout=12.0,
                log_callback=log_callback,
                label="consent for Allow",
            )
            if phase == "done":
                return True
            if phase == "error":
                phase = self._browser_recover_error_page(
                    page,
                    log_callback=log_callback,
                    max_clicks=3,
                    wanted={"consent", "done"},
                )
                if phase == "done":
                    return True
            if phase != "consent":
                if log_callback:
                    log_callback(
                        f"[Debug] Allow refused: not on consent (phase={phase})"
                    )
                return False

            result = self._browser_click_allow_js(
                page, user_code, log_callback=log_callback
            )
            if not result or not result.get("ok"):
                if log_callback:
                    reason = result.get("reason") if isinstance(result, dict) else result
                    log_callback(f"[Debug] browser Allow missed: {reason}")
                return False

            if log_callback:
                log_callback(
                    f"[*] Device Flow: Allow submitted "
                    f"({result.get('via') or 'ok'}"
                    f"{f', attempt={attempt + 1}' if attempt else ''})"
                )

            wait_deadline = time.time() + 15.0
            saw_error = False
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
                phase_now = self._browser_page_phase(page)
                if phase_now == "error":
                    saw_error = True
                    if log_callback:
                        log_callback(
                            "[*] Device Flow: after Allow → error page, clicking Retry"
                        )
                    recovered = self._browser_recover_error_page(
                        page,
                        log_callback=log_callback,
                        max_clicks=3,
                        wanted={"consent", "done"},
                    )
                    if recovered == "done":
                        if log_callback:
                            log_callback("[*] Device Flow: browser reached done")
                        return True
                    if recovered == "consent":
                        # Consent form reloaded after Retry — re-Allow.
                        break
                    if log_callback:
                        log_callback(
                            f"[Debug] browser Allow still error after Retry "
                            f"(phase={recovered})"
                        )
                    return False
                # Still on user-code means we never left Continue page — fail.
                if phase_now == "user_code":
                    if log_callback:
                        log_callback("[Debug] browser Allow still on user-code page")
                    return False
                time.sleep(0.4)

            if self._page_is_device_done(page):
                return True
            if saw_error and self._browser_page_phase(page) == "consent":
                # Loop to re-submit Allow after Retry recovered consent UI.
                continue
            if log_callback:
                log_callback(
                    f"[Debug] browser Allow did not reach done "
                    f"(phase={self._browser_page_phase(page)}, "
                    f"url={self._page_url(page)[:100]})"
                )
            return False

        if log_callback:
            log_callback(
                f"[Debug] browser Allow exhausted retries "
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

    @staticmethod
    def _as_api_request(req: Any) -> Any | None:
        if req is None:
            return None
        if hasattr(req, "get") and hasattr(req, "post"):
            return req
        return None

    @classmethod
    def _resolve_api_request(cls, page: Any) -> Any | None:
        """Return Playwright APIRequestContext from page / browser context.

        Preference order:
          1. ``page.request`` (PatchrightPage / raw Playwright Page)
          2. underlying ``page._p.request`` (adapter wrapper)
          3. ``context.request`` via ``page._ctx`` / ``page.context``
        """
        if page is None:
            return None
        try:
            found = cls._as_api_request(getattr(page, "request", None))
            if found is not None:
                return found
        except Exception:
            pass
        try:
            pw = getattr(page, "_p", None)
            if pw is not None:
                found = cls._as_api_request(getattr(pw, "request", None))
                if found is not None:
                    return found
                ctx = getattr(pw, "context", None)
                found = cls._as_api_request(
                    getattr(ctx, "request", None) if ctx is not None else None
                )
                if found is not None:
                    return found
        except Exception:
            pass
        for attr in ("_ctx", "context"):
            try:
                ctx = getattr(page, attr, None)
                found = cls._as_api_request(
                    getattr(ctx, "request", None) if ctx is not None else None
                )
                if found is not None:
                    return found
            except Exception:
                continue
        return None

    def _request(self, method: str, url: str, *, headers: dict, data: str | None):
        """Device/token HTTP via Playwright page.request / context.request.

        Shares the registration browser's cookie jar, proxy, and TLS stack.
        Manual redirects stay in ``_do`` (max_redirects=0).
        """
        page = self._request_page
        api = self._resolve_api_request(page)
        if api is None:
            raise SSOBuildError(
                "Device Flow HTTP requires page.request / context.request "
                "(bind an active registration browser page)"
            )
        timeout_ms = int(max(float(self.timeout or 30), 45.0) * 1000)
        method_u = (method or "GET").upper()
        hdrs = dict(headers or {})

        def _dispatch(**extra: Any):
            kwargs: dict[str, Any] = {
                "headers": hdrs,
                "timeout": timeout_ms,
            }
            kwargs.update(extra)
            if method_u == "GET":
                return api.get(url, **kwargs)
            if method_u == "POST":
                return api.post(url, data=data, **kwargs)
            return api.fetch(url, method=method_u, data=data, **kwargs)

        try:
            try:
                resp = _dispatch(max_redirects=0, fail_on_status_code=False)
            except TypeError:
                try:
                    resp = _dispatch(max_redirects=0)
                except TypeError:
                    resp = _dispatch()
        except Exception as exc:
            raise SSOBuildError(
                f"xAI OAuth via page.request failed ({method_u} {url}): {exc}"
            ) from exc
        return _PlaywrightHttpResponse(resp)


def convert_sso_to_build(
    sso_token: str,
    *,
    email: str = "",
    name: str = "",
    user_agent: str = DEFAULT_USER_AGENT,
    proxies: dict | None = None,
    page=None,
    mode: str = "browser",
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Convert Web SSO → Build OAuth seed dict (browser verify/approve only).

    ``mode`` is accepted for backward compatibility; only ``browser`` is
    supported. HTTP auto-approve was removed because the IdP no longer accepts it.
    """
    mode = str(mode or "browser").strip().lower() or "browser"
    if mode in ("http", "auto"):
        if log_callback:
            log_callback(
                f"[*] Device Flow: mode={mode} is deprecated; using browser only"
            )
        mode = "browser"
    if mode != "browser":
        raise SSOBuildError(f"unsupported Device Flow mode: {mode} (only browser)")
    if page is None:
        raise SSOBuildError("browser Device Flow requires an active page")

    flow = SSOBuildFlow(
        sso_token,
        user_agent=user_agent,
        proxies=proxies,
    )
    return flow.convert_with_browser(
        page, email=email, name=name, log_callback=log_callback
    )


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
