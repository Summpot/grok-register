"""Unit tests for DO egress integration (no live DigitalOcean calls)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_register.do_egress.local_tunnel import build_local_config, socks_url
from grok_register.do_egress.remote_bootstrap import render_user_data
from grok_register.do_egress.settings import is_do_pool_source, settings_from_config
from grok_register.do_egress.state import EgressNode, EgressState, load_state, save_state
from grok_register.proxyutil import _install_pool_urls, next_pool_proxy, pool_size


class TestSettings(unittest.TestCase):
    def test_source_flags(self):
        self.assertFalse(is_do_pool_source({"proxy_pool_enabled": True}))
        self.assertTrue(
            is_do_pool_source(
                {"proxy_pool_enabled": True, "proxy_pool_source": "do"}
            )
        )
        self.assertTrue(
            is_do_pool_source(
                {"proxy_pool_enabled": True, "do_egress": {"enabled": True}}
            )
        )

    def test_nested_settings(self):
        cfg = {
            "do_egress": {
                "token": "abc",
                "pool_size": 2,
                "socks_base_port": 20000,
            }
        }
        s = settings_from_config(cfg)
        self.assertEqual(s.token, "abc")
        self.assertEqual(s.pool_size, 2)
        self.assertEqual(s.socks_port(1), 20001)
        self.assertEqual(s.region, "sfo3")  # default San Francisco

    def test_default_region_san_francisco(self):
        s = settings_from_config({})
        self.assertEqual(s.region, "sfo3")


class TestBootstrap(unittest.TestCase):
    def test_user_data_has_secret_not_branded_cli(self):
        yml = render_user_data(
            remote_port=8443,
            remote_secret="Sec123",
            singbox_version="1.11.15",
        )
        self.assertIn("Sec123", yml)
        self.assertIn("setup-egress.sh", yml)
        self.assertIn("#cloud-config", yml)


class TestLocalConfig(unittest.TestCase):
    def test_one_socks_per_node(self):
        from grok_register.do_egress.settings import DoEgressSettings

        s = DoEgressSettings(socks_base_port=17891)
        nodes = [
            EgressNode(
                slot=0,
                ip="1.1.1.1",
                remote_secret="a",
                socks_port=17891,
                status="ready",
            ),
            EgressNode(
                slot=1,
                ip="2.2.2.2",
                remote_secret="b",
                socks_port=17892,
                status="ready",
            ),
        ]
        doc = build_local_config(s, nodes)
        self.assertEqual(len(doc["inbounds"]), 2)
        self.assertEqual(doc["inbounds"][0]["listen_port"], 17891)
        self.assertEqual(doc["route"]["rules"][0]["outbound"], "egress-0")
        url = socks_url(s, nodes[0])
        self.assertEqual(url, "socks5://127.0.0.1:17891")


class TestState(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nodes.json"
            st = EgressState(
                nodes=[
                    EgressNode(
                        slot=0, ip="10.0.0.1", status="ready", remote_secret="x"
                    )
                ]
            )
            save_state(path, st)
            loaded = load_state(path)
            self.assertEqual(loaded.nodes[0].remote_secret, "x")


class TestProxyInstall(unittest.TestCase):
    def test_memory_pool_from_socks(self):
        n = _install_pool_urls(
            ["socks5://127.0.0.1:17891", "socks5://127.0.0.1:17892"]
        )
        self.assertEqual(n, 2)
        self.assertEqual(pool_size(), 2)
        p = next_pool_proxy("round_robin")
        self.assertIn("1789", p)


class TestExampleConfig(unittest.TestCase):
    def test_example_has_do_egress(self):
        root = Path(__file__).resolve().parents[1]
        raw = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
        self.assertIn("do_egress", raw)
        self.assertEqual(raw.get("proxy_pool_source"), "file")


if __name__ == "__main__":
    unittest.main()
