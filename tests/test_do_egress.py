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
    def test_user_data_is_valid_yaml_shape_with_b64(self):
        import base64
        import re

        yml = render_user_data(
            remote_port=8443,
            remote_secret="Sec123",
            singbox_version="1.11.15",
            tuic_port=8444,
            tuic_uuid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            tuic_password="TuicPass",
            trojan_port=443,
            trojan_password="TrPass",
        )
        self.assertIn("#cloud-config", yml)
        self.assertIn("encoding: b64", yml)
        self.assertIn("setup-egress.sh", yml)
        blobs = re.findall(r"content: ([A-Za-z0-9+/=]+)", yml)
        self.assertEqual(len(blobs), 2)
        cfg = base64.b64decode(blobs[0]).decode()
        setup = base64.b64decode(blobs[1]).decode()
        self.assertIn("Sec123", cfg)
        self.assertIn("hysteria2", cfg)
        self.assertIn("tuic", cfg)
        self.assertIn("trojan", cfg)
        self.assertIn("TuicPass", cfg)
        self.assertIn("TrPass", cfg)
        self.assertIn('"listen_port": 443', cfg)
        # Must not run host firewall tools
        self.assertNotIn("ufw ", setup.lower())
        self.assertNotIn("ufw\n", setup.lower())
        self.assertNotIn("iptables", setup.lower())
        self.assertNotIn("ufw", yml.lower())
        self.assertIn("systemctl restart sing-box", setup)


class TestLocalConfig(unittest.TestCase):
    def test_one_socks_per_node_with_protocol_fallback(self):
        from grok_register.do_egress.settings import DoEgressSettings

        s = DoEgressSettings(
            socks_base_port=17891,
            enable_hy2=True,
            enable_tuic=True,
            enable_trojan=True,
        )
        nodes = [
            EgressNode(
                slot=0,
                ip="1.1.1.1",
                remote_secret="a",
                tuic_uuid="11111111-1111-4111-8111-111111111111",
                tuic_password="tp",
                tuic_port=8444,
                trojan_port=443,
                trojan_password="tr",
                socks_port=17891,
                status="ready",
                # Probe found all three; order is randomized at probe time
                working_protocols=["hy2", "tuic", "trojan"],
            ),
        ]
        doc = build_local_config(s, nodes)
        self.assertEqual(len(doc["inbounds"]), 1)
        types = {o["type"] for o in doc["outbounds"]}
        self.assertIn("hysteria2", types)
        self.assertIn("tuic", types)
        self.assertIn("trojan", types)
        self.assertIn("urltest", types)
        self.assertEqual(doc["route"]["rules"][0]["outbound"], "egress-0")
        urltest = next(o for o in doc["outbounds"] if o.get("type") == "urltest")
        self.assertEqual(set(urltest["outbounds"]), {"hy2-0", "tuic-0", "trojan-0"})
        url = socks_url(s, nodes[0])
        self.assertEqual(url, "socks5://127.0.0.1:17891")

    def test_working_protocols_filters_leaves(self):
        from grok_register.do_egress.settings import DoEgressSettings

        s = DoEgressSettings(enable_hy2=True, enable_tuic=True, enable_trojan=True)
        n = EgressNode(
            slot=0,
            ip="1.1.1.1",
            remote_secret="a",
            tuic_uuid="11111111-1111-4111-8111-111111111111",
            status="ready",
            working_protocols=["trojan"],
        )
        doc = build_local_config(s, [n])
        types = {o["type"] for o in doc["outbounds"] if o.get("type") not in ("direct", "block", "urltest")}
        self.assertEqual(types, {"trojan"})

    def test_working_protocols_order_preserved_for_urltest(self):
        from grok_register.do_egress.settings import DoEgressSettings

        s = DoEgressSettings(enable_hy2=True, enable_tuic=True, enable_trojan=True)
        n = EgressNode(
            slot=0,
            ip="1.1.1.1",
            remote_secret="a",
            tuic_uuid="11111111-1111-4111-8111-111111111111",
            tuic_password="tp",
            trojan_password="tr",
            status="ready",
            working_protocols=["tuic", "hy2"],
        )
        doc = build_local_config(s, [n])
        urltest = next(o for o in doc["outbounds"] if o.get("type") == "urltest")
        self.assertEqual(urltest["outbounds"], ["tuic-0", "hy2-0"])

    def test_slot_count_follows_threads_per_droplet(self):
        from grok_register.do_egress.settings import resolve_egress_slot_count

        cfg = {
            "proxy_pool_enabled": True,
            "proxy_pool_source": "do",
            "do_egress": {"pool_size": 3, "threads_per_droplet": 3},
            "register_threads": 1,
        }
        # ceil(threads / 3), capped by pool_size
        self.assertEqual(resolve_egress_slot_count(cfg), 1)
        self.assertEqual(resolve_egress_slot_count(cfg, threads=2), 1)
        self.assertEqual(resolve_egress_slot_count(cfg, threads=3), 1)
        self.assertEqual(resolve_egress_slot_count(cfg, threads=4), 2)
        self.assertEqual(resolve_egress_slot_count(cfg, threads=9), 3)
        self.assertEqual(resolve_egress_slot_count(cfg, threads=12), 3)  # pool_size cap
        self.assertEqual(resolve_egress_slot_count(cfg, size=2, threads=12), 2)


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
        self.assertTrue(raw["do_egress"].get("ssh_probe", False))


