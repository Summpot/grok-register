"""DO egress pool lifecycle for the register proxy pool."""

from __future__ import annotations

import atexit
import secrets
import string
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from grok_register.do_egress.api import (
    DOError,
    DigitalOceanClient,
    public_ipv4,
    wait_droplet_active,
)
from grok_register.do_egress.local_tunnel import apply_local, socks_url, stop_local
from grok_register.do_egress.remote_bootstrap import render_user_data
from grok_register.do_egress.settings import (
    DoEgressSettings,
    is_do_pool_source,
    settings_from_config,
)
from grok_register.do_egress.state import EgressNode, EgressState, load_state, save_state

LogFn = Callable[[str], None]

_lock = threading.RLock()
_active_settings: DoEgressSettings | None = None
_active_cfg: dict[str, Any] | None = None
_enabled = False
_pool_ready = False
_atexit_registered = False
_cleaned_up = False


def _log(msg: str, log: LogFn | None) -> None:
    if log:
        log(msg)
    else:
        # Always surface destroy/create lifecycle on stdout so force-exit is visible
        try:
            print(msg, flush=True)
        except Exception:
            pass


def _secret(n: int = 28) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def is_enabled() -> bool:
    return bool(_enabled and _active_settings is not None)


def socks_urls(cfg: dict[str, Any] | None = None) -> list[str]:
    settings = _active_settings or (settings_from_config(cfg) if cfg else None)
    if not settings:
        return []
    state = load_state(settings.state_path)
    return [socks_url(settings, n) for n in state.ready_nodes()]


def shutdown_local(cfg: dict[str, Any] | None = None) -> None:
    """Stop local tunnel process only."""
    settings = _active_settings or (settings_from_config(cfg) if cfg else None)
    if settings:
        stop_local(settings)


def _require_token(settings: DoEgressSettings) -> None:
    if not settings.token:
        raise RuntimeError(
            "DO egress requires token: set do_egress.token / DIGITALOCEAN_TOKEN "
            "or digitalocean_token in config.json"
        )


def _ssh_keys(client: DigitalOceanClient, settings: DoEgressSettings, log: LogFn | None) -> list:
    keys = list(settings.ssh_key_ids)
    if keys:
        return keys
    remote = client.list_ssh_keys()
    if not remote:
        raise RuntimeError(
            "No ssh_key_ids configured and no SSH keys on the DO account"
        )
    ids = [k["id"] for k in remote]
    _log(f"[egress] using account SSH keys: {ids}", log)
    return ids


def _register_atexit_once() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(_atexit_cleanup)
    _atexit_registered = True


def _atexit_cleanup() -> None:
    """Best-effort destroy on interpreter exit (Ctrl+C path, crash after pool up)."""
    try:
        destroy_all(log=lambda m: print(m, flush=True))
    except Exception as exc:
        try:
            print(f"[egress] atexit cleanup failed: {exc}", flush=True)
        except Exception:
            pass


def destroy_all(
    cfg: dict[str, Any] | None = None,
    *,
    log: LogFn | None = None,
) -> int:
    """Destroy all managed + tag-matched Droplets and stop local tunnel.

    Idempotent. Returns number of destroy attempts (state + tag orphans).
    """
    global _active_settings, _enabled, _pool_ready, _cleaned_up, _active_cfg

    settings = _active_settings
    if settings is None:
        use_cfg = cfg or _active_cfg
        if use_cfg is not None and is_do_pool_source(use_cfg):
            settings = settings_from_config(use_cfg)
        elif _active_cfg is not None:
            try:
                settings = settings_from_config(_active_cfg)
            except Exception:
                settings = None

    if settings is None or not settings.token:
        # Still try stop local if we have settings without token? skip DO
        if settings:
            try:
                stop_local(settings)
            except Exception:
                pass
        _enabled = False
        _pool_ready = False
        return 0

    destroyed = 0
    with _lock:
        try:
            stop_local(settings)
        except Exception as exc:
            _log(f"[egress] stop local tunnel: {exc}", log)

        client = DigitalOceanClient(settings.token)
        state = load_state(settings.state_path)
        for n in list(state.nodes):
            if n.droplet_id:
                _log(
                    f"[egress] destroy slot {n.slot} id={n.droplet_id} ip={n.ip or '-'}",
                    log,
                )
                try:
                    client.destroy_droplet(int(n.droplet_id))
                    destroyed += 1
                except DOError as exc:
                    if exc.status != 404:
                        _log(f"[egress] destroy id={n.droplet_id} failed: {exc}", log)
                    else:
                        destroyed += 1
            state.remove_slot(n.slot)
        save_state(settings.state_path, state)

        # Orphans with our tag (crash mid-create, external leftovers)
        try:
            tagged = client.list_droplets_by_tag(settings.droplet_tag)
            for d in tagged:
                did = int(d.get("id") or 0)
                if not did:
                    continue
                _log(
                    f"[egress] destroy tagged orphan id={did} name={d.get('name')}",
                    log,
                )
                try:
                    client.destroy_droplet(did)
                    destroyed += 1
                except DOError as exc:
                    if exc.status != 404:
                        _log(f"[egress] destroy orphan {did} failed: {exc}", log)
        except DOError as exc:
            _log(f"[egress] list by tag failed: {exc}", log)

        # Clear local tunnel config inbounds
        try:
            empty = EgressState(nodes=[])
            apply_local(settings, empty)
            stop_local(settings)
        except Exception:
            try:
                stop_local(settings)
            except Exception:
                pass

        _enabled = False
        _pool_ready = False
        _cleaned_up = True
        _log(f"[egress] cleanup done (destroy_ops≈{destroyed})", log)

    return destroyed


