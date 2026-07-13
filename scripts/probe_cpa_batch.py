#!/usr/bin/env python3
"""Batch-probe local CPA auth files (models + /responses + optional billing).

Usage (from repo root):
  uv run python scripts/probe_cpa_batch.py
  uv run python scripts/probe_cpa_batch.py --workers 12 --limit 0
"""
from __future__ import annotations

import argparse
import base64
import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from curl_cffi import requests as creq  # noqa: E402
from grok_register.cpa_xai.schema import DEFAULT_CLIENT_HEADERS  # noqa: E402


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        seg = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except Exception:
        return {}


def _val(x: Any) -> Any:
    if isinstance(x, dict) and "val" in x:
        return x.get("val")
    return x


def probe_one(path: Path, *, timeout: float = 25.0) -> dict[str, Any]:
    row: dict[str, Any] = {
        "file": path.name,
        "email": "",
        "ok_file": False,
        "expired": None,
        "ttl_sec": None,
        "scope_cli": False,
        "models": None,
        "responses": None,
        "billing_monthly": None,
        "billing_used": None,
        "billing_cap": None,
        "error": "",
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        row["error"] = f"read_json: {exc}"
        return row

    if not isinstance(data, dict):
        row["error"] = "not_object"
        return row

    row["ok_file"] = True
    row["email"] = str(data.get("email") or "")
    token = str(data.get("access_token") or "").strip()
    if not token:
        row["error"] = "missing_access_token"
        return row

    base = str(data.get("base_url") or "https://cli-chat-proxy.grok.com/v1").rstrip("/")
    pl = _jwt_payload(token)
    exp = int(pl.get("exp") or 0)
    now = int(time.time())
    if exp:
        row["ttl_sec"] = exp - now
        row["expired"] = exp < now
    row["scope_cli"] = "grok-cli:access" in str(pl.get("scope") or "")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        **DEFAULT_CLIENT_HEADERS,
    }
    headers_json = {**headers, "Content-Type": "application/json"}

    # expired tokens: still probe once so status reflects reality
    try:
        r = creq.get(base + "/models", headers=headers, impersonate="chrome131", timeout=timeout)
        row["models"] = int(r.status_code)
    except Exception as exc:
        row["models"] = 0
        row["error"] = f"models: {exc}"

    try:
        r = creq.post(
            base + "/responses",
            headers=headers_json,
            json={
                "model": "grok-4.5",
                "stream": False,
                "input": "Reply with exactly MINT_OK",
                "reasoning": {"effort": "low"},
            },
            impersonate="chrome131",
            timeout=timeout,
        )
        row["responses"] = int(r.status_code)
        if r.status_code != 200:
            # keep short error code only
            try:
                body = r.json()
                row["responses_code"] = body.get("code") or ""
            except Exception:
                row["responses_code"] = ""
    except Exception as exc:
        row["responses"] = 0
        if not row["error"]:
            row["error"] = f"responses: {exc}"

    try:
        r = creq.get(base + "/billing", headers=headers, impersonate="chrome131", timeout=timeout)
        if r.status_code == 200:
            j = r.json()
            cfg = j.get("config") or j
            row["billing_monthly"] = _val(cfg.get("monthlyLimit"))
            row["billing_used"] = _val(cfg.get("used"))
            row["billing_cap"] = _val(cfg.get("onDemandCap"))
        else:
            row["billing_monthly"] = f"http_{r.status_code}"
    except Exception as exc:
        row["billing_monthly"] = f"err:{type(exc).__name__}"

    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch probe local CPA auth files")
    ap.add_argument(
        "--auth-dir",
        default=str(ROOT / "output" / "cpa_auths"),
        help="directory with xai-*.json",
    )
    ap.add_argument("--workers", type=int, default=12, help="concurrency (default 12)")
    ap.add_argument("--limit", type=int, default=0, help="0 = all files")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument(
        "--out",
        default="",
        help="jsonl output path (default output/cpa_probe_YYYYMMDD_HHMMSS.jsonl)",
    )
    args = ap.parse_args()

    auth_dir = Path(args.auth_dir)
    files = sorted(auth_dir.glob("xai-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if args.limit and args.limit > 0:
        files = files[: args.limit]
    if not files:
        print(f"no files in {auth_dir}")
        return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else (ROOT / "output" / f"cpa_probe_{ts}.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"auth_dir={auth_dir}")
    print(f"files={len(files)} workers={args.workers} timeout={args.timeout}")
    print(f"out={out_path}")
    print(f"start={datetime.now().isoformat(timespec='seconds')}")

    models_c: Counter[int] = Counter()
    resp_c: Counter[int] = Counter()
    chat_ok = chat_403 = chat_other = expired_n = scope_ok_n = 0
    billing_nonzero = 0
    done = 0
    t0 = time.time()

    with out_path.open("w", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(probe_one, p, timeout=args.timeout): p for p in files}
        for fut in as_completed(futs):
            row = fut.result()
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            done += 1

            ms = row.get("models")
            rs = row.get("responses")
            if isinstance(ms, int):
                models_c[ms] += 1
            if isinstance(rs, int):
                resp_c[rs] += 1
            if rs == 200:
                chat_ok += 1
            elif rs == 403:
                chat_403 += 1
            else:
                chat_other += 1
            if row.get("expired"):
                expired_n += 1
            if row.get("scope_cli"):
                scope_ok_n += 1
            bm, bc = row.get("billing_monthly"), row.get("billing_cap")
            try:
                if (bm not in (0, None, "0", 0.0)) or (bc not in (0, None, "0", 0.0)):
                    if not isinstance(bm, str):
                        billing_nonzero += 1
            except Exception:
                pass

            if done % 25 == 0 or done == len(files):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                print(
                    f"progress {done}/{len(files)} "
                    f"chat200={chat_ok} chat403={chat_403} other={chat_other} "
                    f"{rate:.1f}/s",
                    flush=True,
                )

    # summary markdown-friendly
    summary = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "auth_dir": str(auth_dir),
        "total": len(files),
        "models_status": dict(sorted(models_c.items(), key=lambda x: str(x[0]))),
        "responses_status": dict(sorted(resp_c.items(), key=lambda x: str(x[0]))),
        "chat_ok_200": chat_ok,
        "chat_403": chat_403,
        "chat_other": chat_other,
        "token_expired": expired_n,
        "scope_has_grok_cli_access": scope_ok_n,
        "billing_nonzero_count": billing_nonzero,
        "elapsed_sec": round(time.time() - t0, 1),
        "detail_jsonl": str(out_path),
    }
    summary_path = out_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n========== SUMMARY ==========")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"summary_file={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
