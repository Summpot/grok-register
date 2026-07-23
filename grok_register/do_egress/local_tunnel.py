"""Local tunnel client: expose one SOCKS5 port per egress node on 127.0.0.1.

Uses sing-box under the hood; register workers only see socks5://127.0.0.1:port.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from grok_register.do_egress.settings import DoEgressSettings
from grok_register.do_egress.state import EgressNode, EgressState

# Default URL for end-to-end readiness (via SOCKS → remote tunnel → internet)
DEFAULT_HEALTH_URL = "https://www.gstatic.com/generate_204"


def _tls_quic() -> dict[str, Any]:
    return {
        "enabled": True,
        "server_name": "egress",
        "insecure": True,
        "alpn": ["h3"],
    }


def _tls_tcp() -> dict[str, Any]:
    return {
        "enabled": True,
        "server_name": "egress",
        "insecure": True,
    }


def _protocol_outbounds(
    settings: DoEgressSettings,
    n: EgressNode,
    *,
    only: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build protocol leaf outbounds + ordered tags for urltest (prefer first).

    only: if set to hy2|tuic|trojan, build just that protocol (for probes).
    When node.working_protocols is non-empty, only include those that passed probe.
    """
    leaves: list[dict[str, Any]] = []
    prefer = (settings.protocol_prefer or "hy2").strip().lower()
    working = {str(x).lower() for x in (n.working_protocols or [])}

    def _want(name: str) -> bool:
        if only is not None:
            return only == name
        if working:
            return name in working
        return True

    hy2_tag = f"hy2-{n.slot}"
    tuic_tag = f"tuic-{n.slot}"
    trojan_tag = f"trojan-{n.slot}"

    if settings.enable_hy2 and _want("hy2"):
        leaves.append(
            {
                "type": "hysteria2",
                "tag": hy2_tag,
                "server": n.ip,
                "server_port": int(n.remote_port or settings.remote_port),
                "password": n.remote_secret,
                "tls": _tls_quic(),
            }
        )
    if settings.enable_tuic and _want("tuic"):
        uuid = (n.tuic_uuid or "").strip()
        if uuid:
            leaves.append(
                {
                    "type": "tuic",
                    "tag": tuic_tag,
                    "server": n.ip,
                    "server_port": int(n.tuic_port or settings.tuic_port or 8444),
                    "uuid": uuid,
                    "password": (n.tuic_password or n.remote_secret or ""),
                    "congestion_control": "bbr",
                    "udp_relay_mode": "native",
                    "zero_rtt_handshake": False,
                    "tls": _tls_quic(),
                }
            )
    if settings.enable_trojan and _want("trojan"):
        leaves.append(
            {
                "type": "trojan",
                "tag": trojan_tag,
                "server": n.ip,
                "server_port": int(n.trojan_port or settings.trojan_port or 443),
                "password": (n.trojan_password or n.remote_secret or ""),
                "connect_timeout": "15s",
                "tls": _tls_tcp(),
            }
        )

    present = {x["tag"]: x for x in leaves}
    order_pref = []
    # prefer first, then stable order hy2 → tuic → trojan
    for name, tag in (
        ("hy2", hy2_tag),
        ("tuic", tuic_tag),
        ("trojan", trojan_tag),
    ):
        if tag in present:
            order_pref.append((0 if name == prefer else 1, name, tag))
    order_pref.sort(key=lambda t: (t[0], {"hy2": 0, "tuic": 1, "trojan": 2}.get(t[1], 9)))
    ordered = [t[2] for t in order_pref]
    if not ordered:
        ordered = [x["tag"] for x in leaves]
    return leaves, ordered


