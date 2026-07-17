"""Playwright sync driver must be per-thread (greenlet safety)."""
from __future__ import annotations

import threading

from grok_register.browser_adapter import _get_playwright, stop_thread_playwright


def test_playwright_instances_are_thread_local():
    ids: dict[str, int] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(2, timeout=30)

    def worker(name: str) -> None:
        try:
            barrier.wait()
            pw = _get_playwright()
            ids[name] = id(pw)
            # Second call on same thread must reuse the same driver.
            assert _get_playwright() is pw
        except BaseException as exc:  # noqa: BLE001 — collect for main thread
            errors.append(exc)
        finally:
            try:
                stop_thread_playwright()
            except Exception:
                pass

    t1 = threading.Thread(target=worker, args=("a",), name="pw-a")
    t2 = threading.Thread(target=worker, args=("b",), name="pw-b")
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not errors, f"worker errors: {errors}"
    assert set(ids) == {"a", "b"}
    assert ids["a"] != ids["b"], "threads must not share one Playwright driver"


def test_same_thread_reuses_playwright():
    pw1 = _get_playwright()
    try:
        pw2 = _get_playwright()
        assert pw1 is pw2
    finally:
        stop_thread_playwright()
