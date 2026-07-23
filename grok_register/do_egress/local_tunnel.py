"""Local tunnel client: expose one SOCKS5 port per egress node on 127.0.0.1.

Uses sing-box under the hood; register workers only see socks5://127.0.0.1:port.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from grok_register.do_egress.settings import DoEgressSettings
from grok_register.do_egress.state import EgressNode, EgressState


def build_local_config(settings: DoEgressSettings, nodes: list[EgressNode]) -> dict[str, Any]:
    ready = sorted(
        [n for n in nodes if n.status == "ready" and n.ip and n.remote_secret],
        key=lambda n: n.slot,
    )
    inbounds: list[dict[str, Any]] = []
    outbounds: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []

    for n in ready:
        in_tag = f"socks-{n.slot}"
        out_tag = f"egress-{n.slot}"
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
        # Tunnel outbound — protocol is internal wiring only
        outbounds.append(
            {
                "type": "hysteria2",
                "tag": out_tag,
                "server": n.ip,
                "server_port": int(n.remote_port or settings.remote_port),
                "password": n.remote_secret,
                "tls": {
                    "enabled": True,
                    "server_name": "egress",
                    "insecure": True,
                    "alpn": ["h3"],
                },
            }
        )
        rules.append({"inbound": [in_tag], "outbound": out_tag})

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
