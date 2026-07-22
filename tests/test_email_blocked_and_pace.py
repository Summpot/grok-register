"""Tests for email_blocked_domains and register pace helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from grok_register import app


class EmailBlockedDomainTests(unittest.TestCase):
    def setUp(self):
        self.original = app.config.copy()
        app._cf_domain_index = 0

    def tearDown(self):
        app.config = self.original
        app._cf_domain_index = 0

    def test_suffix_match_blocks_cloud(self):
        app.config["email_blocked_domains"] = "ohmyaitrash.cloud"
        self.assertTrue(app.is_email_domain_blocked("ohmyaitrash.cloud"))
        self.assertTrue(app.is_email_domain_blocked("abc.ohmyaitrash.cloud"))
        self.assertFalse(app.is_email_domain_blocked("ohmyaitrash.org"))
        self.assertFalse(app.is_email_domain_blocked("ohmyaitrash.online"))

    def test_cloudflare_next_skips_blocked(self):
        app.config["defaultDomains"] = (
            "ohmyaitrash.cloud,ohmyaitrash.org,627500.xyz"
        )
        app.config["email_blocked_domains"] = "ohmyaitrash.cloud"
        seen = [app.cloudflare_next_default_domain() for _ in range(6)]
        self.assertNotIn("ohmyaitrash.cloud", seen)
        self.assertTrue(any(d == "ohmyaitrash.org" for d in seen))
        self.assertTrue(any(d == "627500.xyz" for d in seen))

    def test_all_blocked_raises(self):
        app.config["defaultDomains"] = "ohmyaitrash.cloud"
        app.config["email_blocked_domains"] = "ohmyaitrash.cloud"
        with self.assertRaises(Exception) as ctx:
            app.cloudflare_next_default_domain()
        self.assertIn("email_blocked_domains", str(ctx.exception))


class PaceTests(unittest.TestCase):
    def setUp(self):
        self.original = app.config.copy()
        app.PERF_FLAGS["sleep_scale"] = 1.0

    def tearDown(self):
        app.config = self.original
        app.PERF_FLAGS["sleep_scale"] = 1.0

    def test_pace_scale_applies(self):
        app.config["register_pace_enabled"] = True
        app.config["register_pace_scale"] = 2.0
        app.PERF_FLAGS["sleep_scale"] = 1.0
        self.assertAlmostEqual(app.register_pace_scale(), 2.0)

    def test_pace_disabled_uses_perf_scale(self):
        app.config["register_pace_enabled"] = False
        app.PERF_FLAGS["sleep_scale"] = 0.15
        self.assertAlmostEqual(app.register_pace_scale(), 0.15)

    def test_human_pause_records_pace(self):
        app.config["register_pace_enabled"] = True
        app.config["register_pace_scale"] = 1.0
        from grok_register import reg_stats

        reg_stats.set_enabled(True)
        reg_stats.begin_attempt(worker_id=1)
        with patch.object(app, "sleep_with_cancel", return_value=None):
            app._human_pause_cancel(0.5, 0.5, None, name="unit_test_pause")
        attempt = reg_stats.current_attempt()
        self.assertIsNotNone(attempt)
        self.assertTrue(any(p.get("name") == "unit_test_pause" for p in attempt["pace"]))
        reg_stats.abandon_attempt()


if __name__ == "__main__":
    unittest.main()
