"""Provision Cloudflare Email Routing DNS (apex + wildcard MX/SPF/DKIM/DMARC).

Requires a token with Zone.DNS Edit on the target zone.

Env:
  CF_API_TOKEN   Cloudflare API token
  CF_ZONE_ID     optional; auto-resolved from --domain when omitted

Example:
  set CF_API_TOKEN=...
  python scripts/setup_cf_email_domain.py --domain 5673214.xyz
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Cloudflare Email Routing MX targets (same for all zones).
MX_TARGETS = [
    (10, "route1.mx.cloudflare.net"),
    (20, "route2.mx.cloudflare.net"),
    (30, "route3.mx.cloudflare.net"),
]

# Account-level CF Email Routing DKIM public key (shared across sibling zones).
CF_DKIM = (
    "v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"
    "iweykoi+o48IOGuP7GR3X0MOExCUDY/BCRHoWBnh3rChl7WhdyCxW3jgq1daEjPPqoi7sJvdg5hEQVsgVRQP4DcnQDVjGMbASQtrY4WmB1VebF+RPJB2ECPsEDTpeiI5ZyUAwJaVX7r6bznU67g7LvFq35yIo4sdlmtZGV+i0H4cpYH9+3JJ78k"
    "m4KXwaf9xUJCWF6nxeD+qG6Fyruw1Qlbds2r85U9dkNDVAS3gioCvELryh1TxKGiVTkg4wqHTyHfWsp7KD3WQHYJn0RyfJJu6YEmL77zonn7p2SRMvTMP3ZEXibnC9gz3nnhR6wcYL8Q7zXypKTMD58bTixDSJwIDAQAB"
)


def api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}
        raise SystemExit(f"HTTP {exc.code} {method} {path}: {payload}") from exc


def resolve_zone(token: str, domain: str) -> str:
    data = api("GET", f"/zones?name={domain}", token)
    results = data.get("result") or []
    if not results:
        raise SystemExit(f"Zone not found for {domain}")
    return str(results[0]["id"])


def list_records(token: str, zone_id: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        data = api(
            "GET",
            f"/zones/{zone_id}/dns_records?per_page=100&page={page}",
            token,
        )
        batch = data.get("result") or []
        out.extend(batch)
        info = data.get("result_info") or {}
        if page >= int(info.get("total_pages") or 1):
            break
        page += 1
    return out


def has_record(records: list[dict], *, type_: str, name: str, content: str | None = None) -> bool:
    name = name.rstrip(".").lower()
    for r in records:
        if str(r.get("type") or "").upper() != type_.upper():
            continue
        rname = str(r.get("name") or "").rstrip(".").lower()
        if rname != name:
            continue
        if content is None:
            return True
        if str(r.get("content") or "").strip().strip('"') == content.strip().strip('"'):
            return True
    return False


def create_record(token: str, zone_id: str, payload: dict) -> dict:
    return api("POST", f"/zones/{zone_id}/dns_records", token, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, help="Base domain e.g. 5673214.xyz")
    parser.add_argument("--zone-id", default="", help="Optional zone id")
    parser.add_argument(
        "--token",
        default=os.environ.get("CF_API_TOKEN", ""),
        help="API token (or CF_API_TOKEN env)",
    )
    args = parser.parse_args()
    domain = args.domain.strip().lower().lstrip("@")
    token = (args.token or "").strip()
    if not token:
        print("CF_API_TOKEN / --token required", file=sys.stderr)
        return 2

    zone_id = (args.zone_id or "").strip() or resolve_zone(token, domain)
    print(f"[*] zone {domain} id={zone_id}")

    # Probe email routing (may fail without Email Routing permissions).
    for path in (
        f"/zones/{zone_id}/email/routing",
        f"/zones/{zone_id}/email/routing/dns",
        f"/zones/{zone_id}/email/routing/rules",
    ):
        try:
            data = api("GET", path, token)
            print(f"[*] GET {path} ok keys={list((data.get('result') or {}).keys()) if isinstance(data.get('result'), dict) else type(data.get('result')).__name__}")
        except SystemExit as exc:
            print(f"[!] GET {path} failed: {exc}")

    records = list_records(token, zone_id)
    created = 0
    skipped = 0

    desired: list[dict] = []
    for host in (domain, f"*.{domain}"):
        for prio, target in MX_TARGETS:
            desired.append(
                {
                    "type": "MX",
                    "name": host,
                    "content": target,
                    "priority": prio,
                    "ttl": 1,
                    "proxied": False,
                }
            )
    desired.append(
        {
            "type": "TXT",
            "name": domain,
            "content": "v=spf1 include:_spf.mx.cloudflare.net ~all",
            "ttl": 1,
        }
    )
    desired.append(
        {
            "type": "TXT",
            "name": f"cf2024-1._domainkey.{domain}",
            "content": CF_DKIM,
            "ttl": 1,
        }
    )
    desired.append(
        {
            "type": "TXT",
            "name": f"_dmarc.{domain}",
            "content": "v=DMARC1; p=none;",
            "ttl": 1,
        }
    )

    for payload in desired:
        name = payload["name"]
        type_ = payload["type"]
        content = payload["content"]
        # CF stores FQDN names; match either short or FQDN.
        match_names = {name.lower(), f"{name}.{domain}".lower() if not name.endswith(domain) else name.lower()}
        exists = False
        for r in records:
            if str(r.get("type")).upper() != type_:
                continue
            rname = str(r.get("name") or "").rstrip(".").lower()
            if rname not in match_names and rname != name.lower():
                # wildcard / apex normalized
                if type_ == "MX":
                    if name.startswith("*.") and rname == f"*.{domain}":
                        pass
                    elif name == domain and rname == domain:
                        pass
                    else:
                        continue
                else:
                    continue
            rc = str(r.get("content") or "").strip().strip('"')
            if rc == content.strip().strip('"') or (
                type_ == "MX" and rc.lower() == content.lower()
            ):
                if type_ != "MX" or int(r.get("priority") or -1) == int(payload.get("priority") or -1):
                    exists = True
                    break
        if exists:
            print(f"[=] skip {type_} {name} -> {content[:60]}")
            skipped += 1
            continue
        # Re-check with helper
        fqdn = name if name.endswith(domain) or name.startswith("*.") else f"{name}.{domain}"
        if has_record(records, type_=type_, name=fqdn if not name.startswith("*.") else f"*.{domain}", content=content if type_ != "MX" else content):
            # also check priority for MX via list scan already above
            pass

        try:
            res = create_record(token, zone_id, payload)
        except SystemExit as exc:
            print(f"[!] create failed {type_} {name}: {exc}")
            continue
        if not res.get("success"):
            print(f"[!] create failed {type_} {name}: {res}")
            continue
        created += 1
        print(f"[+] created {type_} {name} prio={payload.get('priority')} -> {content[:72]}")
        # refresh local cache
        records.append(res.get("result") or payload)

    print(f"\n[*] done created={created} skipped={skipped}")
    print(
        "\nNext (requires broader token / dashboard):\n"
        "  1) Email Routing → Enable for this zone\n"
        "  2) Catch-all → Send to Worker cloudflare_temp_email\n"
        "  3) Worker vars: add domain to DOMAINS + RANDOM_SUBDOMAIN_DOMAINS\n"
        "  4) config.json defaultDomains includes the domain\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