def build_local_config(settings: DoEgressSettings, nodes: list[EgressNode]) -> dict[str, Any]:
    """One SOCKS inbound per node; outbound = urltest(hy2/tuic/trojan) for fallback."""
    ready = sorted(
        [n for n in nodes if n.status == "ready" and n.ip and n.remote_secret],
        key=lambda n: n.slot,
    )
    inbounds: list[dict[str, Any]] = []
    outbounds: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []

    for n in ready:
        in_tag = f"socks-{n.slot}"
        group_tag = f"egress-{n.slot}"
        port = n.socks_port or settings.socks_port(n.slot)
        inbound: dict[str, Any] = {
            "type": "socks",
            "tag": in_tag,
            "listen": settings.socks_listen,
            "listen_port": int(port),
            "sniff": False,
        }
        if settings.socks_user and settings.socks_password:
            inbound["users"] = [
                {
                    "username": settings.socks_user,
                    "password": settings.socks_password,
                }
            ]
        inbounds.append(inbound)

        leaves, ordered = _protocol_outbounds(settings, n)
        if not leaves:
            continue
        outbounds.extend(leaves)
        if len(ordered) == 1:
            # Single protocol — no urltest needed
            rules.append({"inbound": [in_tag], "outbound": ordered[0]})
        else:
            outbounds.append(
                {
                    "type": "urltest",
                    "tag": group_tag,
                    "outbounds": ordered,
                    "url": DEFAULT_HEALTH_URL,
                    "interval": "30s",
                    "tolerance": 150,
                    "idle_timeout": "30m",
                    "interrupt_exist_connections": False,
                }
            )
            rules.append({"inbound": [in_tag], "outbound": group_tag})

    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {
            "rules": rules,
            "final": "block",
            "auto_detect_interface": True,
        },
    }


def socks_url(settings: DoEgressSettings, node: EgressNode) -> str:
    host = settings.socks_listen
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = node.socks_port or settings.socks_port(node.slot)
    if settings.socks_user and settings.socks_password:
        u = quote(settings.socks_user, safe="")
        p = quote(settings.socks_password, safe="")
        return f"socks5://{u}:{p}@{host}:{port}"
    return f"socks5://{host}:{port}"


def write_local_config(settings: DoEgressSettings, state: EgressState) -> Path:
    path = settings.local_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = build_local_config(settings, state.nodes)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, 0, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def read_pid(settings: DoEgressSettings) -> int | None:
    path = settings.pid_path
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return pid if _pid_alive(pid) else None


def stop_local(settings: DoEgressSettings) -> bool:
    pid = read_pid(settings)
    if pid is None:
        if settings.pid_path.is_file():
            try:
                settings.pid_path.unlink()
            except Exception:
                pass
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True,
                text=True,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.4)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        settings.pid_path.unlink(missing_ok=True)  # type: ignore[call-arg]
    except TypeError:
        if settings.pid_path.is_file():
            settings.pid_path.unlink()
    time.sleep(0.3)
    return True


def start_local(settings: DoEgressSettings) -> int:
    config_path = settings.local_config_path
    if not config_path.is_file():
        raise FileNotFoundError(f"local tunnel config missing: {config_path}")
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(settings.log_path, "a", encoding="utf-8")  # noqa: SIM115
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
    proc = subprocess.Popen(
        [settings.singbox_exe, "run", "-c", str(config_path)],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )
    settings.pid_path.parent.mkdir(parents=True, exist_ok=True)
    settings.pid_path.write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.8)
    if proc.poll() is not None:
        log_f.close()
        raise RuntimeError(
            f"local tunnel exited immediately (code={proc.returncode}); "
            f"check singbox_exe={settings.singbox_exe!r} and {settings.log_path}"
        )
    return proc.pid


