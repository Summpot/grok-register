"""Add a base domain to cloudflare_temp_email DOMAINS + RANDOM_SUBDOMAIN_DOMAINS."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TOKEN = (os.environ.get("CF_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
ACCOUNT = (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip()
SCRIPT = os.environ.get("CF_WORKER_SCRIPT", "cloudflare_temp_email")
DOMAIN = (sys.argv[1] if len(sys.argv) > 1 else "5673214.xyz").strip().lower()

if not TOKEN or not ACCOUNT:
    print("CF_API_TOKEN and CLOUDFLARE_ACCOUNT_ID required", file=sys.stderr)
    sys.exit(2)


def api(method: str, path: str, body=None, *, multipart_settings: dict | None = None):
    headers = {"Authorization": f"Bearer {TOKEN}"}
    data = None
    if multipart_settings is not None:
        boundary = "----GrokRegBoundary7MA4YWxkTrZu0gW"
        payload = json.dumps(multipart_settings)
        data = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="settings"; filename="blob"\r\n'
            f"Content-Type: application/json\r\n\r\n"
            f"{payload}\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        raise RuntimeError(f"{method} {path} HTTP {e.code}: {payload}") from e


def upsert_json_list(bindings: list, name: str, extra: str) -> None:
    for b in bindings:
        if b.get("name") == name and b.get("type") == "json":
            vals = list(b.get("json") or [])
            if extra not in vals:
                vals.append(extra)
            b["json"] = vals
            print(f"[*] {name} -> {vals}")
            return
    bindings.append({"name": name, "type": "json", "json": [extra]})
    print(f"[+] created {name} -> {[extra]}")


def main() -> int:
    settings = api("GET", f"/accounts/{ACCOUNT}/workers/scripts/{SCRIPT}/settings")
    result = settings.get("result") or {}
    bindings = list(result.get("bindings") or [])
    print(f"[*] loaded {len(bindings)} bindings from {SCRIPT}")

    upsert_json_list(bindings, "DOMAINS", DOMAIN)
    upsert_json_list(bindings, "RANDOM_SUBDOMAIN_DOMAINS", DOMAIN)

    body = {
        "compatibility_date": result.get("compatibility_date"),
        "compatibility_flags": result.get("compatibility_flags") or [],
        "usage_model": result.get("usage_model") or "standard",
        "bindings": bindings,
        "tags": result.get("tags") or [],
    }
    if result.get("logpush") is not None:
        body["logpush"] = result.get("logpush")

    try:
        out = api(
            "PATCH",
            f"/accounts/{ACCOUNT}/workers/scripts/{SCRIPT}/settings",
            multipart_settings=body,
        )
        print(f"[+] PATCH settings success={out.get('success')} errors={out.get('errors')}")
        for b in (out.get("result") or {}).get("bindings") or []:
            if b.get("name") in ("DOMAINS", "RANDOM_SUBDOMAIN_DOMAINS"):
                print(f"    {b.get('name')}: {b.get('json')}")
        return 0 if out.get("success") else 1
    except Exception as exc:
        print(f"[!] PATCH failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
