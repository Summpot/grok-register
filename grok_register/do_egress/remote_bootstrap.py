"""Cloud-init user-data: install tunnel endpoint on a fresh Ubuntu Droplet.

Protocols (all optional via flags):
  - Hysteria2  UDP  (remote_port)
  - TUIC       UDP  (tuic_port)
  - Trojan     TCP  (trojan_port)  — fallback when UDP is blocked

Scripts are embedded as base64 so multi-line shell never breaks YAML
(previous bug: f-string multi-line ufw rules lost indentation → cloud-init
parse failure → setup never ran → no sing-box).
"""

from __future__ import annotations

import base64
import json
import textwrap


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def render_user_data(
    *,
    remote_port: int,
    remote_secret: str,
    singbox_version: str,
    tuic_port: int | None = None,
    tuic_uuid: str = "",
    tuic_password: str = "",
    trojan_port: int | None = None,
    trojan_password: str = "",
    allow_from_cidrs: list[str] | None = None,
    server_name: str = "egress",
    enable_hy2: bool = True,
    enable_tuic: bool = True,
    enable_trojan: bool = True,
) -> str:
    allow_from_cidrs = list(allow_from_cidrs or [])
    hy2_port = int(remote_port)
    t_port = int(tuic_port if tuic_port is not None else (hy2_port + 1))
    tr_port = int(trojan_port if trojan_port is not None else (hy2_port + 2))
    t_pass = tuic_password or remote_secret
    t_uuid = tuic_uuid or "00000000-0000-4000-8000-000000000001"
    tr_pass = trojan_password or remote_secret

    inbounds: list[dict] = []
    if enable_hy2:
        inbounds.append(
            {
                "type": "hysteria2",
                "tag": "hy2-in",
                "listen": "::",
                "listen_port": hy2_port,
                "users": [{"password": remote_secret}],
                "tls": {
                    "enabled": True,
                    "alpn": ["h3"],
                    "certificate_path": "/etc/sing-box/cert.pem",
                    "key_path": "/etc/sing-box/key.pem",
                    "server_name": server_name,
                },
            }
        )
    if enable_tuic:
        inbounds.append(
            {
                "type": "tuic",
                "tag": "tuic-in",
                "listen": "::",
                "listen_port": t_port,
                "users": [{"uuid": t_uuid, "password": t_pass}],
                "congestion_control": "bbr",
                "tls": {
                    "enabled": True,
                    "alpn": ["h3"],
                    "certificate_path": "/etc/sing-box/cert.pem",
                    "key_path": "/etc/sing-box/key.pem",
                    "server_name": server_name,
                },
            }
        )
    if enable_trojan:
        inbounds.append(
            {
                "type": "trojan",
                "tag": "trojan-in",
                "listen": "::",
                "listen_port": tr_port,
                "users": [{"password": tr_pass}],
                "tls": {
                    "enabled": True,
                    "certificate_path": "/etc/sing-box/cert.pem",
                    "key_path": "/etc/sing-box/key.pem",
                    "server_name": server_name,
                },
            }
        )
    if not inbounds:
        raise ValueError("at least one tunnel protocol must be enabled")

    server_cfg = {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": inbounds,
        "outbounds": [{"type": "direct", "tag": "direct"}],
    }
    server_json = json.dumps(server_cfg, indent=2, ensure_ascii=False)

    udp_ports: list[int] = []
    tcp_ports: list[int] = []
    if enable_hy2:
        udp_ports.append(hy2_port)
    if enable_tuic:
        udp_ports.append(t_port)
    if enable_trojan:
        tcp_ports.append(tr_port)

    ufw_cmds: list[str] = []
    if allow_from_cidrs:
        for c in allow_from_cidrs:
            ufw_cmds.append(f"ufw allow from {c} to any port 22 proto tcp")
            for p in tcp_ports:
                ufw_cmds.append(f"ufw allow from {c} to any port {int(p)} proto tcp")
            for p in udp_ports:
                ufw_cmds.append(f"ufw allow from {c} to any port {int(p)} proto udp")
    else:
        ufw_cmds.append("ufw allow 22/tcp")
        for p in tcp_ports:
            ufw_cmds.append(f"ufw allow {int(p)}/tcp")
        for p in udp_ports:
            ufw_cmds.append(f"ufw allow {int(p)}/udp")

    setup_lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "export DEBIAN_FRONTEND=noninteractive",
        "mkdir -p /etc/sing-box /var/log/sing-box",
        "",
        "ARCH=$(uname -m)",
        'case "$ARCH" in',
        "  x86_64|amd64) SB_ARCH=amd64 ;;",
        "  aarch64|arm64) SB_ARCH=arm64 ;;",
        '  *) echo "unsupported arch: $ARCH"; exit 1 ;;',
        "esac",
        "",
        f'VER="{singbox_version}"',
        'URL="https://github.com/SagerNet/sing-box/releases/download/v${VER}/sing-box-${VER}-linux-${SB_ARCH}.tar.gz"',
        'curl -fsSL -o /tmp/sing-box.tar.gz "$URL"',
        "tar -xzf /tmp/sing-box.tar.gz -C /tmp",
        "install -m 755 /tmp/sing-box-*/sing-box /usr/local/bin/sing-box",
        "",
        "if [[ ! -f /etc/sing-box/cert.pem || ! -f /etc/sing-box/key.pem ]]; then",
        "  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \\",
        "    -keyout /etc/sing-box/key.pem -out /etc/sing-box/cert.pem \\",
        f'    -days 3650 -nodes -subj "/CN={server_name}"',
        "  chmod 600 /etc/sing-box/key.pem /etc/sing-box/cert.pem",
        "fi",
        "",
        "/usr/local/bin/sing-box check -c /etc/sing-box/config.json",
        "",
        "cat >/etc/systemd/system/sing-box.service <<'UNIT'",
        "[Unit]",
        "Description=egress tunnel",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json",
        "Restart=on-failure",
        "RestartSec=3",
        "LimitNOFILE=1048576",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "UNIT",
        "",
        "ufw --force reset || true",
        "ufw default deny incoming",
        "ufw default allow outgoing",
        *ufw_cmds,
        "ufw --force enable || true",
        "",
        "systemctl daemon-reload",
        "systemctl enable sing-box",
        "systemctl restart sing-box",
        "systemctl is-active --quiet sing-box",
        "echo ready >/var/log/sing-box/ready",
        "",
    ]
    setup = "\n".join(setup_lines)

    cfg_b64 = _b64(server_json + "\n")
    setup_b64 = _b64(setup)

    return textwrap.dedent(
        f"""\
        #cloud-config
        package_update: true
        package_upgrade: false
        packages:
          - curl
          - ca-certificates
          - openssl
          - ufw

        write_files:
          - path: /etc/sing-box/config.json
            permissions: "0600"
            owner: root:root
            encoding: b64
            content: {cfg_b64}
          - path: /usr/local/bin/setup-egress.sh
            permissions: "0755"
            owner: root:root
            encoding: b64
            content: {setup_b64}

        runcmd:
          - [/bin/bash, /usr/local/bin/setup-egress.sh]
        """
    )