def _create_slot(
    settings: DoEgressSettings,
    client: DigitalOceanClient,
    state: EgressState,
    slot: int,
    log: LogFn | None,
) -> EgressNode:
    secret = _secret()
    name = settings.droplet_name(slot)
    user_data = render_user_data(
        remote_port=settings.remote_port,
        remote_secret=secret,
        singbox_version=settings.singbox_version,
        allow_from_cidrs=settings.allow_from_cidrs,
    )
    node = EgressNode(
        slot=slot,
        name=name,
        remote_port=settings.remote_port,
        remote_secret=secret,
        socks_port=settings.socks_port(slot),
        region=settings.region,
        status="creating",
        created_at=time.time(),
    )
    state.upsert(node)
    save_state(settings.state_path, state)

    _log(
        f"[egress] slot {slot}: creating droplet {name} "
        f"region={settings.region} size={settings.size}",
        log,
    )
    ssh_keys = _ssh_keys(client, settings, log)
    droplet = client.create_droplet(
        name=name,
        region=settings.region,
        size=settings.size,
        image=settings.image,
        ssh_keys=ssh_keys,
        user_data=user_data,
        tags=[settings.droplet_tag, f"{settings.droplet_tag}-slot-{slot}"],
    )
    droplet_id = int(droplet["id"])
    node.droplet_id = droplet_id
    state.upsert(node)
    save_state(settings.state_path, state)

    droplet = wait_droplet_active(
        client,
        droplet_id,
        timeout_s=settings.create_timeout_s,
        poll_s=settings.ready_poll_s,
    )
    ip = public_ipv4(droplet)
    node.ip = ip
    state.upsert(node)
    save_state(settings.state_path, state)
    _log(
        f"[egress] slot {slot}: active ip={ip}; wait bootstrap {settings.ready_wait_s}s",
        log,
    )
    time.sleep(max(0, int(settings.ready_wait_s)))
    node.status = "ready"
    node.last_error = ""
    state.upsert(node)
    save_state(settings.state_path, state)
    _log(
        f"[egress] slot {slot}: ready socks={settings.socks_listen}:{node.socks_port}",
        log,
    )
    return node


def _destroy_slot(
    settings: DoEgressSettings,
    client: DigitalOceanClient,
    state: EgressState,
    slot: int,
    log: LogFn | None,
) -> None:
    node = state.get_slot(slot)
    if not node:
        return
    if node.droplet_id:
        _log(
            f"[egress] slot {slot}: destroy droplet id={node.droplet_id} ip={node.ip}",
            log,
        )
        try:
            client.destroy_droplet(int(node.droplet_id))
        except DOError as exc:
            if exc.status != 404:
                raise
    state.remove_slot(slot)
    save_state(settings.state_path, state)


def ensure_pool(
    cfg: dict[str, Any] | None,
    *,
    log: LogFn | None = None,
    size: int | None = None,
    force: bool = False,
) -> list[str]:
    """Ensure egress nodes exist, start local tunnel, return SOCKS5 URLs.

    On first (or force) ensure: destroy leftover Droplets from previous runs,
    then create a fresh pool in the configured region (default San Francisco).
    """
    global _active_settings, _active_cfg, _enabled, _pool_ready, _cleaned_up

    if not is_do_pool_source(cfg or {}):
        _enabled = False
        _pool_ready = False
        return []

    settings = settings_from_config(cfg)
    _require_token(settings)
    target = int(size if size is not None else settings.pool_size)
    if target < 0:
        raise ValueError("pool size must be >= 0")

    with _lock:
        if _pool_ready and _enabled and not force and _active_settings is not None:
            state = load_state(_active_settings.state_path)
            urls = [socks_url(_active_settings, n) for n in state.ready_nodes()]
            if len(urls) >= target:
                return urls

        _active_settings = settings
        _active_cfg = dict(cfg or {})
        _cleaned_up = False
        _register_atexit_once()

        client = DigitalOceanClient(settings.token)

        # Startup cleanup: never reuse stale Droplets from a prior crash/exit
        _log(
            f"[egress] startup cleanup (region={settings.region}) then create pool_size={target}",
            log,
        )
        # destroy_all takes lock — call internal cleanup without re-enter
        _cleanup_droplets_unlocked(settings, client, log=log)

        state = EgressState(nodes=[])
        save_state(settings.state_path, state)

        for slot in range(target):
            try:
                _create_slot(settings, client, state, slot, log)
            except Exception as exc:
                node = state.get_slot(slot) or EgressNode(slot=slot)
                node.status = "error"
                node.last_error = str(exc)[:300]
                state.upsert(node)
                save_state(settings.state_path, state)
                _log(f"[egress] slot {slot}: error {exc}", log)
                # Best-effort cleanup partial pool on failure
                try:
                    _cleanup_droplets_unlocked(settings, client, log=log)
                except Exception:
                    pass
                raise
            state = load_state(settings.state_path)

        urls = apply_local(settings, state)
        _enabled = bool(urls)
        _pool_ready = bool(urls)
        _log(f"[egress] pool ready size={len(urls)} region={settings.region}", log)
        return urls


