"""Cloud-init user-data: install tunnel endpoint on a fresh Ubuntu Droplet.

Transport protocol is an implementation detail of the tunnel stack (sing-box).
"""

from __future__ import annotations

import json
import textwrap


def render_user_data(
    *,
    remote_port: int,
    remote_secret: str,
    singbox_version: str,
    allow_from_cidrs: list[str] | None = None,
    server_name: str = "egress",
) -> str:
    allow_from_cidrs = list(allow_from_cidrs or [])
    # sing-box server: accept tunneled traffic and exit via droplet public IP
    server_cfg = {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "hysteria2",
                "tag": "in",
                "listen": "::",
                "listen_port": int(remote_port),
                "users": [{"password": remote_secret}],
                "tls": {
                    "enabled": True,
                    "alpn": ["h3"],
                    "certificate_path": "/etc/sing-box/cert.pem",
                    "key_path": "/etc/sing-box/key.pem",
                    "server_name": server_name,
                },
            }
        ],
        "outbounds": [{"type": "direct", "tag": "direct"}],
    }
    server_json = json.dumps(server_cfg, indent=2, ensure_ascii=False)
    indented_json = textwrap.indent(server_json, "      ")

    if allow_from_cidrs:
        ufw_rules = "\n".join(
            [
                *(f"ufw allow from {c} to any port 22 proto tcp" for c in allow_from_cidrs),
                *(
                    f"ufw allow from {c} to any port {int(remote_port)} proto udp"
                    for c in allow_from_cidrs
                ),
            ]
        )
    else:
        ufw_rules = f"ufw allow 22/tcp\nufw allow {int(remote_port)}/udp"

    setup = textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        mkdir -p /etc/sing-box /var/log/sing-box

        ARCH=$(uname -m)
        case "$ARCH" in
          x86_64|amd64) SB_ARCH=amd64 ;;
          aarch64|arm64) SB_ARCH=arm64 ;;
          *) echo "unsupported arch: $ARCH"; exit 1 ;;
        esac

        VER="{singbox_version}"
        URL="https://github.com/SagerNet/sing-box/releases/download/v${{VER}}/sing-box-${{VER}}-linux-${{SB_ARCH}}.tar.gz"
        curl -fsSL -o /tmp/sing-box.tar.gz "$URL"
        tar -xzf /tmp/sing-box.tar.gz -C /tmp
        install -m 755 /tmp/sing-box-*/sing-box /usr/local/bin/sing-box

        if [[ ! -f /etc/sing-box/cert.pem || ! -f /etc/sing-box/key.pem ]]; then
          openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \\
            -keyout /etc/sing-box/key.pem -out /etc/sing-box/cert.pem \\
            -days 3650 -nodes -subj "/CN={server_name}"
          chmod 600 /etc/sing-box/key.pem /etc/sing-box/cert.pem
        fi

        cat >/etc/systemd/system/sing-box.service <<'UNIT'
        [Unit]
        Description=egress tunnel
        After=network-online.target
        Wants=network-online.target

        [Service]
        Type=simple
        ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
        Restart=on-failure
        RestartSec=3
        LimitNOFILE=1048576

        [Install]
        WantedBy=multi-user.target
        UNIT
        sed -i 's/^        //' /etc/systemd/system/sing-box.service

        ufw --force reset || true
        ufw default deny incoming
        ufw default allow outgoing
        {ufw_rules}
        ufw --force enable || true

        systemctl daemon-reload
        systemctl enable sing-box
        systemctl restart sing-box
        systemctl is-active --quiet sing-box
        echo ready >/var/log/sing-box/ready
        """
    )
    indented_setup = textwrap.indent(setup, "      ")

    return f"""#cloud-config
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
    content: |
{indented_json}
  - path: /usr/local/bin/setup-egress.sh
    permissions: "0755"
    owner: root:root
    content: |
{indented_setup}

runcmd:
  - [/bin/bash, /usr/local/bin/setup-egress.sh]
"""
