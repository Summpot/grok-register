"""TLS browser ownership must not be contaminated by module globals."""
from __future__ import annotations

import threading

from grok_register import app as reg


class _FakeBrowser:
    def __init__(self, name: str):
        self.name = name
        self.quit_calls = 0

    def quit(self, del_data: bool = False):
        self.quit_calls += 1


def test_sync_does_not_steal_foreign_browser_into_tls():
    mine = _FakeBrowser("mine")
    foreign = _FakeBrowser("foreign")
    reg._tls_set_browser(mine)
    reg._tls_set_page(mine)
    reg.browser = foreign
    reg.page = foreign

    reg._sync_thread_browser_globals()

    assert reg._tls_get_browser() is mine
    assert reg._tls_get_page() is mine


def test_stop_browser_only_clears_this_thread_tls():
    mine = _FakeBrowser("mine")
    reg._tls_set_browser(mine)
    reg._tls_set_page(mine)
    reg.browser = mine
    reg.page = mine

    reg.stop_browser()

    assert reg._tls_get_browser() is None
    assert reg._tls_get_page() is None
    assert mine.quit_calls == 1
    assert reg.browser is None
    assert reg.page is None


def test_stop_browser_does_not_clear_foreign_module_globals():
    mine = _FakeBrowser("mine")
    foreign = _FakeBrowser("foreign")
    reg._tls_set_browser(mine)
    reg._tls_set_page(mine)
    # Module globals currently mirror another worker.
    reg.browser = foreign
    reg.page = foreign

    reg.stop_browser()

    assert reg._tls_get_browser() is None
    assert reg._tls_get_page() is None
    assert mine.quit_calls == 1
    assert reg.browser is foreign
    assert reg.page is foreign


def test_is_playwright_thread_error_detects_greenlet_message():
    assert reg._is_playwright_thread_error(
        Exception("Cannot switch to a different thread\nCurrent: x\nExpected: y")
    )
    assert not reg._is_playwright_thread_error(Exception("timeout waiting for selector"))


def test_two_threads_keep_separate_tls_browsers():
    barrier = threading.Barrier(2, timeout=30)
    seen: dict[str, object] = {}
    errors: list[BaseException] = []

    def worker(name: str) -> None:
        try:
            fake = _FakeBrowser(name)
            reg._tls_set_browser(fake)
            reg._tls_set_page(fake)
            barrier.wait()
            # After both set TLS, each must still see its own object.
            assert reg._tls_get_browser() is fake
            assert reg._tls_get_page() is fake
            seen[name] = reg._tls_get_browser()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            reg._tls_set_browser(None)
            reg._tls_set_page(None)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not errors, errors
    assert set(seen) == {"a", "b"}
    assert seen["a"] is not seen["b"]
