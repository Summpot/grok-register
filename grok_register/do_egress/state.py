"""Persist egress node inventory (local SOCKS slot ↔ droplet)."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EgressNode:
    slot: int
    droplet_id: int | None = None
    name: str = ""
    ip: str = ""
    remote_port: int = 8443
    remote_secret: str = ""
    socks_port: int = 0
    region: str = ""
    status: str = "empty"  # empty | creating | ready | error
    created_at: float = 0.0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EgressNode:
        return cls(
            slot=int(d.get("slot", 0)),
            droplet_id=(int(d["droplet_id"]) if d.get("droplet_id") is not None else None),
            name=str(d.get("name") or ""),
            ip=str(d.get("ip") or ""),
            remote_port=int(d.get("remote_port") or d.get("hy2_port") or 8443),
            remote_secret=str(d.get("remote_secret") or d.get("hy2_password") or ""),
            socks_port=int(d.get("socks_port") or 0),
            region=str(d.get("region") or ""),
            status=str(d.get("status") or "empty"),
            created_at=float(d.get("created_at") or 0.0),
            last_error=str(d.get("last_error") or ""),
        )


@dataclass
class EgressState:
    nodes: list[EgressNode] = field(default_factory=list)
    updated_at: float = 0.0

    def get_slot(self, slot: int) -> EgressNode | None:
        for n in self.nodes:
            if n.slot == slot:
                return n
        return None

    def upsert(self, node: EgressNode) -> None:
        for i, n in enumerate(self.nodes):
            if n.slot == node.slot:
                self.nodes[i] = node
                return
        self.nodes.append(node)
        self.nodes.sort(key=lambda x: x.slot)

    def remove_slot(self, slot: int) -> None:
        self.nodes = [n for n in self.nodes if n.slot != slot]

    def ready_nodes(self) -> list[EgressNode]:
        return [
            n
            for n in self.nodes
            if n.status == "ready" and n.ip and n.remote_secret
        ]


_lock = threading.Lock()


def load_state(path: Path) -> EgressState:
    if not path.is_file():
        return EgressState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return EgressState()
    nodes = [
        EgressNode.from_dict(x) for x in (raw.get("nodes") or []) if isinstance(x, dict)
    ]
    return EgressState(nodes=nodes, updated_at=float(raw.get("updated_at") or 0.0))


def save_state(path: Path, state: EgressState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.time()
    payload = {
        "updated_at": state.updated_at,
        "nodes": [n.to_dict() for n in sorted(state.nodes, key=lambda x: x.slot)],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _lock:
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