def _cleanup_droplets_unlocked(
    settings: DoEgressSettings,
    client: DigitalOceanClient,
    *,
    log: LogFn | None = None,
) -> int:
    """Destroy state + tagged droplets. Caller must hold _lock."""
    destroyed = 0
    try:
        stop_local(settings)
    except Exception:
        pass

    state = load_state(settings.state_path)
    for n in list(state.nodes):
        if n.droplet_id:
            _log(
                f"[egress] cleanup slot {n.slot} id={n.droplet_id} ip={n.ip or '-'}",
                log,
            )
            try:
                client.destroy_droplet(int(n.droplet_id))
                destroyed += 1
            except DOError as exc:
                if exc.status != 404:
                    _log(f"[egress] cleanup id={n.droplet_id}: {exc}", log)
        state.remove_slot(n.slot)
    save_state(settings.state_path, EgressState(nodes=[]))

    try:
        for d in client.list_droplets_by_tag(settings.droplet_tag):
            did = int(d.get("id") or 0)
            if not did:
                continue
            _log(
                f"[egress] cleanup tagged id={did} name={d.get('name')}",
                log,
            )
            try:
                client.destroy_droplet(did)
                destroyed += 1
            except DOError as exc:
                if exc.status != 404:
                    _log(f"[egress] cleanup tagged {did}: {exc}", log)
    except DOError as exc:
        _log(f"[egress] list tag failed: {exc}", log)

    return destroyed


def _slot_for_proxy(settings: DoEgressSettings, proxy: str) -> int | None:
    """Map socks5://127.0.0.1:PORT back to slot."""
    p = (proxy or "").strip()
    if not p:
        return None
    try:
        u = urlparse(p if "://" in p else f"socks5://{p}")
        port = int(u.port or 0)
    except Exception:
        return None
    if port <= 0:
        return None
    state = load_state(settings.state_path)
    for n in state.nodes:
        sp = n.socks_port or settings.socks_port(n.slot)
        if sp == port:
            return n.slot
    base = int(settings.socks_base_port)
    if port >= base:
        slot = port - base
        if 0 <= slot < 64:
            return slot
    return None


def rotate_for_proxy(
    proxy: str,
    *,
    reason: str = "",
    cfg: dict[str, Any] | None = None,
    log: LogFn | None = None,
) -> bool:
    """Recreate the Droplet behind a local SOCKS URL. Keeps the same local port."""
    global _active_settings, _pool_ready

    settings = _active_settings
    if settings is None and cfg is not None and is_do_pool_source(cfg):
        settings = settings_from_config(cfg)
        _active_settings = settings
    if settings is None or not _enabled:
        if cfg is not None and is_do_pool_source(cfg):
            settings = settings_from_config(cfg)
        else:
            return False
        if not settings.token:
            return False

    slot = _slot_for_proxy(settings, proxy)
    if slot is None:
        return False
    if not settings.rotate_on_disable:
        _log(f"[egress] rotate disabled; leave slot {slot} as-is ({reason[:80]})", log)
        return True

    with _lock:
        _require_token(settings)
        client = DigitalOceanClient(settings.token)
        state = load_state(settings.state_path)
        _log(f"[egress] rotate slot {slot}: {reason[:120]}", log)
        _destroy_slot(settings, client, state, slot, log)
        state = load_state(settings.state_path)
        try:
            _create_slot(settings, client, state, slot, log)
        except Exception as exc:
            _log(f"[egress] rotate slot {slot} failed: {exc}", log)
            apply_local(settings, load_state(settings.state_path))
            return True
        apply_local(settings, load_state(settings.state_path))
        _pool_ready = True
        return True
