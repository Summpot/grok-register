"""Local config sanity check (optional; may hit real remote services).

Reads project-root config.json only. Does not hardcode private endpoints.
Run from repo root, or via scripts/verify_config_safe.ps1.

Examples:
  uv run python scripts/verify_config_safe.py
  uv run python scripts/verify_config_safe.py --probe-mail
  uv run python scripts/verify_config_safe.py --probe-remote
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"


def mask(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safe local config verification (no hardcoded secrets/hosts)."
    )
    parser.add_argument(
        "--probe-mail",
        action="store_true",
        help="Optionally probe the configured email provider (real network).",
    )
    parser.add_argument(
        "--probe-remote",
        action="store_true",
        help="Optionally probe remote grok2api when auto-add-remote is enabled.",
    )
    args = parser.parse_args()

    if not CONFIG_PATH.is_file():
        print(f"[!] missing {CONFIG_PATH} — copy config.example.json first", file=sys.stderr)
        return 1

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    print("config:", CONFIG_PATH.resolve())

    keys = [
        "email_provider",
        "register_count",
        "max_mail_retry",
        "code_poll_timeout",
        "grok2api_auto_add_local",
        "grok2api_auto_add_remote",
        "grok2api_auto_add_build",
        "grok2api_remote_base",
        "grok2api_remote_mode",
    ]
    for key in keys:
        print(f"{key}:", config.get(key))

    print("cloudflare_api_key:", mask(config.get("cloudflare_api_key")))
    print("yyds_api_key:", mask(config.get("yyds_api_key")))
    print("grok2api_remote_app_key:", mask(config.get("grok2api_remote_app_key")))
    print("grok2api_remote_password:", mask(config.get("grok2api_remote_password")))

    if args.probe_mail:
        _probe_mail(config)
    else:
        print("mail probe: skipped (pass --probe-mail to hit configured email API)")

    if args.probe_remote:
        _probe_remote(config)
    else:
        print("remote probe: skipped (pass --probe-remote to hit configured grok2api)")

    print("VERIFY OK")
    return 0


def _probe_mail(config: dict) -> None:
    try:
        import requests
    except ImportError:
        print("[!] requests not installed; skip mail probe", file=sys.stderr)
        return

    provider = str(config.get("email_provider") or "").strip().lower()
    if provider == "yyds":
        base = str(config.get("yyds_api_base") or "").strip().rstrip("/")
        api_key = config.get("yyds_api_key") or ""
        if not base:
            print("YYDS probe: skipped (set yyds_api_base in config.json to enable)")
            return
        url = f"{base}/v1/domains"
        response = requests.get(url, headers={"X-API-Key": api_key}, timeout=20)
        ok = False
        try:
            ok = bool(response.json().get("success"))
        except Exception:
            ok = False
        print("YYDS HTTP:", response.status_code, "success:", ok)
        return

    if provider == "cloudflare":
        base = str(config.get("cloudflare_api_base") or "").strip().rstrip("/")
        if not base:
            print("Cloudflare probe: skipped (cloudflare_api_base empty)")
            return
        path = str(config.get("cloudflare_path_domains") or "/api/domains")
        url = f"{base}{path if path.startswith('/') else '/' + path}"
        headers: dict[str, str] = {}
        key = str(config.get("cloudflare_api_key") or "").strip()
        mode = str(config.get("cloudflare_auth_mode") or "none").strip().lower()
        if key and mode == "x-admin-auth":
            headers["x-admin-auth"] = key
        elif key and mode == "x-api-key":
            headers["X-API-Key"] = key
        elif key and mode not in ("", "none"):
            headers["Authorization"] = f"Bearer {key}"
        response = requests.get(url, headers=headers, timeout=20)
        print("Cloudflare domains HTTP:", response.status_code)
        return

    print(f"mail probe: no probe implemented for email_provider={provider!r}")


def _probe_remote(config: dict) -> None:
    try:
        import requests
    except ImportError:
        print("[!] requests not installed; skip remote probe", file=sys.stderr)
        return

    if not config.get("grok2api_auto_add_remote"):
        print("grok2api probe: skipped (grok2api_auto_add_remote is false)")
        return
    base = str(config.get("grok2api_remote_base") or "").strip().rstrip("/")
    app_key = config.get("grok2api_remote_app_key")
    if not base or not app_key:
        print("grok2api probe: skipped (remote_base or app_key empty)")
        return
    response = requests.get(base + "/admin/api/tokens", params={"app_key": app_key}, timeout=15)
    print("grok2api HTTP:", response.status_code)


if __name__ == "__main__":
    raise SystemExit(main())
