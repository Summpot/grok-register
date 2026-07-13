"""Protocol OAuth mint (SSO-first, no browser) via vendored xconsole_client.

Uses the same Free Build / CLIProxy channel as browser device-code mint.
With a fresh SSO cookie, CreateSession / YesCaptcha is not required.
"""
from __future__ import annotations

from typing import Any, Callable

from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def _sso_from_cookies(cookies: Any) -> str:
    if not isinstance(cookies, list):
        return ""
    for name in ("sso", "sso-rw"):
        for c in cookies:
            if isinstance(c, dict) and c.get("name") == name and c.get("value"):
                return str(c.get("value") or "").strip()
    return ""


def mint_tokens_protocol(
    *,
    email: str,
    password: str,
    sso: str | None = None,
    cookies: Any | None = None,
    proxy: str | None = None,
    yescaptcha_key: str = "",
    debug: bool = False,
    redirect_port: int = 56121,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Run protocol OAuth and return token dict (access/refresh/id/expires_in).

    Raises RuntimeError on failure.
    """
    log = log or _noop
    email = (email or "").strip()
    password = password or ""
    if not email:
        raise RuntimeError("missing email")

    sso_val = (sso or "").strip() or _sso_from_cookies(cookies)
    session_cookies: dict[str, str] | None = {"sso": sso_val} if sso_val else None

    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    log(
        f"protocol mint start: {email} "
        f"sso={'yes' if sso_val else 'no'} "
        f"proxy={proxy_log_label(resolved) or '(none)'} "
        f"yescaptcha={'yes' if (yescaptcha_key or '').strip() else 'no'}"
    )

    # Import lazily so browser-only installs are unaffected until protocol is used.
    from grok_register.xconsole_client.oauth_protocol import login_with_protocol

    # Do not write intermediate oauth_output or cliproxy dirs; caller uses cpa writer.
    result = login_with_protocol(
        email,
        password,
        yescaptcha_key=(yescaptcha_key or "").strip(),
        proxy=resolved or "",
        debug=debug,
        cliproxyapi_auth_dir=None,
        output_dir=None,
        redirect_port=int(redirect_port or 56121),
        session_cookies=session_cookies,
    )
    access = (result.access_token or "").strip()
    refresh = (result.refresh_token or "").strip()
    if not access or not refresh:
        raise RuntimeError("protocol OAuth returned empty access/refresh token")

    token = result.token or {}
    log(
        f"protocol mint ok: {email} "
        f"expires_in={token.get('expires_in')!r} "
        f"userinfo_email={result.email or ''}"
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": str(token.get("id_token") or ""),
        "expires_in": token.get("expires_in"),
        "token_type": token.get("token_type") or "Bearer",
        "backend": "protocol",
    }
