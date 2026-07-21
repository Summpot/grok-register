"""Unit tests for patient Turnstile solve helpers (no real browser)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from grok_register.browser_adapter import (
    _box_click_priority,
    _checkbox_point_from_box,
    _gauss_clamp,
    solve_turnstile_patient,
)


class TestTurnstileGeometry(unittest.TestCase):
    def test_gauss_clamp_bounds(self):
        for _ in range(40):
            v = _gauss_clamp(10.0, 3.0, 5.0, 15.0)
            self.assertGreaterEqual(v, 5.0)
            self.assertLessEqual(v, 15.0)

    def test_checkbox_point_in_left_circle(self):
        box = {"x": 100.0, "y": 200.0, "width": 300.0, "height": 65.0}
        for _ in range(30):
            cx, cy = _checkbox_point_from_box(box)
            self.assertGreaterEqual(cx, 108.0)
            self.assertLessEqual(cx, 142.0)
            self.assertGreaterEqual(cy, 200.0 + 65 * 0.25)
            self.assertLessEqual(cy, 200.0 + 65 * 0.75)

    def test_box_priority_prefers_classic_widget(self):
        classic = {"kind": "iframe", "x": 0, "y": 10, "width": 300, "height": 65}
        huge = {"kind": "iframe", "x": 0, "y": 10, "width": 800, "height": 600}
        host = {"kind": "host", "x": 0, "y": 10, "width": 300, "height": 65}
        ranked = sorted([huge, host, classic], key=_box_click_priority)
        self.assertEqual(ranked[0], classic)
        self.assertEqual(ranked[-1], huge)


class TestSolveTurnstilePatient(unittest.TestCase):
    def test_returns_existing_token(self):
        page = MagicMock()
        token = "x" * 90
        page.evaluate.return_value = token
        # find boxes unused
        got = solve_turnstile_patient(
            page,
            max_clicks=1,
            timeout=2.0,
            auto_solve_wait=(0.01, 0.02),
            post_click_wait=(0.01, 0.02),
            sleep_fn=lambda s: None,
        )
        self.assertEqual(got, token)

    def test_auto_solve_without_click(self):
        page = MagicMock()
        # First few token reads empty, then filled (auto-solve window)
        values = ["", "", "y" * 85]
        page.evaluate.side_effect = lambda *a, **k: values.pop(0) if values else "y" * 85

        clicks = {"n": 0}

        def sleep_fn(_s):
            return None

        # Patch click path by making find boxes empty and click never needed
        # solve will wait auto window then may try click — inject token during auto wait
        # Simpler: always return growing token via side_effect on evaluate only for token
        page.evaluate.side_effect = None
        seq = iter(["", "", "z" * 88])

        def eval_side(script, *args, **kwargs):
            # token value / token len scripts return sequential values
            if "cf-turnstile-response" in str(script):
                try:
                    return next(seq)
                except StopIteration:
                    return "z" * 88
            # bbox js
            if "getBoundingClientRect" in str(script) or "kind" in str(script):
                return [
                    {
                        "x": 10,
                        "y": 20,
                        "width": 300,
                        "height": 65,
                        "kind": "iframe",
                        "tag": "iframe",
                    }
                ]
            return ""

        page.evaluate.side_effect = eval_side
        page.frames = []
        page.viewport_size = {"width": 1000, "height": 800}
        page.mouse = MagicMock()

        # Force no real click success needed if auto-solve fills token
        got = solve_turnstile_patient(
            page,
            max_clicks=3,
            timeout=3.0,
            auto_solve_wait=(0.05, 0.08),
            post_click_wait=(0.05, 0.08),
            sleep_fn=sleep_fn,
        )
        self.assertGreaterEqual(len(got), 80)

    def test_click_then_token(self):
        page = MagicMock()
        state = {"token": "", "clicks": 0}

        def eval_side(script, *args, **kwargs):
            s = str(script)
            if "cf-turnstile-response" in s and "querySelector" in s:
                return state["token"]
            if "getBoundingClientRect" in s or "out.push" in s or "kind" in s:
                return [
                    {
                        "x": 50,
                        "y": 80,
                        "width": 300,
                        "height": 65,
                        "kind": "iframe",
                        "tag": "iframe",
                    }
                ]
            return True

        page.evaluate.side_effect = eval_side
        page.frames = []
        page.viewport_size = {"width": 1200, "height": 900}
        page.mouse = MagicMock()

        def on_click(*a, **k):
            state["clicks"] += 1
            state["token"] = "T" * 90

        page.mouse.click.side_effect = on_click

        got = solve_turnstile_patient(
            page,
            max_clicks=3,
            timeout=4.0,
            auto_solve_wait=(0.02, 0.03),
            post_click_wait=(0.05, 0.08),
            sleep_fn=lambda s: None,
        )
        self.assertEqual(got, "T" * 90)
        self.assertGreaterEqual(state["clicks"], 1)
        self.assertLessEqual(state["clicks"], 3)

    def test_timeout_raises(self):
        page = MagicMock()
        page.evaluate.return_value = ""
        page.frames = []
        page.viewport_size = {"width": 800, "height": 600}
        page.mouse = MagicMock()
        with self.assertRaises(RuntimeError):
            solve_turnstile_patient(
                page,
                max_clicks=2,
                timeout=0.4,
                auto_solve_wait=(0.01, 0.02),
                post_click_wait=(0.01, 0.02),
                sleep_fn=lambda s: None,
            )


if __name__ == "__main__":
    unittest.main()