class TestManagedSshKeys(unittest.TestCase):
    def test_generate_local_keypair(self):
        from grok_register.do_egress.settings import DoEgressSettings
        from grok_register.do_egress import ssh_keys as sk

        with tempfile.TemporaryDirectory() as td:
            s = DoEgressSettings(state_dir=td, ssh_key_name="test-egress")
            priv, pub = sk.ensure_local_keypair(s)
            self.assertTrue(priv.is_file())
            self.assertTrue(pub.is_file())
            text = pub.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("ssh-ed25519 "))
            # Second call reuses
            priv2, pub2 = sk.ensure_local_keypair(s)
            self.assertEqual(priv, priv2)
            self.assertEqual(pub.read_text(encoding="utf-8"), text)

    def test_find_or_create_reuses_existing_pubkey(self):
        from grok_register.do_egress import ssh_keys as sk

        class FakeClient:
            def __init__(self):
                self.created = 0
                self.keys = [
                    {
                        "id": 42,
                        "name": "old",
                        "public_key": "ssh-ed25519 AAAAB3NzaC1lZDI1NTE5AAAAIGtestblob comment",
                    }
                ]

            def list_ssh_keys(self):
                return list(self.keys)

            def create_ssh_key(self, *, name, public_key):
                self.created += 1
                kid = 100 + self.created
                row = {"id": kid, "name": name, "public_key": public_key}
                self.keys.append(row)
                return row

        client = FakeClient()
        kid = sk.find_or_create_do_key(
            client,
            name="grok-reg-egress",
            public_key="ssh-ed25519 AAAAB3NzaC1lZDI1NTE5AAAAIGtestblob other-comment",
        )
        self.assertEqual(kid, 42)
        self.assertEqual(client.created, 0)

    def test_find_or_create_uploads_new(self):
        from grok_register.do_egress import ssh_keys as sk

        class FakeClient:
            def list_ssh_keys(self):
                return []

            def create_ssh_key(self, *, name, public_key):
                return {"id": 7, "name": name, "public_key": public_key}

        kid = sk.find_or_create_do_key(
            FakeClient(),
            name="grok-reg-egress",
            public_key="ssh-ed25519 AAAAB3NzaC1lZDI1NTE5AAAAINewkey x",
        )
        self.assertEqual(kid, 7)

    def test_ssh_remote_ready_passes_identity(self):
        from grok_register.do_egress import local_tunnel as lt

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd

            class R:
                returncode = 0
                stdout = ""
                stderr = ""

            return R()

        with patch.object(lt.subprocess, "run", side_effect=fake_run):
            ok, detail = lt.ssh_remote_service_ready(
                "1.2.3.4", identity_file=r"C:\keys\id_ed25519", timeout_s=5
            )
        self.assertTrue(ok)
        self.assertIn("ssh:ready+active", detail)
        self.assertIn("-i", captured["cmd"])
        self.assertIn(r"C:\keys\id_ed25519", captured["cmd"])
        self.assertIn("IdentitiesOnly=yes", captured["cmd"])


if __name__ == "__main__":
    unittest.main()
