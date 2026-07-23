"""Register via Grok Build OAuth (Auth Code + PKCE) — same as ``grok login --oauth``.

Primary path (OAuth entry registration):

  1. OIDC discovery → PKCE + state + nonce
  2. Bind loopback ``http://127.0.0.1:{port}/callback``
  3. Open **authorize URL** in the registration browser (this is the signup entry)
  4. Complete sign-up on that page (email / code / profile / Turnstile)
  5. Consent Allow → capture ``code`` on loopback
  6. POST ``/oauth2/token`` with ``authorization_code`` + PKCE verifier

Secondary path (legacy): already have Web SSO → seed cookies → authorize → tokens
(``convert_sso_to_build``). Prefer the OAuth-entry path so registration and Build
tokens happen in one authorize session.

Aligned with grok-build ``auth/oidc/login.rs`` + ``protocol.rs``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

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
SSO_BUILD_CLIENT_VERSION = "0.2.111"
SSO_BUILD_CLIENT_SURFACE = "cli"

SSO_ISSUER = "https://auth.x.ai"
SSO_AUTHORIZE_URL = f"{SSO_ISSUER}/oauth2/authorize"
SSO_TOKEN_URL = f"{SSO_ISSUER}/oauth2/token"
SSO_DISCOVERY_URL = f"{SSO_ISSUER}/.well-known/openid-configuration"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
MAX_BODY = 2 << 20
AUTH_CALLBACK_TIMEOUT_SECS = 600
LOOPBACK_CORS_ORIGINS = frozenset(
    {
        "https://accounts.x.ai",
        "https://auth.x.ai",
    }
)


class SSOBuildError(RuntimeError):
    """OAuth registration / conversion failed."""

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
    if parsed.scheme == "http" and host in ("localhost", "127.0.0.1"):
        return True
    return False


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


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256 (RFC 7636)."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


def build_authorize_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    code_challenge: str,
    state: str,
    nonce: str,
    referrer: str = SSO_BUILD_REFERRER,
) -> str:
    """Build authorize URL matching grok-build ``build_authorize_url``."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "referrer": referrer or SSO_BUILD_REFERRER,
    }
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


def parse_callback_query(query: str | dict) -> tuple[str, str, str]:
    """Return (code, state, error) from a callback query string or mapping."""
    if isinstance(query, dict):
        params = {
            str(k): (v[0] if isinstance(v, list) and v else v) for k, v in query.items()
        }
    else:
        raw = parse_qs(str(query or "").lstrip("?"), keep_blank_values=True)
        params = {k: (v[0] if v else "") for k, v in raw.items()}
    code = str(params.get("code") or "").strip()
    state = str(params.get("state") or "").strip()
    error = str(params.get("error") or "").strip()
    desc = str(params.get("error_description") or "").strip()
    if error and desc:
        error = f"{error}: {desc}"
    return code, state, error


def callback_success_html() -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset=utf-8>"
        "<title>Signed in</title></head><body style='font-family:system-ui;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "background:#0a0a0a;color:#e5e5e5'>"
        "<div style='text-align:center'><h1>Signed in</h1>"
        "<p style='color:#a3a3a3'>You can close this window and return to Grok.</p>"
        "</div></body></html>"
    )


def callback_error_html() -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset=utf-8>"
        "<title>Access denied</title></head><body style='font-family:system-ui;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "background:#0a0a0a;color:#e5e5e5'>"
        "<div style='text-align:center'><h1>Access denied</h1>"
        "<p style='color:#a3a3a3'>Close this window and try again.</p>"
        "</div></body></html>"
    )


