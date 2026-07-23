#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Managed SSH keypair for DO egress droplets (local file + account key).

Generates an ed25519 key under state_dir/ssh/, uploads the public half to
DigitalOcean, and returns the key id for droplet create + private path for
``ssh -i`` readiness probes.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from pathlib import Path
from typing import Callable

from grok_register.do_egress.api import DOError, DigitalOceanClient
from grok_register.do_egress.settings import DoEgressSettings
from grok_register.paths import PROJECT_ROOT

LogFn = Callable[[str], None]

DEFAULT_KEY_NAME = "grok-reg-egress"
_PRIV_NAME = "id_ed25519"
_PUB_NAME = "id_ed25519.pub"


def _state_root(settings: DoEgressSettings) -> Path:
    p = Path(settings.state_dir)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def managed_key_paths(settings: DoEgressSettings) -> tuple[Path, Path]:
    """Return (private_key_path, public_key_path)."""
    override = str(getattr(settings, "ssh_identity_file", "") or "").strip()
    if override:
        priv = Path(override)
        if not priv.is_absolute():
            priv = PROJECT_ROOT / priv
        pub = Path(str(priv) + ".pub")
        return priv, pub
    base = _state_root(settings) / "ssh"
    return base / _PRIV_NAME, base / _PUB_NAME


def _log(msg: str, log: LogFn | None) -> None:
    if log:
        log(msg)
    else:
        try:
            print(msg, flush=True)
        except Exception:
            pass


def _harden_private_key_acl(priv: Path) -> None:
    """OpenSSH on Windows rejects world-readable private keys; tighten ACL."""
    if os.name != "nt" or not priv.is_file():
        if os.name != "nt" and priv.is_file():
            try:
                os.chmod(priv, 0o600)
            except Exception:
                pass
        return
    try:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        if not user:
            return
        # Remove inheritance, grant only current user read
        subprocess.run(
            ["icacls", str(priv), "/inheritance:r"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        subprocess.run(
            ["icacls", str(priv), "/grant:r", f"{user}:(R)"],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except Exception:
        pass


def generate_ed25519_keypair(priv: Path, pub: Path, *, comment: str = DEFAULT_KEY_NAME) -> None:
    """Create a new ed25519 keypair via OpenSSH ``ssh-keygen`` (no passphrase)."""
    priv.parent.mkdir(parents=True, exist_ok=True)
    for p in (priv, pub):
        try:
            if p.is_file():
                p.unlink()
        except Exception:
            pass

    cmd = [
        "ssh-keygen",
        "-t",
        "ed25519",
        "-f",
        str(priv),
        "-N",
        "",
        "-C",
        comment,
        "-q",
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "ssh-keygen 未找到：请安装 OpenSSH 客户端（Windows 可选功能 / Git for Windows）"
        ) from exc
    if r.returncode != 0 or not priv.is_file() or not pub.is_file():
        err = (r.stderr or r.stdout or "").strip()[:300]
        raise RuntimeError(f"ssh-keygen 失败 rc={r.returncode}: {err}")
    _harden_private_key_acl(priv)


def ensure_local_keypair(
    settings: DoEgressSettings,
    *,
    log: LogFn | None = None,
) -> tuple[Path, Path]:
    """Ensure private/public key files exist; generate if missing."""
    priv, pub = managed_key_paths(settings)
    if priv.is_file() and pub.is_file() and priv.stat().st_size > 0 and pub.stat().st_size > 0:
        _harden_private_key_acl(priv)
        return priv, pub
    comment = str(getattr(settings, "ssh_key_name", "") or DEFAULT_KEY_NAME).strip() or DEFAULT_KEY_NAME
    _log(f"[egress] generating managed SSH key: {priv}", log)
    generate_ed25519_keypair(priv, pub, comment=comment)
    _log(f"[egress] SSH public key written: {pub}", log)
    return priv, pub


def _normalize_pubkey(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _pubkey_blob(text: str) -> str:
    """ssh-ed25519 AAAA... comment → type + blob (ignore comment)."""
    parts = _normalize_pubkey(text).split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return _normalize_pubkey(text)


def find_or_create_do_key(
    client: DigitalOceanClient,
    *,
    name: str,
    public_key: str,
    log: LogFn | None = None,
) -> int:
    """Return DO ssh key id for this public key (create if needed)."""
    want = _pubkey_blob(public_key)
    if not want:
        raise RuntimeError("empty public key")

    existing = client.list_ssh_keys()
    for k in existing:
        got = _pubkey_blob(str(k.get("public_key") or ""))
        if got and got == want:
            kid = int(k.get("id") or 0)
            if kid:
                _log(
                    f"[egress] reusing DO SSH key id={kid} name={k.get('name')!r}",
                    log,
                )
                return kid

    key_name = (name or DEFAULT_KEY_NAME).strip() or DEFAULT_KEY_NAME
    # Avoid name collision with a different key
    taken = {str(k.get("name") or "") for k in existing}
    if key_name in taken:
        key_name = f"{key_name}-{secrets.token_hex(3)}"

    try:
        created = client.create_ssh_key(name=key_name, public_key=public_key.strip())
    except DOError as exc:
        # Race: another process created same key
        if exc.status in (422, 409):
            for k in client.list_ssh_keys():
                if _pubkey_blob(str(k.get("public_key") or "")) == want:
                    return int(k["id"])
        raise
    kid = int((created or {}).get("id") or 0)
    if not kid:
        raise RuntimeError(f"DO create SSH key returned no id: {created}")
    _log(f"[egress] uploaded DO SSH key id={kid} name={key_name!r}", log)
    return kid


def ensure_managed_ssh(
    client: DigitalOceanClient,
    settings: DoEgressSettings,
    *,
    log: LogFn | None = None,
) -> tuple[list[int | str], Path]:
    """Ensure local key + DO account key. Mutates settings.ssh_identity_file.

    Returns (ssh_key_ids for droplet create, private_key_path).
    """
    priv, pub = ensure_local_keypair(settings, log=log)
    public_text = pub.read_text(encoding="utf-8").strip()
    if not public_text or not re.match(r"^ssh-\w+\s+\S+", public_text):
        raise RuntimeError(f"invalid public key file: {pub}")

    name = str(getattr(settings, "ssh_key_name", "") or DEFAULT_KEY_NAME).strip() or DEFAULT_KEY_NAME
    managed_id = find_or_create_do_key(
        client, name=name, public_key=public_text, log=log
    )

    # Always use managed private key for probes
    settings.ssh_identity_file = str(priv)

    ids: list[int | str] = [managed_id]
    for extra in settings.ssh_key_ids or []:
        if extra in (None, ""):
            continue
        if extra not in ids and str(extra) != str(managed_id):
            ids.append(extra)
    return ids, priv
