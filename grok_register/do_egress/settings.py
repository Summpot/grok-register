"""Read DO egress settings from the main register config dict."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from grok_register.paths import OUTPUT_DIR, PROJECT_ROOT


def _nested(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("do_egress")
    return raw if isinstance(raw, dict) else {}


def _get(cfg: dict[str, Any], key: str, default: Any = None) -> Any:
    """Prefer nested do_egress.*, then flat do_egress_* / do_* keys."""
    nest = _nested(cfg)
    if key in nest and nest[key] not in (None, ""):
        return nest[key]
    flat = f"do_egress_{key}"
    if flat in cfg and cfg[flat] not in (None, ""):
        return cfg[flat]
    short = f"do_{key}"
    if short in cfg and cfg[short] not in (None, ""):
        return cfg[short]
    return default


@dataclass
class DoEgressSettings:
    token: str = ""
    region: str = "sfo3"  # San Francisco 3
    size: str = "s-1vcpu-512mb-10gb"
    image: str = "ubuntu-24-04-x64"
    ssh_key_ids: list[int | str] = field(default_factory=list)
    # Auto-generated ed25519 under state_dir/ssh/; also set after ensure_managed_ssh.
    ssh_identity_file: str = ""
    # Name when uploading the managed public key to the DO account.
    ssh_key_name: str = "grok-reg-egress"
    droplet_tag: str = "grok-reg-egress"
    name_prefix: str = "reg-egress"
    pool_size: int = 3
    remote_port: int = 8443  # Hysteria2 UDP
    tuic_port: int = 8444  # TUIC UDP
    # 443 looks like HTTPS — often works when high ports are filtered (CN→DO)
    trojan_port: int = 443
    enable_hy2: bool = True
    enable_tuic: bool = True
    enable_trojan: bool = True
    # Deprecated / ignored: all enabled protocols are probed; working ones are
    # shuffled for random selection. Kept for config backward compatibility.
    protocol_prefer: str = ""
    # Primary readiness: SSH ready marker + systemctl (managed key injected on create)
    ssh_probe: bool = True
    singbox_version: str = "1.11.15"
    socks_listen: str = "127.0.0.1"
    socks_base_port: int = 17891
    socks_user: str = ""
    socks_password: str = ""
    state_dir: str = "output/do_egress"
    singbox_exe: str = "sing-box"
    create_timeout_s: int = 180
    # Max seconds to wait for remote sing-box readiness (polled, not fixed sleep)
    ready_wait_s: int = 240
    ready_poll_s: int = 5
    allow_from_cidrs: list[str] = field(default_factory=list)
    rotate_on_disable: bool = True
    auto_reload_local: bool = True

    @property
    def state_path(self) -> Path:
        p = Path(self.state_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p / "nodes.json"

    @property
    def local_config_path(self) -> Path:
        p = Path(self.state_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p / "local-tunnel.json"

    @property
    def pid_path(self) -> Path:
        p = Path(self.state_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p / "local-tunnel.pid"

    @property
    def log_path(self) -> Path:
        p = Path(self.state_dir)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p / "local-tunnel.log"

    def socks_port(self, slot: int) -> int:
        return int(self.socks_base_port) + int(slot)

    def droplet_name(self, slot: int) -> str:
        return f"{self.name_prefix}-{int(slot)}"


def settings_from_config(cfg: dict[str, Any] | None) -> DoEgressSettings:
    cfg = cfg or {}
    nest = _nested(cfg)

    token = (
        (os.environ.get("DIGITALOCEAN_TOKEN") or "").strip()
        or (os.environ.get("DO_TOKEN") or "").strip()
        or str(_get(cfg, "token") or nest.get("token") or cfg.get("digitalocean_token") or "").strip()
    )

    ssh = _get(cfg, "ssh_key_ids", nest.get("ssh_key_ids") or [])
    if not isinstance(ssh, list):
        ssh = [ssh] if ssh else []

    allow = _get(cfg, "allow_from_cidrs", nest.get("allow_from_cidrs") or [])
    if not isinstance(allow, list):
        allow = [allow] if allow else []
    allow = [str(x).strip() for x in allow if str(x).strip()]

    state_dir = str(_get(cfg, "state_dir", nest.get("state_dir") or "output/do_egress"))

    return DoEgressSettings(
        token=token,
        region=str(_get(cfg, "region", nest.get("region") or "sfo3")),
        size=str(_get(cfg, "size", nest.get("size") or "s-1vcpu-512mb-10gb")),
        image=str(_get(cfg, "image", nest.get("image") or "ubuntu-24-04-x64")),
        ssh_key_ids=list(ssh),
        ssh_identity_file=str(
            _get(cfg, "ssh_identity_file", nest.get("ssh_identity_file") or "") or ""
        ).strip(),
        ssh_key_name=str(
            _get(cfg, "ssh_key_name", nest.get("ssh_key_name") or "grok-reg-egress")
            or "grok-reg-egress"
        ).strip()
        or "grok-reg-egress",
        droplet_tag=str(_get(cfg, "droplet_tag", nest.get("droplet_tag") or "grok-reg-egress")),
        name_prefix=str(_get(cfg, "name_prefix", nest.get("name_prefix") or "reg-egress")),
        pool_size=int(_get(cfg, "pool_size", nest.get("pool_size") or 3) or 3),
        remote_port=int(_get(cfg, "remote_port", nest.get("remote_port") or 8443) or 8443),
        tuic_port=int(_get(cfg, "tuic_port", nest.get("tuic_port") or 8444) or 8444),
        trojan_port=int(
            _get(cfg, "trojan_port", nest.get("trojan_port") or 443) or 443
        ),
        enable_hy2=bool(nest.get("enable_hy2", cfg.get("do_egress_enable_hy2", True))),
        enable_tuic=bool(nest.get("enable_tuic", cfg.get("do_egress_enable_tuic", True))),
        enable_trojan=bool(
            nest.get("enable_trojan", cfg.get("do_egress_enable_trojan", True))
        ),
        protocol_prefer=str(
            _get(cfg, "protocol_prefer", nest.get("protocol_prefer") or "") or ""
        )
        .strip()
        .lower(),
        ssh_probe=bool(nest.get("ssh_probe", cfg.get("do_egress_ssh_probe", True))),
        singbox_version=str(
            _get(cfg, "singbox_version", nest.get("singbox_version") or "1.11.15")
        ),
        socks_listen=str(_get(cfg, "socks_listen", nest.get("socks_listen") or "127.0.0.1")),
        socks_base_port=int(
            _get(cfg, "socks_base_port", nest.get("socks_base_port") or 17891) or 17891
        ),
        socks_user=str(_get(cfg, "socks_user", nest.get("socks_user") or "") or ""),
        socks_password=str(
            _get(cfg, "socks_password", nest.get("socks_password") or "") or ""
        ),
        state_dir=state_dir,
        singbox_exe=str(_get(cfg, "singbox_exe", nest.get("singbox_exe") or "sing-box")),
        create_timeout_s=int(
            _get(cfg, "create_timeout_s", nest.get("create_timeout_s") or 180) or 180
        ),
        ready_wait_s=int(
            _get(cfg, "ready_wait_s", nest.get("ready_wait_s") or 240) or 240
        ),
        ready_poll_s=int(_get(cfg, "ready_poll_s", nest.get("ready_poll_s") or 5) or 5),
        allow_from_cidrs=allow,
        rotate_on_disable=bool(
            nest.get("rotate_on_disable", cfg.get("do_egress_rotate_on_disable", True))
        ),
        auto_reload_local=bool(
            nest.get("auto_reload_local", cfg.get("do_egress_auto_reload_local", True))
        ),
    )


def is_do_pool_source(cfg: dict[str, Any] | None) -> bool:
    """True when register should build proxy pool from DO egress nodes."""
    cfg = cfg or {}
    if not cfg.get("proxy_pool_enabled", False):
        return False
    source = str(cfg.get("proxy_pool_source") or "file").strip().lower()
    if source in ("do", "digitalocean", "do_egress", "egress"):
        return True
    # Nested enabled flag
    nest = _nested(cfg)
    if nest.get("enabled") is True:
        return True
    if cfg.get("do_egress_enabled") is True:
        return True
    return False


def resolve_egress_slot_count(
    cfg: dict[str, Any] | None,
    *,
    size: int | None = None,
    threads: int | None = None,
) -> int:
    """How many Droplets to create: min(pool_size, threads).

    When register threads < pool_size, only create that many nodes.
    """
    settings = settings_from_config(cfg)
    max_n = max(1, int(settings.pool_size or 1))
    if size is not None:
        return max(1, min(int(size), max_n))
    thr = threads
    if thr is None and cfg:
        try:
            thr = int(cfg.get("register_threads") or 0) or None
        except Exception:
            thr = None
    if thr is not None and thr > 0:
        return max(1, min(int(thr), max_n))
    return max_n


# silence unused import warning for OUTPUT_DIR if any tooling flags it
_ = OUTPUT_DIR
