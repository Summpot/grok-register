"""Unit tests for cooperative cancel / interrupt lifecycle."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from grok_register import lifecycle


class TestLifecycleCancel(unittest.TestCase):
    def setUp(self) -> None:
        lifecycle.reset_for_tests()

    def tearDown(self) -> None:
        lifecycle.reset_for_tests()

    def test_request_cancel_sets_flag_and_reason(self) -> None:
        n = lifecycle.request_cancel(reason="unit", source="test")
        self.assertEqual(n, 1)
        self.assertTrue(lifecycle.is_cancelled())
        self.assertEqual(lifecycle.cancel_reason(), "unit")
        self.assertEqual(lifecycle.cancel_source(), "test")
        self.assertEqual(lifecycle.press_count(), 1)

    def test_debounce_coalesces_duplicate_presses(self) -> None:
        n1 = lifecycle.request_cancel(reason="a", source="SIGINT")
        n2 = lifecycle.request_cancel(reason="b", source="CTRL_C_EVENT")
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)  # debounced
        self.assertEqual(lifecycle.press_count(), 1)

    def test_second_press_after_debounce_triggers_force(self) -> None:
        lifecycle.request_cancel(reason="first", source="SIGINT")
        # expire debounce window
        time.sleep(0.25)
        with patch.object(lifecycle, "_force_exit") as fe:
            n = lifecycle.request_cancel(reason="second", source="SIGINT")
            self.assertEqual(n, 2)
            fe.assert_called_once()

    def test_wait_event_returns_on_event(self) -> None:
        ev = threading.Event()

        def _set() -> None:
            time.sleep(0.05)
            ev.set()

        threading.Thread(target=_set, daemon=True).start()
        ok = lifecycle.wait_event(ev, timeout=2.0, poll_s=0.05)
        self.assertTrue(ok)

    def test_wait_event_returns_false_on_cancel(self) -> None:
        ev = threading.Event()

        def _cancel() -> None:
            time.sleep(0.05)
            lifecycle.request_cancel(reason="wait-test", source="test")

        threading.Thread(target=_cancel, daemon=True).start()
        ok = lifecycle.wait_event(ev, timeout=2.0, poll_s=0.05)
        self.assertFalse(ok)
        self.assertTrue(lifecycle.is_cancelled())

    def test_sleep_aborts_on_cancel(self) -> None:
        def _cancel() -> None:
            time.sleep(0.05)
            lifecycle.request_cancel(reason="sleep-test", source="test")

        threading.Thread(target=_cancel, daemon=True).start()
        t0 = time.monotonic()
        done = lifecycle.sleep(5.0, poll_s=0.05)
        elapsed = time.monotonic() - t0
        self.assertFalse(done)
        self.assertLess(elapsed, 2.0)

    def test_should_stop_alias(self) -> None:
        self.assertFalse(lifecycle.should_stop())
        lifecycle.request_cancel(reason="x", source="test")
        self.assertTrue(lifecycle.should_stop())


class TestEnsurePoolRespectsCancel(unittest.TestCase):
    def setUp(self) -> None:
        lifecycle.reset_for_tests()

    def tearDown(self) -> None:
        lifecycle.reset_for_tests()

    def test_wait_first_ready_interruptible_sees_cancel(self) -> None:
        from grok_register.do_egress import pool as pool_mod

        # ensure clean events
        pool_mod._first_ready.clear()
        lifecycle.request_cancel(reason="pre", source="test")
        ok = pool_mod._wait_first_ready_interruptible(2.0)
        self.assertFalse(ok)
        self.assertTrue(pool_mod._is_shutdown_requested())


if __name__ == "__main__":
    unittest.main()