class _LoopbackCallbackServer:
    """Minimal 127.0.0.1 callback server with accounts-app CORS (Private Network Access)."""

    def __init__(self, port: int = 0):
        self._result: dict[str, str] | None = None
        self._event = threading.Event()
        self._error: str | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _cors_headers(self) -> None:
                origin = str(self.headers.get("Origin") or "").strip()
                if origin in LOOPBACK_CORS_ORIGINS or (
                    origin.startswith("https://") and origin.endswith(".x.ai")
                ):
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    self.headers.get("Access-Control-Request-Headers") or "*",
                )
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self._cors_headers()
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path or "/"
                if path.rstrip("/") != "/callback":
                    self.send_response(404)
                    self._cors_headers()
                    self.end_headers()
                    return
                code, state, error = parse_callback_query(parsed.query)
                if error and not code:
                    owner._error = error
                    owner._result = {"code": "", "state": state, "error": error}
                    owner._event.set()
                    body = callback_error_html().encode("utf-8")
                    self.send_response(200)
                    self._cors_headers()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not code:
                    self.send_response(400)
                    self._cors_headers()
                    self.end_headers()
                    return
                owner._result = {"code": code, "state": state, "error": ""}
                owner._event.set()
                body = callback_success_html().encode("utf-8")
                self.send_response(200)
                self._cors_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
        self.port = int(self._httpd.server_address[1])
        self.redirect_uri = f"http://127.0.0.1:{self.port}/callback"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> dict[str, str]:
        if not self._event.wait(timeout=max(float(timeout), 1.0)):
            raise SSOBuildError("Login timed out after waiting for OAuth callback")
        result = self._result or {}
        if result.get("error") and not result.get("code"):
            raise SSOBuildError(
                f"OAuth callback error: {result.get('error')}",
                unauthorized=True,
            )
        if not result.get("code"):
            raise SSOBuildError("OAuth callback missing authorization code")
        return result

    def close(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass


class _PlaywrightHttpResponse:
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


class OAuthRegisterSession:
    """One authorize session: generate OAuth link → register inside it → tokens.

    Lifecycle:
      begin()           → authorize_url (open this as signup entry)
      finish(page)      → after signup completes, Allow + code exchange → seed
      close()           → always (also called from finish)
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 30.0,
        client_surface: str = SSO_BUILD_CLIENT_SURFACE,
        callback_timeout: float = AUTH_CALLBACK_TIMEOUT_SECS,
    ):
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.callback_timeout = float(callback_timeout or AUTH_CALLBACK_TIMEOUT_SECS)
        self.client_surface = (client_surface or SSO_BUILD_CLIENT_SURFACE).strip() or "cli"
        self._request_page: Any = None
        self._server: _LoopbackCallbackServer | None = None
        self._code_verifier = ""
        self._state = ""
        self._nonce = ""
        self._redirect_uri = ""
        self._authorize_url = ""
        self._token_endpoint = SSO_TOKEN_URL
        self._authorization_endpoint = SSO_AUTHORIZE_URL
        self._started = False
        self._closed = False

    @property
    def authorize_url(self) -> str:
        return self._authorize_url

    @property
    def redirect_uri(self) -> str:
        return self._redirect_uri

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    def begin(
        self,
        page=None,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> str:
        """Bind loopback, mint PKCE, return authorize URL (signup entry)."""
        if self._closed:
            raise SSOBuildError("OAuth session already closed")
        if self._started:
            return self._authorize_url

        self._request_page = page
        discovery = self._discover(log_callback=log_callback)
        self._authorization_endpoint = discovery["authorization_endpoint"]
        self._token_endpoint = discovery["token_endpoint"]

        code_verifier, code_challenge = generate_pkce()
        self._code_verifier = code_verifier
        self._state = str(uuid.uuid4())
        self._nonce = str(uuid.uuid4())

        server = _LoopbackCallbackServer(port=0)
        server.start()
        self._server = server
        self._redirect_uri = server.redirect_uri
        self._authorize_url = build_authorize_url(
            self._authorization_endpoint,
            client_id=SSO_BUILD_CLIENT_ID,
            redirect_uri=self._redirect_uri,
            scope=SSO_BUILD_SCOPE,
            code_challenge=code_challenge,
            state=self._state,
            nonce=self._nonce,
            referrer=SSO_BUILD_REFERRER,
        )
        self._started = True
        if log_callback:
            log_callback(
                f"[*] OAuth: authorize URL ready (loopback={self._redirect_uri}) "
                f"— register inside this session (grok login --oauth)"
            )
        return self._authorize_url

    def finish(
        self,
        page,
        *,
        email: str = "",
        name: str = "",
        log_callback: Callable[[str], None] | None = None,
        already_registered: bool = True,
    ) -> dict[str, Any]:
        """After signup (or with existing SSO), complete consent + token exchange."""
        if not self._started or self._server is None:
            raise SSOBuildError("OAuth session not started (call begin first)")
        if self._closed:
            raise SSOBuildError("OAuth session already closed")
        if page is None:
            raise SSOBuildError("browser OAuth requires an active page")

        self._request_page = page
        try:
            if log_callback:
                log_callback(
                    "[*] OAuth: waiting for consent/callback "
                    f"(registered={already_registered})"
                )
            code_payload = self._await_code(page, log_callback=log_callback)
            if log_callback:
                log_callback("[*] OAuth: exchanging authorization code (page.request)")
            token = self._exchange_code(
                self._token_endpoint,
                code=code_payload["code"],
                redirect_uri=self._redirect_uri,
                code_verifier=self._code_verifier,
            )
            return self._seed_from_token(token, email=email, name=name)
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None

    # ── discovery / token ──────────────────────────────────────────────

    def _discover(
        self,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, str]:
        try:
            status, _url, body = self._do("GET", SSO_DISCOVERY_URL, None, api_client=True)
            if 200 <= status < 300 and body:
                payload = json.loads(
                    body.decode("utf-8", errors="replace")
                    if isinstance(body, (bytes, bytearray))
                    else body
                )
                auth_ep = str(payload.get("authorization_endpoint") or "").strip()
                token_ep = str(payload.get("token_endpoint") or "").strip()
                if auth_ep and token_ep and safe_xai_url(auth_ep) and safe_xai_url(token_ep):
                    if log_callback:
                        log_callback("[*] OAuth: OIDC discovery ok")
                    return {
                        "authorization_endpoint": auth_ep,
                        "token_endpoint": token_ep,
                    }
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] OAuth discovery failed, using defaults: {exc}")
        if log_callback:
            log_callback("[*] OAuth: using hardcoded auth.x.ai endpoints")
        return {
            "authorization_endpoint": SSO_AUTHORIZE_URL,
            "token_endpoint": SSO_TOKEN_URL,
        }

    def _exchange_code(
        self,
        token_endpoint: str,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        status, _url, body = self._do(
            "POST",
            token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": SSO_BUILD_CLIENT_ID,
                "code_verifier": code_verifier,
            },
            api_client=True,
        )
        try:
            payload = json.loads(
                body.decode("utf-8", errors="replace")
                if isinstance(body, (bytes, bytearray))
                else body
            )
        except Exception as exc:
            raise SSOBuildError(f"parse OAuth token: {exc}") from exc
        if not (200 <= status < 300) or not payload.get("access_token"):
            err = first_value(
                payload.get("error_description") if isinstance(payload, dict) else None,
                payload.get("error") if isinstance(payload, dict) else None,
                str(status),
            )
            raise SSOBuildError(f"OAuth token exchange failed: {err}", status=status)
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

    def _seed_from_token(
        self, token: dict[str, Any], *, email: str = "", name: str = ""
    ) -> dict[str, Any]:
        claims = decode_jwt_claims(
            first_value(token.get("id_token"), token.get("access_token"))
        )
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
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if expires_at
            else "",
            "scope": SSO_BUILD_SCOPE,
        }

    # ── consent / callback ─────────────────────────────────────────────

    def _await_code(
        self,
        page,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, str]:
        server = self._server
        if server is None:
            raise SSOBuildError("OAuth loopback server missing")
        expected_state = self._state
        deadline = time.time() + max(self.callback_timeout, 30.0)
        last_allow = 0.0
        allow_attempts = 0
        max_allow = 8

        while time.time() < deadline:
            if server._event.is_set():
                payload = server.wait(timeout=1.0)
                return self._validate_state(payload, expected_state)

            page_code = self._code_from_page_url(page)
            if page_code:
                if log_callback:
                    log_callback("[*] OAuth: code captured from browser redirect URL")
                return self._validate_state(page_code, expected_state)

            phase = self._browser_page_phase(page)
            if phase == "consent" and allow_attempts < max_allow:
                now = time.time()
                if now - last_allow >= 1.0:
                    if log_callback:
                        log_callback(
                            f"[*] OAuth: consent page → Allow "
                            f"(attempt {allow_attempts + 1})"
                        )
                    clicked = self._browser_click_allow(page, log_callback=log_callback)
                    allow_attempts += 1
                    last_allow = now
                    if clicked:
                        time.sleep(0.5)
            elif phase == "error":
                if log_callback:
                    log_callback("[*] OAuth: error page → Retry")
                self._browser_click_retry(page, log_callback=log_callback)
                time.sleep(0.6)

            time.sleep(0.25)

        if server._event.is_set():
            payload = server.wait(timeout=1.0)
            return self._validate_state(payload, expected_state)
        page_code = self._code_from_page_url(page)
        if page_code:
            return self._validate_state(page_code, expected_state)
        raise SSOBuildError(
            f"OAuth callback timeout "
            f"(phase={self._browser_page_phase(page)}, url={self._page_url(page)[:160]})"
        )

    def _validate_state(self, payload: dict[str, str], expected_state: str) -> dict[str, str]:
        received = str(payload.get("state") or "").strip()
        if received and expected_state and received != expected_state:
            raise SSOBuildError("OAuth authentication failed: state mismatch")
        if not payload.get("code"):
            raise SSOBuildError("OAuth callback missing authorization code")
        return payload

    def _code_from_page_url(self, page) -> dict[str, str] | None:
        url = self._page_url(page)
        if not url:
            return None
        try:
            parsed = urlparse(url)
        except Exception:
            return None
        host = (parsed.hostname or "").lower()
        if host not in ("127.0.0.1", "localhost"):
            return None
        if "/callback" not in (parsed.path or "") and "code=" not in (parsed.query or ""):
            return None
        code, state, error = parse_callback_query(parsed.query)
        if error and not code:
            raise SSOBuildError(f"OAuth callback error: {error}", unauthorized=True)
        if not code:
            return None
        return {"code": code, "state": state, "error": ""}

    def _page_url(self, page) -> str:
        try:
            return str(getattr(page, "url", "") or "")
        except Exception:
            return ""

    def _browser_page_phase(self, page) -> str:
        url = self._page_url(page).lower()
        if "127.0.0.1" in url or "localhost" in url:
            if "code=" in url or "/callback" in url:
                return "done"
        try:
            phase = page.run_js(
                """
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const url = (location.href || '').toLowerCase();
if (url.includes('127.0.0.1') || url.includes('localhost')) {
  if (url.includes('code=') || url.includes('/callback')) return 'done';
}
const buttons = Array.from(document.querySelectorAll(
  'button, input[type="submit"], a[role="button"], a'
));
const texts = buttons.map((b) => normalize(b.innerText || b.textContent || b.value || ''));
const hasRetry = texts.some((t) =>
  t === 'retry' || t === '重试' || t === 'try again' || t === '再试一次');
const hasAllow = texts.some((t) =>
  t === 'allow' || t === '允许' || t === 'approve' || t === '授权' || t === 'authorize');
const hasDeny = texts.some((t) =>
  t === 'deny' || t === '拒绝' || t === '取消' || t === 'cancel');
let bodyText = '';
try {
  bodyText = normalize(document.body && document.body.innerText ? document.body.innerText : '');
} catch (e) { bodyText = ''; }
const looksError = /error loading this page|an error occurred|there was an error|出错|加载失败/.test(bodyText)
  || Array.from(document.querySelectorAll('[role="alert"], .text-destructive, .text-danger'))
      .some((el) => normalize(el.innerText || el.textContent || '').length > 0);
if (hasRetry && (looksError || !hasAllow)) return 'error';
if (hasAllow && hasDeny) return 'consent';
if (hasAllow) return 'consent';
if (/authorize|授权|consent|permissions?/.test(bodyText) && hasAllow) return 'consent';
if (url.includes('consent') || url.includes('authorize')) {
  if (hasAllow) return 'consent';
}
return 'unknown';
"""
            )
        except Exception:
            phase = "unknown"
        phase = str(phase or "unknown").strip().lower()
        if phase in ("consent", "error", "done", "unknown"):
            return phase
        return "unknown"

    def _browser_click_allow(
        self,
        page,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> bool:
        js = """
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const isAllow = (t) =>
  t === 'allow' || t === '允许' || t === 'approve' || t === '授权' || t === 'authorize'
  || t === 'continue' || t === '继续' || t === '确认';
const isDeny = (t) =>
  t === 'deny' || t === '拒绝' || t === 'cancel' || t === '取消';
const nodes = Array.from(document.querySelectorAll(
  'button, input[type="submit"], input[type="button"], a[role="button"], a'
));
const visible = (el) => {
  try {
    if (el.disabled || el.getAttribute('disabled') != null || el.getAttribute('aria-disabled') === 'true') {
      return false;
    }
    return !!(el.offsetParent !== null || (el.getClientRects && el.getClientRects().length));
  } catch (e) { return true; }
};
let btn = nodes.find((b) => {
  const t = normalize(b.innerText || b.textContent || b.value || '');
  return (t === 'allow' || t === '允许' || t === 'approve' || t === '授权' || t === 'authorize') && visible(b);
});
if (!btn) {
  btn = nodes.find((b) => {
    const t = normalize(b.innerText || b.textContent || b.value || '');
    return isAllow(t) && !isDeny(t) && visible(b);
  });
}
if (!btn) {
  return { ok: false, reason: 'no_allow_button' };
}
try {
  const form = btn.form || btn.closest('form');
  if (form) {
    let action = form.querySelector('input[name="action"]');
    if (action) action.value = 'allow';
  }
} catch (e) {}
btn.click();
return {
  ok: true,
  via: 'allow_click',
  text: normalize(btn.innerText || btn.textContent || btn.value || '').slice(0, 40),
  url: location.href || '',
};
"""
        try:
            result = page.run_js(js)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] OAuth Allow JS failed: {exc}")
            return False
        if not isinstance(result, dict) or not result.get("ok"):
            if log_callback:
                reason = result.get("reason") if isinstance(result, dict) else result
                log_callback(f"[Debug] OAuth Allow missed: {reason}")
            return False
        return True

    def _browser_click_retry(
        self,
        page,
        *,
        log_callback: Callable[[str], None] | None = None,
    ) -> bool:
        js = """
const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const isRetry = (t) => t === 'retry' || t === '重试' || t === 'try again' || t === '再试一次';
const nodes = Array.from(document.querySelectorAll(
  'button, input[type="submit"], input[type="button"], a[role="button"], a'
));
const btn = nodes.find((b) => {
  const t = normalize(b.innerText || b.textContent || b.value || '');
  return isRetry(t);
});
if (!btn) return { ok: false, reason: 'no_retry_button' };
btn.click();
return { ok: true, via: 'retry_click' };
"""
        try:
            result = page.run_js(js)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] OAuth Retry JS failed: {exc}")
            return False
        return isinstance(result, dict) and bool(result.get("ok"))

    # ── HTTP ───────────────────────────────────────────────────────────

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
        # Without page.request, use stdlib for discovery only.
        if self._resolve_api_request(self._request_page) is None:
            return self._do_stdlib(method, endpoint, form)

        current_url = endpoint
        current_method = method.upper()
        current_form = form
        for _redirect in range(9):
            headers = {
                "Accept": "application/json",
                "User-Agent": self.user_agent,
                "x-grok-client-version": SSO_BUILD_CLIENT_VERSION,
                "x-grok-client-surface": self.client_surface,
            }
            data = None
            if current_form is not None:
                data = urlencode(current_form)
                headers["Content-Type"] = "application/x-www-form-urlencoded"

            response = self._request(current_method, current_url, headers=headers, data=data)
            status = int(getattr(response, "status_code", 0) or 0)
            try:
                body = (
                    response.content
                    if hasattr(response, "content")
                    else (response.text or "").encode("utf-8")
                )
            except Exception:
                body = b""
            if len(body) > MAX_BODY:
                raise SSOBuildError("xAI OAuth response > 2 MiB", status=status)

            if status < 300 or status > 399:
                return status, current_url, body

            location = str(
                (getattr(response, "headers", {}) or {}).get("Location") or ""
            ).strip()
            if not location:
                raise SSOBuildError(
                    f"xAI OAuth redirect missing Location (HTTP {status})", status=status
                )
            next_url = urljoin(current_url, location)
            if not safe_xai_url(next_url):
                raise SSOBuildError(
                    f"xAI OAuth redirected to untrusted host: {next_url}", status=status
                )
            if status in (301, 302, 303, 307, 308) and current_method not in ("GET", "HEAD"):
                current_method = "GET"
                current_form = None
            current_url = next_url
        raise SSOBuildError("xAI OAuth too many redirects")

    def _do_stdlib(
        self, method: str, endpoint: str, form: dict | None
    ) -> tuple[int, str, bytes]:
        """Fallback HTTP without Playwright (discovery before page is ready)."""
        import urllib.error
        import urllib.request

        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "x-grok-client-version": SSO_BUILD_CLIENT_VERSION,
            "x-grok-client-surface": self.client_surface,
        }
        if form is not None:
            data = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(
            endpoint, data=data, headers=headers, method=(method or "GET").upper()
        )
        try:
            with urllib.request.urlopen(req, timeout=max(float(self.timeout), 15.0)) as resp:
                body = resp.read()
                return int(resp.status), endpoint, body
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            return int(exc.code), endpoint, body or b""

    @staticmethod
    def _as_api_request(req: Any) -> Any | None:
        if req is None:
            return None
        if hasattr(req, "get") and hasattr(req, "post"):
            return req
        return None

    @classmethod
    def _resolve_api_request(cls, page: Any) -> Any | None:
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
        page = self._request_page
        api = self._resolve_api_request(page)
        if api is None:
            raise SSOBuildError(
                "OAuth HTTP requires page.request / context.request "
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


# Back-compat alias used by older call sites / tests.
SSOBuildFlow = OAuthRegisterSession


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
    """Legacy: already have Web SSO → open authorize with cookies → tokens.

    Prefer :class:`OAuthRegisterSession` so registration runs **inside** the
    authorize URL instead of converting after the fact.
    """
    _ = proxies  # kept for call-site compatibility
    mode = str(mode or "browser").strip().lower() or "browser"
    if mode in ("http", "auto"):
        mode = "browser"
    if mode != "browser":
        raise SSOBuildError(f"unsupported OAuth mode: {mode} (only browser)")
    if page is None:
        raise SSOBuildError("browser OAuth requires an active page")

    token = normalize_sso_token(sso_token)
    if not token:
        raise SSOBuildError("SSO token empty", unauthorized=True)

    # Seed SSO so authorize skips login and goes to consent.
    cookies = []
    for domain in (".x.ai", "accounts.x.ai", "auth.x.ai"):
        for cname in ("sso", "sso-rw"):
            cookies.append(
                {
                    "name": cname,
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
            ctx = getattr(page, "_ctx", None) or getattr(
                getattr(page, "_p", None), "context", None
            )
            if ctx is not None:
                ctx.add_cookies(cookies)
        except Exception:
            pass

    session = OAuthRegisterSession(user_agent=user_agent)
    try:
        auth_url = session.begin(page, log_callback=log_callback)
        if log_callback:
            log_callback("[*] OAuth: open authorize URL (SSO already present)")
        try:
            page.get(auth_url, timeout=60)
        except TypeError:
            page.get(auth_url)
        return session.finish(
            page, email=email, name=name, log_callback=log_callback, already_registered=True
        )
    except Exception:
        session.close()
        raise


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
