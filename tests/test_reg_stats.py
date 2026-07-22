"""Unit tests for registration attempt telemetry / bot_flag analysis."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grok_register import reg_stats


def _make_jwt(claims: dict) -> str:
    import base64

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    header = b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64(json.dumps(claims).encode())
    return f"{header}.{payload}.sig"


class RegStatsTests(unittest.TestCase):
    def setUp(self):
        reg_stats.abandon_attempt()
        reg_stats.set_enabled(True)
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "reg_stats.jsonl"
        reg_stats.set_stats_path(str(self.path))

    def tearDown(self):
        reg_stats.abandon_attempt()
        reg_stats.set_enabled(None)
        reg_stats.set_stats_path(None)
        self._tmp.cleanup()

    def test_begin_finish_writes_jsonl(self):
        a = reg_stats.begin_attempt(worker_id=1, idx=2, email="a@example.com", proxy="http://u:p@1.2.3.4:8080")
        self.assertIsNotNone(a)
        reg_stats.record_mouse_click(
            {
                "purpose": "turnstile",
                "target_x": 120.5,
                "target_y": 240.1,
                "start_x": 400,
                "start_y": 300,
                "steps_mid": 20,
                "click_delay_ms": 90,
            }
        )
        reg_stats.record_turnstile_event(
            {
                "event": "solved",
                "method": "click",
                "clicks_done": 1,
                "token_len": 100,
                "widget_w": 300,
                "widget_h": 65,
                "duration_ms": 3500,
            }
        )
        rec = reg_stats.finish_attempt(
            "bot_flag",
            reason="bot_flag_source=1",
            bot_flagged=True,
            access_token=_make_jwt({"bot_flag_source": 1, "sub": "uid-1"}),
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["outcome"], "bot_flag")
        self.assertEqual(rec["email_domain"], "example.com")
        self.assertEqual(rec["proxy"], "1.2.3.4:8080")  # no credentials
        self.assertEqual(rec["jwt_claims"].get("bot_flag_source"), 1)
        self.assertEqual(rec["turnstile_summary"]["method"], "click")
        self.assertEqual(rec["mouse_summary"]["clicks"], 1)
        self.assertTrue(self.path.is_file())
        rows = reg_stats.load_records(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempt_id"], rec["attempt_id"])

    def test_disabled_is_noop(self):
        reg_stats.set_enabled(False)
        self.assertIsNone(reg_stats.begin_attempt(worker_id=1))
        reg_stats.record_mouse_click({"target_x": 1})
        self.assertIsNone(reg_stats.finish_attempt("success"))
        self.assertFalse(self.path.exists())

    def test_safe_jwt_claims_drops_secrets(self):
        token = _make_jwt(
            {
                "bot_flag_source": 1,
                "sub": "user-abc",
                "email": "should-not-store@x.com",
                "password": "nope",
                "team_id": "t1",
                "amr": ["pwd"],
            }
        )
        claims = reg_stats.safe_jwt_claims(token)
        self.assertEqual(claims.get("bot_flag_source"), 1)
        self.assertEqual(claims.get("sub"), "user-abc")
        self.assertEqual(claims.get("team_id"), "t1")
        self.assertNotIn("email", claims)
        self.assertNotIn("password", claims)

    def test_analyze_bot_flag_rates(self):
        records = [
            {
                "outcome": "success",
                "email_domain": "good.com",
                "proxy": "10.0.0.1:1",
                "duration_ms": 10000,
                "turnstile_summary": {
                    "method": "auto",
                    "clicks_done": 0,
                    "force_used": False,
                },
                "mouse_summary": {"last_click_delay_ms": 80, "last_steps_mid": 18},
            },
            {
                "outcome": "success",
                "email_domain": "good.com",
                "proxy": "10.0.0.1:1",
                "duration_ms": 12000,
                "turnstile_summary": {
                    "method": "click",
                    "clicks_done": 1,
                    "force_used": False,
                },
                "mouse_summary": {"last_click_delay_ms": 100, "last_steps_mid": 22},
            },
            {
                "outcome": "bot_flag",
                "reason": "bot_flag_source=1",
                "email_domain": "bad.com",
                "proxy": "10.0.0.2:1",
                "duration_ms": 20000,
                "turnstile_summary": {
                    "method": "click",
                    "clicks_done": 3,
                    "force_used": True,
                },
                "mouse_summary": {"last_click_delay_ms": 60, "last_steps_mid": 15},
            },
            {
                "outcome": "bot_flag",
                "reason": "bot_flag_source=1",
                "email_domain": "bad.com",
                "proxy": "10.0.0.2:1",
                "duration_ms": 22000,
                "turnstile_summary": {
                    "method": "click",
                    "clicks_done": 4,
                    "force_used": True,
                },
                "mouse_summary": {"last_click_delay_ms": 55, "last_steps_mid": 14},
            },
        ]
        report = reg_stats.analyze_records(records)
        self.assertEqual(report["total"], 4)
        self.assertEqual(report["by_outcome"]["bot_flag"], 2)
        self.assertAlmostEqual(report["bot_flag_rate"], 0.5)
        bot_f = report["feature_compare"]["bot_flag"]
        ok_f = report["feature_compare"]["success"]
        self.assertGreater(bot_f["avg_clicks"], ok_f["avg_clicks"])
        text = reg_stats.format_analysis(report)
        self.assertIn("bot_flag", text)
        self.assertTrue(any("点击" in h or "bot_flag" in h for h in report["hints"]))

    def test_mouse_click_records_when_attempt_active(self):
        from grok_register.browser_adapter import mouse_click_xy

        reg_stats.begin_attempt(worker_id=9)
        page = type("P", (), {})()
        page.viewport_size = {"width": 1000, "height": 800}
        moves = []

        class Mouse:
            def move(self, x, y, steps=1):
                moves.append((x, y, steps))

            def click(self, x, y, delay=None):
                moves.append(("click", x, y, delay))

        page.mouse = Mouse()
        with patch("grok_register.browser_adapter.human_pause", return_value=None):
            mouse_click_xy(page, 150.0, 200.0, purpose="turnstile")
        rec = reg_stats.finish_attempt("success")
        self.assertEqual(len(rec["mouse"]), 1)
        sample = rec["mouse"][0]
        self.assertEqual(sample["purpose"], "turnstile")
        self.assertIsNotNone(sample.get("start_x"))
        self.assertIsNotNone(sample.get("mid_x"))
        self.assertIsNotNone(sample.get("steps_mid"))
        self.assertIsNotNone(sample.get("click_delay_ms"))
        self.assertTrue(any(m[0] == "click" for m in moves if isinstance(m, tuple)))


if __name__ == "__main__":
    unittest.main()
