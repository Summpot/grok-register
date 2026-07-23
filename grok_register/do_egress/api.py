"""Minimal DigitalOcean Droplets API client."""

from __future__ import annotations

import time
from typing import Any

import requests

API_BASE = "https://api.digitalocean.com/v2"


class DOError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class DigitalOceanClient:
    def __init__(self, token: str, timeout: float = 60.0):
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {(token or '').strip()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        r = self._session.request(
            method, url, json=json_body, params=params, timeout=self._timeout
        )
        if r.status_code == 204:
            return None
        try:
            data = r.json() if r.content else None
        except Exception:
            data = {"raw": r.text}
        if r.status_code >= 400:
            msg = f"DO API {method} {path} -> {r.status_code}"
            if isinstance(data, dict):
                msg = f"{msg}: {data.get('message') or data.get('id') or data}"
            raise DOError(msg, status=r.status_code, body=data)
        return data

    def create_droplet(
        self,
        *,
        name: str,
        region: str,
        size: str,
        image: str,
        ssh_keys: list[int | str],
        user_data: str,
        tags: list[str],
    ) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/droplets",
            json_body={
                "name": name,
                "region": region,
                "size": size,
                "image": image,
                "ssh_keys": ssh_keys or [],
                "user_data": user_data,
                "tags": tags,
                "ipv6": False,
                "monitoring": False,
            },
        )
        droplet = (data or {}).get("droplet")
        if not droplet:
            raise DOError("create_droplet: missing droplet", body=data)
        return droplet

    def get_droplet(self, droplet_id: int) -> dict[str, Any]:
        data = self._request("GET", f"/droplets/{int(droplet_id)}")
        droplet = (data or {}).get("droplet")
        if not droplet:
            raise DOError(f"missing droplet {droplet_id}", body=data)
        return droplet

    def destroy_droplet(self, droplet_id: int) -> None:
        self._request("DELETE", f"/droplets/{int(droplet_id)}")

    def list_droplets_by_tag(self, tag: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                "/droplets",
                params={"tag_name": tag, "page": page, "per_page": 50},
            )
            batch = (data or {}).get("droplets") or []
            out.extend(batch)
            links = ((data or {}).get("links") or {}).get("pages") or {}
            if not links.get("next"):
                break
            page += 1
        return out

    def list_ssh_keys(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                "/account/keys",
                params={"page": page, "per_page": 50},
            )
            batch = (data or {}).get("ssh_keys") or []
            out.extend(batch)
            links = ((data or {}).get("links") or {}).get("pages") or {}
            if not links.get("next"):
                break
            page += 1
            if page > 20:
                break
        return out

    def create_ssh_key(self, *, name: str, public_key: str) -> dict[str, Any]:
        data = self._request(
            "POST",
            "/account/keys",
            json_body={"name": name, "public_key": public_key},
        )
        key = (data or {}).get("ssh_key")
        if not key:
            raise DOError("create_ssh_key: missing ssh_key", body=data)
        return key


def public_ipv4(droplet: dict[str, Any]) -> str:
    for net in droplet.get("networks", {}).get("v4") or []:
        if net.get("type") == "public" and net.get("ip_address"):
            return str(net["ip_address"])
    return ""


def wait_droplet_active(
    client: DigitalOceanClient,
    droplet_id: int,
    *,
    timeout_s: float = 180,
    poll_s: float = 5,
) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = client.get_droplet(droplet_id)
        if str(last.get("status") or "") == "active" and public_ipv4(last):
            return last
        time.sleep(poll_s)
    raise TimeoutError(f"droplet {droplet_id} not ready within {timeout_s}s")
