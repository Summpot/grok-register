"""DO egress pool lifecycle for the register proxy pool."""

from __future__ import annotations

import atexit
import secrets
import string
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from grok_register.do_egress.api import (
    DOError,
    DigitalOceanClient,
    public_ipv4,
    wait_droplet_active,
)
from grok_register.do_egress.local_tunnel import (
    apply_local,
    probe_remote_ready,
    socks_url,
    ssh_remote_service_ready,
    stop_local,
    tcp_port_open,
)
from grok_register.do_egress.remote_bootstrap import render_user_data
from grok_register.do_egress.settings import (
    DoEgressSettings,
    is_do_pool_source,
    resolve_egress_slot_count,
    settings_from_config,
)
from grok_register.do_egress.singbox_bin import ensure_singbox_for_settings
from grok_register.do_egress.ssh_keys import ensure_managed_ssh
from grok_register.do_egress.state import EgressNode, EgressState, load_state, save_state

LogFn = Callable[[str], None]

_lock = threading.RLock()
_state_io_lock = threading.Lock()  # serialize load/upsert/save across create workers
_active_settings: DoEgressSettings | None = None
_active_cfg: dict[str, Any] | None = None
_enabled = False
_pool_ready = False
_atexit_registered = False
_cleaned_up = False
# Progressive create: registration may start after the first ready droplet.
_building = False
_first_ready = threading.Event()
_all_done = threading.Event()
_create_executor: ThreadPoolExecutor | None = None
_create_futures: dict[Any, int] = {}
_reload_lock = threading.Lock()


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


def is_building() -> bool:
    """True while background droplet creates are still running."""
    return bool(_building)


def wait_until_first_ready(timeout_s: float = 300.0) -> bool:
    """Block until at least one droplet is ready (or timeout / cancel / empty)."""
    if _pool_ready and _enabled:
        return True
    try:
        from grok_register.lifecycle import is_cancelled, wait_event

        ok = wait_event(_first_ready, timeout=max(0.1, float(timeout_s)))
        if is_cancelled():
            return False
    except Exception:
        ok = _first_ready.wait(timeout=max(0.1, float(timeout_s)))
    return bool(ok and _pool_ready and _enabled)


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
    """Ensure managed ed25519 keypair, upload to DO, return key ids for create.

    Always injects the managed public key so SSH readiness probes work with
    the matching private key under state_dir/ssh/.
    """
    ids, priv = ensure_managed_ssh(client, settings, log=log)
    _log(
        f"[egress] droplet SSH keys={ids} identity={priv}",
        log,
    )
    return list(ids)


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
    global _building, _create_executor, _create_futures

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
        _building = False
        # Unblock waiters, but do NOT pretend the pool is ready — callers must
        # check is_cancelled() / _cleaned_up / ready node list after wait.
        _cleaned_up = True
        _all_done.set()
        _first_ready.set()
        return 0

    destroyed = 0
    with _lock:
        _building = False
        _cleaned_up = True
        # Wake waiters so ensure_pool can return promptly on shutdown.
        # Hand-off must treat empty ready_nodes + cleaned_up as "not usable".
        _all_done.set()
        _first_ready.set()
        # Best-effort: do not wait on in-flight creates (destroy will reclaim by tag)
        _create_futures = {}
        _create_executor = None
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
        _building = False
        _cleaned_up = True
        _log(f"[egress] cleanup done (destroy_ops≈{destroyed})", log)

    return destroyed


def _sync_proxy_pool_urls(settings: DoEgressSettings, log: LogFn | None = None) -> list[str]:
    """Reload local tunnel from current ready nodes and publish SOCKS URLs to proxyutil."""
    state = load_state(settings.state_path)
    with _reload_lock:
        urls = apply_local(settings, state)
    try:
        from grok_register.proxyutil import install_do_pool_urls

        install_do_pool_urls(urls, building=_building)
    except Exception as exc:
        _log(f"[egress] install proxy pool urls failed: {exc}", log)
    return urls


def _on_slot_ready(settings: DoEgressSettings, slot: int, log: LogFn | None) -> None:
    """Called when one droplet finishes bootstrap; refresh tunnel + proxy pool."""
    global _enabled, _pool_ready
    urls = _sync_proxy_pool_urls(settings, log=log)
    _enabled = bool(urls)
    _pool_ready = bool(urls)
    if urls:
        _first_ready.set()
        _log(
            f"[egress] progressive ready slots={len(urls)} "
            f"(latest slot={slot}); register may use these SOCKS now",
            log,
        )


