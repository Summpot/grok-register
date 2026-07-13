#!/usr/bin/env python3
"""Refresh local CPA xai-*.json access tokens, then write back files.

Usage:
  uv run python scripts/refresh_cpa_batch.py
  uv run python scripts/refresh_cpa_batch.py --workers 8 --only-expired
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from grok_register.cpa_xai.schema import (  # noqa: E402
    CLIENT_ID,
    DEFAULT_TOKEN_ENDPOINT,
    build_cpa_xai_auth,
)
from grok_register.cpa_xai.writer import write_cpa_xai_auth  # noqa: E402
from grok_register.xconsole_client.xai_oauth import refresh_access_token  # noqa: E402


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        seg = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except Exception:
        return {}


def _is_expired(access_token: str, skew_sec: int = 120) -> bool:
    pl = _jwt_payload(access_token)
    exp = int(pl.get("exp") or 0)
    if not exp:
        return True
    return exp <= int(time.time()) + skew_sec


def refresh_one(path: Path, *, proxy: str = "") -> dict[str, Any]:
    row: dict[str, Any] = {
        "file": path.name,
        "email": "",
        "ok": False,
        "skipped": False,
        "reason": "",
        "error": "",
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        row["error"] = f"read: {exc}"
        return row
    if not isinstance(data, dict):
        row["error"] = "not_object"
        return row

    email = str(data.get("email") or "")
    row["email"] = email
    refresh = str(data.get("refresh_token") or "").strip()
    access = str(data.get("access_token") or "").strip()
    if not refresh:
        row["error"] = "missing_refresh_token"
        return row

    token_endpoint = str(data.get("token_endpoint") or DEFAULT_TOKEN_ENDPOINT).strip()
    # refresh_access_token uses module TOKEN_ENDPOINT; still pass via monkey if needed.
    try:
        # Prefer library helper (uses auth.x.ai/oauth2/token)
        tok = refresh_access_token(refresh, client_id=CLIENT_ID, proxy=proxy)
    except Exception as exc:
        row["error"] = str(exc)[:400]
        return row

    new_access = str(tok.get("access_token") or "").strip()
    new_refresh = str(tok.get("refresh_token") or refresh).strip()
    if not new_access:
        row["error"] = "refresh returned empty access_token"
        return row

    base_url = str(data.get("base_url") or "https://cli-chat-proxy.grok.com/v1")
    try:
        payload = build_cpa_xai_auth(
            email=email or str(data.get("email") or ""),
            access_token=new_access,
            refresh_token=new_refresh,
            id_token=str(tok.get("id_token") or data.get("id_token") or "") or None,
            expires_in=tok.get("expires_in") or data.get("expires_in"),
            sub=str(data.get("sub") or "") or None,
            base_url=base_url,
            token_endpoint=token_endpoint or DEFAULT_TOKEN_ENDPOINT,
            headers=data.get("headers") if isinstance(data.get("headers"), dict) else None,
            disabled=bool(data.get("disabled", False)),
        )
        # keep original filename
        write_cpa_xai_auth(path.parent, payload, filename=path.name)
    except Exception as exc:
        # fallback: patch fields in place
        try:
            data["access_token"] = new_access
            data["refresh_token"] = new_refresh
            if tok.get("id_token"):
                data["id_token"] = tok.get("id_token")
            if tok.get("expires_in") is not None:
                data["expires_in"] = tok.get("expires_in")
            # recompute expired from jwt if possible
            pl = _jwt_payload(new_access)
            if pl.get("exp"):
                data["expired"] = datetime.fromtimestamp(int(pl["exp"]), tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            data["last_refresh"] = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as exc2:
            row["error"] = f"write: {exc}; fallback: {exc2}"
            return row

    row["ok"] = True
    row["reason"] = "refreshed"
    row["was_expired"] = _is_expired(access) if access else True
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh local CPA auth access tokens")
    ap.add_argument("--auth-dir", default=str(ROOT / "output" / "cpa_auths"))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-expired", action="store_true", default=True)
    ap.add_argument("--all", action="store_true", help="refresh even if not expired")
    ap.add_argument("--proxy", default="")
    args = ap.parse_args()
    only_expired = not args.all

    auth_dir = Path(args.auth_dir)
    files = sorted(auth_dir.glob("xai-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    selected: list[Path] = []
    skipped_fresh = 0
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            access = str(data.get("access_token") or "")
            if only_expired and access and not _is_expired(access):
                skipped_fresh += 1
                continue
            selected.append(p)
        except Exception:
            selected.append(p)
    if args.limit and args.limit > 0:
        selected = selected[: args.limit]

    print(f"auth_dir={auth_dir}")
    print(f"total_files={len(files)} selected={len(selected)} skipped_fresh={skipped_fresh}")
    print(f"workers={args.workers} only_expired={only_expired}")
    if not selected:
        print("nothing to refresh")
        return 0

    ok = fail = 0
    errors: list[str] = []
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(refresh_one, p, proxy=args.proxy): p for p in selected}
        for fut in as_completed(futs):
            row = fut.result()
            done += 1
            if row.get("ok"):
                ok += 1
            else:
                fail += 1
                if len(errors) < 15:
                    errors.append(f"{row.get('file')}: {row.get('error')}")
            if done % 50 == 0 or done == len(selected):
                print(
                    f"progress {done}/{len(selected)} ok={ok} fail={fail} "
                    f"{done / max(time.time() - t0, 0.1):.1f}/s",
                    flush=True,
                )

    print("\n========== REFRESH SUMMARY ==========")
    print(
        json.dumps(
            {
                "selected": len(selected),
                "ok": ok,
                "fail": fail,
                "skipped_fresh": skipped_fresh,
                "elapsed_sec": round(time.time() - t0, 1),
                "sample_errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
