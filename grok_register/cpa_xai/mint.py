"""High-level: mint CPA xai-*.json for one free registered account."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .browser_confirm import mint_with_browser
from .probe import probe_mini_response, probe_models
from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy
from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth
from .writer import write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def mint_and_export(
    *,
    email: str,
    password: str,
    auth_dir: str | Path,
    page: Any | None = None,
    proxy: str | None = None,
    headless: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    probe: bool = True,
    probe_chat: bool = False,
    browser_timeout_sec: float = 240.0,
    force_standalone: bool = True,
    cookies: Any | None = None,
    sso: str | None = None,
    reuse_browser: bool = True,
    recycle_every: int = 15,
    backend: str = "protocol",
    yescaptcha_key: str = "",
    protocol_debug: bool = False,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Mint OAuth tokens then write CPA file.

    backend:
      - protocol: SSO-first HTTP OAuth (no browser; YesCaptcha only if password login needed)
      - browser: Chromium device-code flow
      - auto: try protocol first when SSO present, else browser; on protocol fail fall back to browser
    """
    log = log or _noop
    email = (email or "").strip()
    if not email or not password:
        return {"ok": False, "email": email, "error": "missing email/password"}

    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    backend_norm = (backend or "protocol").strip().lower()
    if backend_norm not in {"protocol", "browser", "auto"}:
        backend_norm = "protocol"

    log(
        f"mint start: {email} backend={backend_norm} "
        f"proxy={proxy_log_label(resolved) or '(none)'}"
    )

    tokens: dict[str, Any] | None = None
    used_backend = backend_norm
    last_err: str | None = None

    def _try_protocol() -> dict[str, Any]:
        from .protocol_mint import mint_tokens_protocol

        return mint_tokens_protocol(
            email=email,
            password=password,
            sso=sso,
            cookies=cookies,
            proxy=resolved or None,
            yescaptcha_key=yescaptcha_key,
            debug=protocol_debug,
            log=log,
        )

    def _try_browser() -> dict[str, Any]:
        return mint_with_browser(
            email=email,
            password=password,
            page=None if force_standalone else page,
            proxy=resolved or None,
            headless=headless,
            browser_timeout_sec=browser_timeout_sec,
            force_standalone=force_standalone,
            cookies=cookies,
            reuse_browser=reuse_browser,
            recycle_every=recycle_every,
            poll_log=log,
            cancel=cancel,
        )

    sso_present = bool((sso or "").strip())
    if not sso_present and isinstance(cookies, list):
        for c in cookies:
            if isinstance(c, dict) and c.get("name") in ("sso", "sso-rw") and c.get("value"):
                sso_present = True
                break

    try:
        if backend_norm == "browser":
            used_backend = "browser"
            tokens = _try_browser()
        elif backend_norm == "protocol":
            used_backend = "protocol"
            tokens = _try_protocol()
        else:  # auto
            if sso_present:
                try:
                    used_backend = "protocol"
                    tokens = _try_protocol()
                except Exception as pe:  # noqa: BLE001
                    last_err = str(pe)
                    log(f"protocol mint failed, fallback browser: {pe}")
                    used_backend = "browser"
                    tokens = _try_browser()
            else:
                log("auto: no SSO, use browser mint")
                used_backend = "browser"
                tokens = _try_browser()
    except Exception as e:  # noqa: BLE001
        log(f"mint failed: {e}")
        err = str(e)
        if last_err:
            err = f"{err} (prior protocol: {last_err})"
        return {
            "ok": False,
            "email": email,
            "error": err,
            "backend": used_backend,
        }

    payload = build_cpa_xai_auth(
        email=email,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens.get("id_token"),
        expires_in=tokens.get("expires_in"),
        base_url=base_url,
    )
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path} backend={used_backend}")

    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": tokens.get("user_code"),
        "base_url": base_url,
        "proxy": proxy_log_label(resolved),
        "backend": used_backend,
    }

    if probe:
        pr = probe_models(tokens["access_token"], base_url=base_url, proxy=resolved or None)
        result["probe_models"] = pr
        log(
            f"probe models: ok={pr.get('ok')} has_grok_45={pr.get('has_grok_45')} "
            f"ids={pr.get('model_ids')}"
        )
        if not pr.get("has_grok_45"):
            result["ok"] = False
            result["error"] = "token ok but grok-4.5 not listed"
        if probe_chat and pr.get("has_grok_45"):
            ch = probe_mini_response(
                tokens["access_token"], base_url=base_url, proxy=resolved or None
            )
            result["probe_chat"] = ch
            log(f"probe chat: ok={ch.get('ok')} model={ch.get('model')} text={ch.get('text')!r}")
            if not ch.get("ok"):
                result["ok"] = False
                result["error"] = f"chat probe failed: {ch.get('error') or ch.get('status')}"
    return result
