"""Convert CPA xAI auth JSON to sub2api import JSON and keep a combined local export.

The output shape follows GPTSession2CPAandSub2API's sub2api document:
{
  "exported_at": "...",
  "proxies": [],
  "accounts": [ ... ]
}
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = str(token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode("utf-8"))
    except Exception:
        return {}


def _expires_at_ms(access_token: str, expired: str | None = None) -> int | None:
    payload = _jwt_payload(access_token)
    if payload.get("exp") is not None:
        try:
            return int(payload["exp"]) * 1000
        except Exception:
            pass
    if expired:
        try:
            text = str(expired).replace("Z", "+00:00")
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        except Exception:
            return None
    return None


def _email_key(email: str) -> str:
    return (email or "").strip().lower().replace("@", "_").replace(".", "_")


def cpa_xai_to_sub2api_account(cpa: dict[str, Any], *, source: str = "cpa_xai") -> dict[str, Any]:
    access_token = str(cpa.get("access_token") or "")
    refresh_token = str(cpa.get("refresh_token") or "")
    email = str(cpa.get("email") or "")
    sub = str(cpa.get("sub") or "")
    base_url = str(cpa.get("base_url") or "https://cli-chat-proxy.grok.com/v1")
    expired = str(cpa.get("expired") or "")
    expires_at = _expires_at_ms(access_token, expired)
    name = email or sub or "xAI Account"
    account = {
        "name": name,
        "platform": "xai",
        "type": "oauth",
        "auto_pause_on_expired": True if expires_at else None,
        "expires_at": expires_at,
        "concurrency": 10,
        "priority": 1,
        "credentials": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": cpa.get("id_token") or "",
            "token_type": cpa.get("token_type") or "Bearer",
            "expires_in": cpa.get("expires_in"),
            "expired": expired,
            "email": email,
            "sub": sub,
            "base_url": base_url,
            "token_endpoint": cpa.get("token_endpoint") or "https://auth.x.ai/oauth2/token",
            "redirect_uri": cpa.get("redirect_uri") or "http://127.0.0.1:56121/callback",
            "headers": cpa.get("headers") or {},
        },
        "extra": {
            "email": email,
            "email_key": _email_key(email),
            "name": name,
            "auth_provider": "xai",
            "source": source,
            "last_refresh": cpa.get("last_refresh") or _now_iso(),
        },
    }
    # Strip None values recursively while preserving empty strings for tokens.
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if x is not None}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return strip(account)


def build_sub2api_document(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"exported_at": _now_iso(), "proxies": [], "accounts": accounts}


def convert_cpa_file(cpa_path: str | Path, out_dir: str | Path | None = None) -> tuple[Path, dict[str, Any]]:
    cpa_path = Path(cpa_path).expanduser().resolve()
    cpa = json.loads(cpa_path.read_text(encoding="utf-8-sig"))
    account = cpa_xai_to_sub2api_account(cpa, source="cpa_xai")
    doc = build_sub2api_document([account])
    out_dir = Path(out_dir or cpa_path.parent.parent / "sub2api_exports").expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"sub2api-{cpa_path.stem}.json"
    out_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_file, doc


def rebuild_combined(cpa_dir: str | Path, out_file: str | Path) -> Path:
    cpa_dir = Path(cpa_dir).expanduser().resolve()
    accounts: list[dict[str, Any]] = []
    for p in sorted(cpa_dir.glob("xai-*.json")):
        try:
            cpa = json.loads(p.read_text(encoding="utf-8-sig"))
            accounts.append(cpa_xai_to_sub2api_account(cpa, source="cpa_xai"))
        except Exception:
            continue
    out_file = Path(out_file).expanduser().resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(build_sub2api_document(accounts), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_file


def export_after_cpa_result(result: dict[str, Any], config: dict[str, Any] | None = None, log_callback=None) -> dict[str, Any]:
    cfg = config or {}
    log = log_callback or (lambda m: None)
    if not cfg.get("sub2api_export_enabled", True):
        return {"ok": False, "skipped": True, "reason": "disabled"}
    cpa_path = result.get("path") or result.get("cpa_path")
    if not cpa_path:
        return {"ok": False, "error": "missing cpa path"}
    reg_dir = Path(__file__).resolve().parent
    out_dir = Path(cfg.get("sub2api_export_dir") or (reg_dir / "sub2api_exports"))
    if not out_dir.is_absolute():
        out_dir = (reg_dir / out_dir).resolve()
    single_path, _doc = convert_cpa_file(cpa_path, out_dir=out_dir)
    cpa_dir = Path(cfg.get("cpa_auth_dir") or (reg_dir / "cpa_auths"))
    if not cpa_dir.is_absolute():
        cpa_dir = (reg_dir / cpa_dir).resolve()
    combined_path = Path(cfg.get("sub2api_combined_file") or (out_dir / "sub2api-accounts.json"))
    if not combined_path.is_absolute():
        combined_path = (reg_dir / combined_path).resolve()
    rebuild_combined(cpa_dir, combined_path)
    log(f"[sub2api] export -> {single_path}")
    log(f"[sub2api] combined -> {combined_path}")
    return {"ok": True, "path": str(single_path), "combined_path": str(combined_path)}