def reload_local(settings: DoEgressSettings) -> int | None:
    stop_local(settings)
    try:
        doc = json.loads(settings.local_config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not doc.get("inbounds"):
        return None
    return start_local(settings)


def apply_local(settings: DoEgressSettings, state: EgressState) -> list[str]:
    """Rewrite tunnel config, reload process, return SOCKS URLs for ready nodes."""
    write_local_config(settings, state)
    if settings.auto_reload_local:
        reload_local(settings)
    return [socks_url(settings, n) for n in state.ready_nodes()]


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_via_socks(proxy_url: str, url: str, timeout_s: float) -> tuple[bool, str]:
    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        try:
            from curl_cffi import requests as crequests  # type: ignore

            r = crequests.get(
                url,
                proxies=proxies,
                timeout=timeout_s,
                allow_redirects=False,
            )
            ok = r.status_code in (200, 204, 301, 302, 304)
            return ok, f"http={r.status_code}"
        except ImportError:
            pass
        import requests

        r = requests.get(
            url,
            proxies=proxies,
            timeout=timeout_s,
            allow_redirects=False,
        )
        ok = r.status_code in (200, 204, 301, 302, 304)
        return ok, f"http={r.status_code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def tcp_port_open(ip: str, port: int, *, timeout_s: float = 2.0) -> bool:
    if not ip or not port:
        return False
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


def _probe_settings_copy(
    settings: DoEgressSettings,
    *,
    enable_hy2: bool,
    enable_tuic: bool,
    enable_trojan: bool,
) -> DoEgressSettings:
    return DoEgressSettings(
        token=settings.token,
        region=settings.region,
        size=settings.size,
        image=settings.image,
        ssh_key_ids=list(settings.ssh_key_ids),
        droplet_tag=settings.droplet_tag,
        name_prefix=settings.name_prefix,
        pool_size=settings.pool_size,
        remote_port=settings.remote_port,
        tuic_port=settings.tuic_port,
        trojan_port=settings.trojan_port,
        enable_hy2=enable_hy2,
        enable_tuic=enable_tuic,
        enable_trojan=enable_trojan,
        protocol_prefer=settings.protocol_prefer,
        ssh_probe=False,
        singbox_version=settings.singbox_version,
        socks_listen="127.0.0.1",
        socks_base_port=settings.socks_base_port,
        socks_user="",
        socks_password="",
        state_dir=settings.state_dir,
        singbox_exe=settings.singbox_exe,
        create_timeout_s=settings.create_timeout_s,
        ready_wait_s=settings.ready_wait_s,
        ready_poll_s=settings.ready_poll_s,
        allow_from_cidrs=list(settings.allow_from_cidrs),
        rotate_on_disable=settings.rotate_on_disable,
        auto_reload_local=False,
    )


def _run_one_probe(
    settings: DoEgressSettings,
    node: EgressNode,
    *,
    protocol: str,
    health_url: str,
    probe_timeout_s: float,
) -> tuple[bool, str]:
    """Run short-lived sing-box with a single protocol; return (ok, detail)."""
    exe = (settings.singbox_exe or "").strip()
    if not exe:
        return False, "singbox_exe empty"

    probe_port = _free_tcp_port()
    probe_node = EgressNode(
        slot=node.slot,
        droplet_id=node.droplet_id,
        name=node.name,
        ip=node.ip,
        remote_port=node.remote_port or settings.remote_port,
        tuic_port=node.tuic_port or settings.tuic_port,
        trojan_port=node.trojan_port or settings.trojan_port,
        remote_secret=node.remote_secret,
        tuic_uuid=node.tuic_uuid,
        tuic_password=node.tuic_password or node.remote_secret,
        trojan_password=node.trojan_password or node.remote_secret,
        socks_port=probe_port,
        region=node.region,
        status="ready",
        working_protocols=[],  # force build from enable flags only
    )
    flags = {
        "hy2": (protocol == "hy2", False, False),
        "tuic": (False, protocol == "tuic", False),
        "trojan": (False, False, protocol == "trojan"),
    }.get(protocol)
    if not flags:
        return False, f"unknown protocol {protocol}"
    ps = _probe_settings_copy(
        settings,
        enable_hy2=flags[0],
        enable_tuic=flags[1],
        enable_trojan=flags[2],
    )
    ps.socks_base_port = probe_port
    leaves, ordered = _protocol_outbounds(ps, probe_node, only=protocol)
    if not leaves:
        return False, f"{protocol}: not configured"

    in_tag = "socks-probe"
    out_tag = ordered[0]
    cfg_path = settings.local_config_path.parent / f"probe-{protocol}-{node.slot}.json"
    log_path = settings.local_config_path.parent / f"probe-{protocol}-{node.slot}.log"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # Write log via sing-box config (avoids full-buffer stdout when not a TTY)
    try:
        if log_path.is_file():
            log_path.unlink()
    except Exception:
        pass

    doc = {
        "log": {
            "level": "info",
            "timestamp": True,
            "output": str(log_path).replace("\\", "/"),
        },
        "inbounds": [
            {
                "type": "socks",
                "tag": in_tag,
                "listen": "127.0.0.1",
                "listen_port": probe_port,
            }
        ],
        "outbounds": leaves
        + [
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {
            "rules": [{"inbound": [in_tag], "outbound": out_tag}],
            "final": "block",
            "auto_detect_interface": True,
        },
    }
    cfg_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    # Preflight: raw TCP to remote (Trojan) for a clear network error
    if protocol == "trojan":
        rport = int(node.trojan_port or settings.trojan_port or 443)
        if not tcp_port_open(node.ip, rport, timeout_s=min(8.0, probe_timeout_s)):
            return (
                False,
                f"{protocol}: tcp {node.ip}:{rport} unreachable (i/o timeout). "
                f"本机到该 VPS 的 TCP 被拦或路由不通；可试 region=sgp1 或确认运营商未墙 DO。",
            )

    proc: subprocess.Popen | None = None
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [exe, "run", "-c", str(cfg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        deadline = time.time() + min(8.0, probe_timeout_s)
        socks_up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                tail = _read_log_tail(log_path)
                return False, f"{protocol}: probe exit={proc.returncode} {tail!r}"
            try:
                with socket.create_connection(("127.0.0.1", probe_port), timeout=0.4):
                    socks_up = True
                    break
            except OSError:
                time.sleep(0.15)
        if not socks_up:
            return False, f"{protocol}: local socks not up"

        time.sleep(0.35)
        # Prefer socks5h so DNS goes through the tunnel
        ok, detail = _http_via_socks(
            f"socks5h://127.0.0.1:{probe_port}",
            health_url,
            timeout_s=probe_timeout_s,
        )
        if not ok:
            # fallback socks5
            ok, detail = _http_via_socks(
                f"socks5://127.0.0.1:{probe_port}",
                health_url,
                timeout_s=probe_timeout_s,
            )
        if ok:
            return True, f"{protocol}:{detail}"
        tail = _read_log_tail(log_path)
        msg = f"{protocol}:{detail}"
        if tail:
            msg += f" | {tail}"
        return False, msg
    finally:
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        proc.kill()
            except Exception:
                pass
        try:
            cfg_path.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            if cfg_path.is_file():
                cfg_path.unlink()
        except Exception:
            pass


def _read_log_tail(path: Path, n: int = 3) -> str:
    try:
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if not lines:
            return ""
        return " | ".join(x.strip() for x in lines[-n:])[-240:]
    except Exception:
        return ""


def probe_remote_ready(
    settings: DoEgressSettings,
    node: EgressNode,
    *,
    health_url: str = DEFAULT_HEALTH_URL,
    probe_timeout_s: float = 12.0,
) -> tuple[bool, str, list[str]]:
    """Probe each enabled protocol separately. Ready if any works.

    Returns (ok, detail, working_protocol_names).
    """
    if not node.ip or not node.remote_secret:
        return False, "missing ip/secret", []

    candidates: list[str] = []
    if settings.enable_hy2:
        candidates.append("hy2")
    if settings.enable_tuic and (node.tuic_uuid or "").strip():
        candidates.append("tuic")
    if settings.enable_trojan:
        candidates.append("trojan")
    if not candidates:
        return False, "no protocols enabled", []

    # Always try TCP trojan before UDP when both enabled (UDP often blackholed CN→DO)
    prefer = (settings.protocol_prefer or "trojan").lower()
    ordered: list[str] = []
    for c in (prefer, "trojan", "hy2", "tuic"):
        if c in candidates and c not in ordered:
            ordered.append(c)
    for c in candidates:
        if c not in ordered:
            ordered.append(c)
    candidates = ordered

    working: list[str] = []
    details: list[str] = []
    # Up to 3 tries on the preferred/TCP protocol before trying others
    primary = candidates[0]
    for attempt in range(1, 4):
        ok, detail = _run_one_probe(
            settings,
            node,
            protocol=primary,
            health_url=health_url,
            probe_timeout_s=probe_timeout_s,
        )
        details.append(f"{primary}#{attempt}:{detail}")
        if ok:
            working.append(primary)
            break
        time.sleep(2.0)

    if not working:
        for proto in candidates[1:]:
            ok, detail = _run_one_probe(
                settings,
                node,
                protocol=proto,
                health_url=health_url,
                probe_timeout_s=probe_timeout_s,
            )
            details.append(detail)
            if ok:
                working.append(proto)
                break

    if working:
        return True, "; ".join(details), working
    return False, "; ".join(details), []


def ssh_remote_service_ready(ip: str, *, timeout_s: float = 8.0) -> tuple[bool, str]:
    """Optional SSH check: ready marker + systemctl (when OpenSSH client works)."""
    if not ip:
        return False, "no ip"
    ssh = "ssh"
    # Windows OpenSSH uses NUL for known_hosts discard
    known = "NUL" if os.name == "nt" else "/dev/null"
    cmd = [
        ssh,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"UserKnownHostsFile={known}",
        "-o",
        f"ConnectTimeout={max(1, int(timeout_s))}",
        f"root@{ip}",
        "test -f /var/log/sing-box/ready && systemctl is-active --quiet sing-box",
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s + 5,
            check=False,
        )
        if r.returncode == 0:
            return True, "ssh:ready+active"
        err = (r.stderr or r.stdout or "").strip()[:160]
        return False, f"ssh rc={r.returncode} {err}"
    except FileNotFoundError:
        return False, "ssh not installed"
    except Exception as exc:
        return False, f"ssh: {exc}"