def _persist_node(settings: DoEgressSettings, node: EgressNode) -> None:
    """Thread-safe upsert of one node into nodes.json."""
    with _state_io_lock:
        st = load_state(settings.state_path)
        st.upsert(node)
        save_state(settings.state_path, st)


def _create_slot(
    settings: DoEgressSettings,
    client: DigitalOceanClient,
    slot: int,
    log: LogFn | None,
    *,
    ssh_keys: list | None = None,
) -> EgressNode:
    """Create one droplet + wait ready. Safe to run in parallel (own DO client)."""
    secret = _secret()
    tuic_uuid = str(uuid.uuid4())
    tuic_password = _secret(24)
    trojan_password = _secret(24)
    name = settings.droplet_name(slot)
    user_data = render_user_data(
        remote_port=settings.remote_port,
        remote_secret=secret,
        singbox_version=settings.singbox_version,
        tuic_port=settings.tuic_port,
        tuic_uuid=tuic_uuid,
        tuic_password=tuic_password,
        trojan_port=settings.trojan_port,
        trojan_password=trojan_password,
        enable_hy2=settings.enable_hy2,
        enable_tuic=settings.enable_tuic,
        enable_trojan=settings.enable_trojan,
    )
    node = EgressNode(
        slot=slot,
        name=name,
        remote_port=settings.remote_port,
        tuic_port=settings.tuic_port,
        trojan_port=settings.trojan_port,
        remote_secret=secret,
        tuic_uuid=tuic_uuid,
        tuic_password=tuic_password,
        trojan_password=trojan_password,
        socks_port=settings.socks_port(slot),
        region=settings.region,
        status="creating",
        created_at=time.time(),
    )
    _persist_node(settings, node)

    _log(
        f"[egress] slot {slot}: creating droplet {name} "
        f"region={settings.region} size={settings.size}",
        log,
    )
    keys = ssh_keys if ssh_keys is not None else _ssh_keys(client, settings, log)
    droplet = client.create_droplet(
        name=name,
        region=settings.region,
        size=settings.size,
        image=settings.image,
        ssh_keys=keys,
        user_data=user_data,
        tags=[settings.droplet_tag, f"{settings.droplet_tag}-slot-{slot}"],
    )
    droplet_id = int(droplet["id"])
    node.droplet_id = droplet_id
    _persist_node(settings, node)

    droplet = wait_droplet_active(
        client,
        droplet_id,
        timeout_s=settings.create_timeout_s,
        poll_s=settings.ready_poll_s,
    )
    ip = public_ipv4(droplet)
    node.ip = ip
    _persist_node(settings, node)
    _log(
        f"[egress] slot {slot}: active ip={ip}; probing remote sing-box "
        f"(max {settings.ready_wait_s}s, poll {settings.ready_poll_s}s)",
        log,
    )
    working = _wait_remote_singbox(settings, node, log=log)
    node.working_protocols = working
    node.status = "ready"
    node.last_error = ""
    _persist_node(settings, node)
    _log(
        f"[egress] slot {slot}: ready socks={settings.socks_listen}:{node.socks_port} "
        f"via={working}",
        log,
    )
    return node


def _create_slot_worker(
    settings: DoEgressSettings,
    slot: int,
    log: LogFn | None,
    ssh_keys: list,
) -> EgressNode:
    """Worker entry: one DigitalOceanClient per thread (requests.Session not shared)."""
    client = DigitalOceanClient(settings.token)
    return _create_slot(settings, client, slot, log, ssh_keys=ssh_keys)


