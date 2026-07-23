"""DigitalOcean egress for the register proxy pool.

Register workers only consume SOCKS5 URLs. Droplet lifecycle and the local
tunnel client are implementation details.
"""

from __future__ import annotations

from grok_register.do_egress.pool import (
    destroy_all,
    ensure_pool,
    is_enabled,
    rotate_for_proxy,
    settings_from_config,
    shutdown_local,
    socks_urls,
)
from grok_register.do_egress.settings import is_do_pool_source

__all__ = [
    "destroy_all",
    "ensure_pool",
    "is_do_pool_source",
    "is_enabled",
    "rotate_for_proxy",
    "settings_from_config",
    "shutdown_local",
    "socks_urls",
]