def _wait_remote_singbox(
    settings: DoEgressSettings,
    node: EgressNode,
    *,
    log: LogFn | None = None,
) -> list[str]:
    """Poll until sing-box is up and at least one tunnel protocol works.

    Primary gate (when managed key present): SSH checks
    ``/var/log/sing-box/ready`` + ``systemctl is-active sing-box``.
    Fallback gate: TCP connect to trojan port.
    Then ``probe_remote_ready`` confirms which protocols work end-to-end.
    """
    timeout_s = max(30, int(settings.ready_wait_s or 240))
    poll_s = max(1, int(settings.ready_poll_s or 5))
    deadline = time.time() + timeout_s
    attempt = 0
    last_detail = ""
    t0 = time.time()
    trojan_port = int(node.trojan_port or settings.trojan_port or 443)
    identity = str(getattr(settings, "ssh_identity_file", "") or "").strip()
    use_ssh = bool(
        settings.ssh_probe and identity and Path(identity).is_file()
    )
    saw_service = False

    while time.time() < deadline:
        attempt += 1

        if use_ssh:
            ssh_ok, ssh_detail = ssh_remote_service_ready(
                node.ip,
                identity_file=identity,
                timeout_s=min(12.0, max(4.0, float(poll_s) + 2.0)),
            )
            last_detail = ssh_detail
            if not ssh_ok:
                remain = deadline - time.time()
                if remain <= 0:
                    break
                _log(
                    f"[egress] slot {node.slot}: not ready yet attempt={attempt} "
                    f"({ssh_detail}); retry in {min(poll_s, remain):.0f}s",
                    log,
                )
                time.sleep(min(poll_s, max(0.5, remain)))
                continue
            if not saw_service:
                saw_service = True
                _log(
                    f"[egress] slot {node.slot}: {ssh_detail}, probing tunnels…",
                    log,
                )
        elif settings.enable_trojan:
            # No identity file: fall back to TCP progress signal
            if not tcp_port_open(node.ip, trojan_port, timeout_s=3.0):
                last_detail = (
                    f"waiting tcp:{node.ip}:{trojan_port} "
                    f"(cloud-init / sing-box / or path filtered)"
                )
                remain = deadline - time.time()
                if remain <= 0:
                    break
                _log(
                    f"[egress] slot {node.slot}: not ready yet attempt={attempt} "
                    f"({last_detail}); retry in {min(poll_s, remain):.0f}s",
                    log,
                )
                time.sleep(min(poll_s, max(0.5, remain)))
                continue
            if not saw_service:
                saw_service = True
                _log(
                    f"[egress] slot {node.slot}: tcp:{trojan_port} open, probing…",
                    log,
                )

        ok, detail, working = probe_remote_ready(
            settings, node, probe_timeout_s=20.0
        )
        last_detail = detail
        if ok and working:
            elapsed = time.time() - t0
            _log(
                f"[egress] slot {node.slot}: tunnel ready after "
                f"{elapsed:.1f}s ({attempt} probe(s), working={working}, {detail})",
                log,
            )
            return working

        remain = deadline - time.time()
        if remain <= 0:
            break
        _log(
            f"[egress] slot {node.slot}: not ready yet attempt={attempt} "
            f"({detail}); retry in {min(poll_s, remain):.0f}s",
            log,
        )
        time.sleep(min(poll_s, max(0.5, remain)))

    hint = ""
    low = (last_detail or "").lower()
    if "ssh" in low and ("connection refused" in low or "timed out" in low or "timeout" in low):
        hint = (
            " SSH(22) 连不上：确认本机有 OpenSSH、安全组/防火墙放行 22，"
            "或 region 换 sgp1；密钥在 state_dir/ssh/id_ed25519。"
        )
    elif "unreachable" in low or "i/o timeout" in low or "waiting tcp" in low:
        hint = (
            " 网络层到 VPS 不通（非账号密码问题）。"
            " 建议: 1) config 里 do_egress.region 改为 sgp1/sgp2 试新加坡;"
            " 2) trojan_port 保持 443;"
            " 3) 本机 PowerShell: Test-NetConnection <ip> -Port 443"
        )
    raise TimeoutError(
        f"slot {node.slot} ip={node.ip}: remote tunnel not ready within "
        f"{timeout_s}s (last={last_detail}).{hint}"
    )


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
    wait_first: bool = True,
    first_ready_timeout_s: float | None = None,
) -> list[str]:
    """Ensure egress nodes; return SOCKS URLs as soon as the first droplet is ready.

    Creates ``ceil(threads / threads_per_droplet)`` droplets (capped by pool_size)
    in parallel. Registration can start after **one** node is ready; remaining
    nodes keep bootstrapping in the background and are published dynamically.
    """
    global _active_settings, _active_cfg, _enabled, _pool_ready, _cleaned_up
    global _building, _create_executor, _create_futures

    if not is_do_pool_source(cfg or {}):
        _enabled = False
        _pool_ready = False
        _building = False
        return []

    settings = settings_from_config(cfg)
    _require_token(settings)
    target = resolve_egress_slot_count(cfg, size=size)
    if target < 1:
        raise ValueError("pool size must be >= 1")
    tpd = max(1, int(getattr(settings, "threads_per_droplet", 3) or 3))
    thr = 0
    try:
        thr = int((cfg or {}).get("register_threads") or 0)
    except Exception:
        thr = 0

    # Re-entry: progressive create already started — never wipe & recreate.
    session_active = False
    with _lock:
        if not force and _active_settings is not None and (_building or _pool_ready or _enabled):
            session_active = True
            settings = _active_settings

    if session_active:
        if wait_first and not _pool_ready:
            timeout = first_ready_timeout_s
            if timeout is None:
                timeout = float(
                    max(
                        60,
                        int(settings.create_timeout_s or 180)
                        + int(settings.ready_wait_s or 240),
                    )
                )
            _wait_first_ready_interruptible(timeout)
        if _is_shutdown_requested():
            _log("[egress] cancelled while waiting (re-entry); not handing off", log)
            return []
        state = load_state(settings.state_path)
        return [socks_url(settings, n) for n in state.ready_nodes()]

    errors: list[tuple[int, Exception]] = []
    errors_lock = threading.Lock()

    with _lock:
        # Double-check under lock (another thread may have started create)
        if not force and _active_settings is not None and (_building or _pool_ready or _enabled):
            settings = _active_settings
            state = load_state(settings.state_path)
            urls = [socks_url(settings, n) for n in state.ready_nodes()]
            if urls or not wait_first:
                return urls
            # fall through to wait outside without starting a second create
            session_active = True
        else:
            session_active = False

        if not session_active:
            # Auto-download sing-box for do_egress.singbox_version if missing
            ensure_singbox_for_settings(settings, log=log)

            _active_settings = settings
            _active_cfg = dict(cfg or {})
            _cleaned_up = False
            _register_atexit_once()
            _first_ready.clear()
            _all_done.clear()
            _building = True
            _enabled = False
            _pool_ready = False

            try:
                from grok_register.proxyutil import set_do_pool_building

                set_do_pool_building(True)
            except Exception:
                pass

            client = DigitalOceanClient(settings.token)

            protos = []
            if settings.enable_hy2:
                protos.append(f"hy2:{settings.remote_port}/udp")
            if settings.enable_tuic:
                protos.append(f"tuic:{settings.tuic_port}/udp")
            if settings.enable_trojan:
                protos.append(f"trojan:{settings.trojan_port}/tcp")
            _log(
                f"[egress] startup cleanup (region={settings.region}) then create "
                f"slots={target} (pool_size max={settings.pool_size}, "
                f"threads_per_droplet={tpd}, register_threads={thr or '-'}) "
                f"protocols=[{', '.join(protos) or 'none'}] "
                f"— progressive: start register after first ready",
                log,
            )
            _cleanup_droplets_unlocked(settings, client, log=log)

            with _state_io_lock:
                save_state(settings.state_path, EgressState(nodes=[]))

            ssh_keys = _ssh_keys(client, settings, log)
            slots = list(range(target))
            _log(
                f"[egress] creating {target} droplet(s) in parallel "
                f"(~{tpd} register thread(s) per droplet)…",
                log,
            )

            workers = min(target, 8)
            ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="do-egress")
            _create_executor = ex

            def _slot_job(slot: int) -> None:
                if _cleaned_up or _is_shutdown_requested():
                    return
                try:
                    _create_slot_worker(settings, slot, log, ssh_keys)
                    if _cleaned_up or _is_shutdown_requested():
                        _log(
                            f"[egress] slot {slot}: finished bootstrap but "
                            f"shutdown in progress — skip ready hand-off",
                            log,
                        )
                        return
                    _on_slot_ready(settings, slot, log)
                except Exception as exc:
                    with errors_lock:
                        errors.append((slot, exc))
                    node = EgressNode(slot=slot, status="error", last_error=str(exc)[:300])
                    try:
                        _persist_node(settings, node)
                    except Exception:
                        pass
                    _log(f"[egress] slot {slot}: error {exc}", log)

            futs = {ex.submit(_slot_job, slot): slot for slot in slots}
            _create_futures = futs

            def _finalize() -> None:
                global _building, _enabled, _pool_ready
                try:
                    for fut in as_completed(futs):
                        try:
                            fut.result()
                        except Exception:
                            pass
                finally:
                    try:
                        ex.shutdown(wait=False, cancel_futures=True)
                    except TypeError:
                        ex.shutdown(wait=False)
                    except Exception:
                        pass
                    with _lock:
                        _building = False
                        state = load_state(settings.state_path)
                        ready_n = len(state.ready_nodes())
                        if ready_n <= 0 and errors:
                            _log(
                                f"[egress] all {len(errors)} create(s) failed; "
                                f"first={errors[0][1]}",
                                log,
                            )
                        elif errors:
                            _log(
                                f"[egress] progressive done: ready={ready_n}/{target}, "
                                f"failed={len(errors)} (keeping ready nodes)",
                                log,
                            )
                        else:
                            _log(
                                f"[egress] progressive done: ready={ready_n}/{target}",
                                log,
                            )
                        try:
                            from grok_register.proxyutil import set_do_pool_building

                            set_do_pool_building(False)
                        except Exception:
                            pass
                        try:
                            if ready_n > 0 and not _cleaned_up:
                                _sync_proxy_pool_urls(settings, log=log)
                                _enabled = True
                                _pool_ready = True
                                _first_ready.set()
                        except Exception as exc:
                            _log(f"[egress] final sync failed: {exc}", log)
                        _all_done.set()

            threading.Thread(
                target=_finalize, name="do-egress-finalize", daemon=True
            ).start()

    # Outside lock: wait only for first ready (not full pool)
    if wait_first:
        timeout = first_ready_timeout_s
        if timeout is None:
            timeout = float(
                max(
                    60,
                    int(settings.create_timeout_s or 180)
                    + int(settings.ready_wait_s or 240),
                )
            )
        ok = _wait_first_ready_interruptible(timeout)
        if _is_shutdown_requested():
            _log(
                "[egress] cancelled while waiting for first ready; "
                "hand-off aborted (cleanup via CLI lifecycle)",
                log,
            )
            return []
        if not ok and not _pool_ready:
            if _building and not _is_shutdown_requested():
                _wait_all_done_interruptible(min(30.0, timeout))
            if _is_shutdown_requested():
                _log("[egress] cancelled during extended wait; hand-off aborted", log)
                return []
            if not _pool_ready:
                with errors_lock:
                    err_s = f"; first={errors[0][1]}" if errors else ""
                raise TimeoutError(
                    f"no egress droplet ready within {timeout:.0f}s "
                    f"(target slots={target}){err_s}"
                )

    if _is_shutdown_requested() or _cleaned_up:
        _log(
            "[egress] hand-off skipped: "
            f"cancelled={_is_shutdown_requested()} cleaned_up={_cleaned_up}",
            log,
        )
        return []

    state = load_state(settings.state_path)
    urls = [socks_url(settings, n) for n in state.ready_nodes()]
    _log(
        f"[egress] hand-off to register: ready={len(urls)}/{target} "
        f"building={_building} region={settings.region}",
        log,
    )
    return urls


def _is_shutdown_requested() -> bool:
    if _cleaned_up:
        return True
    try:
        from grok_register.lifecycle import is_cancelled

        return bool(is_cancelled())
    except Exception:
        return False


def _wait_first_ready_interruptible(timeout: float) -> bool:
    """Wait for first ready; returns True if event fired. Respects cancel."""
    try:
        from grok_register.lifecycle import is_cancelled, wait_event

        if is_cancelled():
            return False
        return bool(wait_event(_first_ready, timeout=timeout))
    except Exception:
        return bool(_first_ready.wait(timeout=timeout))


def _wait_all_done_interruptible(timeout: float) -> bool:
    try:
        from grok_register.lifecycle import is_cancelled, wait_event

        if is_cancelled():
            return False
        return bool(wait_event(_all_done, timeout=timeout))
    except Exception:
        return bool(_all_done.wait(timeout=timeout))


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
        try:
            _create_slot(settings, client, slot, log)
        except Exception as exc:
            _log(f"[egress] rotate slot {slot} failed: {exc}", log)
            apply_local(settings, load_state(settings.state_path))
            return True
        apply_local(settings, load_state(settings.state_path))
        _pool_ready = True
        return True
