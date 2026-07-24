"""Process lifecycle: cooperative cancel + reliable interrupt handling.

Problems this module solves
---------------------------
1. Custom SIGINT handlers that only set a flag but never force-exit on the 2nd
   press → "Ctrl+C does nothing / is delayed".
2. Long blocking waits (egress first-ready) that ignore cancel → process keeps
   creating Droplets after the user asked to stop.
3. Opaque "正在停止" with no signal identity → hard to tell real Ctrl+C from
   other console events (CTRL_CLOSE, SIGTERM, etc.).
4. register workers using a DummyStop cancel callback that never fires.

Design
------
- One process-wide ``CancelState`` (threading.Event + press counter + reason).
- First interrupt → cooperative cancel (workers poll ``is_cancelled()``).
- Second interrupt → best-effort cleanup thread + hard ``os._exit``.
- Windows: also install a console control handler so CTRL_CLOSE / CTRL_BREAK
  are visible and logged (not silent process death).
- Waits should use ``wait_event`` / ``sleep`` so they wake within ~0.25s of cancel.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from typing import Callable

# ── state ──────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_cancel = threading.Event()
_presses = 0
_reason = ""
_source = ""  # e.g. "SIGINT", "CTRL_C_EVENT", "api"
_installed = False
_force_exit_code = 130
_cleanup_hooks: list[Callable[[], None]] = []
_force_cleanup_started = False
_last_press_mono = 0.0

# How long the 2nd press waits for cleanup hooks before hard exit.
_FORCE_CLEANUP_JOIN_S = 8.0
# Windows often delivers both Console CTRL_C and SIGINT for one keypress.
_PRESS_DEBOUNCE_S = 0.2


def is_cancelled() -> bool:
    return _cancel.is_set()


def cancel_event() -> threading.Event:
    """Shared event for code that already waits on threading.Event."""
    return _cancel


def cancel_reason() -> str:
    with _lock:
        return _reason


def cancel_source() -> str:
    with _lock:
        return _source


def press_count() -> int:
    with _lock:
        return _presses


def reset_for_tests() -> None:
    """Test-only: clear cancel state (does not uninstall OS handlers)."""
    global _presses, _reason, _source, _force_cleanup_started, _last_press_mono
    with _lock:
        _presses = 0
        _reason = ""
        _source = ""
        _force_cleanup_started = False
        _last_press_mono = 0.0
        _cancel.clear()


def register_cleanup_hook(fn: Callable[[], None]) -> None:
    """Register a best-effort cleanup called on force-exit (2nd interrupt)."""
    if not callable(fn):
        return
    with _lock:
        if fn not in _cleanup_hooks:
            _cleanup_hooks.append(fn)


def request_cancel(
    reason: str = "cancel",
    *,
    source: str = "api",
    force: bool = False,
    debounce: bool = True,
) -> int:
    """Request cooperative cancel. Returns press count after this request.

    If ``force`` or press count reaches 2+, starts force-exit path.
    Safe to call from any thread (including signal/console handlers).

    Duplicate deliveries within ``_PRESS_DEBOUNCE_S`` (SIGINT + CTRL_C for the
    same keypress) coalesce into a single press unless ``debounce=False``.
    """
    global _presses, _reason, _source, _last_press_mono

    now = time.monotonic()
    with _lock:
        if (
            debounce
            and not force
            and _presses > 0
            and (now - _last_press_mono) < _PRESS_DEBOUNCE_S
        ):
            _cancel.set()
            return _presses
        _presses += 1
        _last_press_mono = now
        n = _presses
        if not _reason:
            _reason = str(reason or "cancel")
        # Prefer the latest concrete source for diagnostics
        _source = str(source or "api")
        src = _source
        why = _reason

    _cancel.set()
    _emit_stop_message(n, src, why)

    if force or n >= 2:
        _force_exit(n, src)
    return n


def _emit_stop_message(n: int, source: str, reason: str) -> None:
    try:
        if n <= 1:
            msg = (
                f"\n[!] 正在停止… source={source} reason={reason!r} "
                f"（将清理 DO Droplet；再按一次 Ctrl+C 强制结束）\n"
            )
        else:
            msg = (
                f"\n[!] 强制结束 source={source} presses={n} "
                f"（跳过优雅等待，尽力清理后退出）\n"
            )
        # signal-safe-ish: write + flush stderr
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        pass


def _force_exit(n: int, source: str) -> None:
    """Second+ interrupt: run cleanup hooks briefly, then hard-exit."""
    global _force_cleanup_started
    with _lock:
        if _force_cleanup_started:
            # Triple+ press: die immediately
            try:
                os._exit(_force_exit_code)
            except Exception:
                raise SystemExit(_force_exit_code)
        _force_cleanup_started = True
        hooks = list(_cleanup_hooks)

    def _run() -> None:
        for fn in hooks:
            try:
                fn()
            except Exception as exc:
                try:
                    sys.stderr.write(f"[!] force cleanup hook failed: {exc}\n")
                    sys.stderr.flush()
                except Exception:
                    pass

    try:
        t = threading.Thread(target=_run, name="lifecycle-force-cleanup", daemon=True)
        t.start()
        t.join(timeout=_FORCE_CLEANUP_JOIN_S)
    except Exception:
        pass
    try:
        sys.stderr.write(
            f"[!] os._exit({_force_exit_code}) after force cleanup "
            f"(source={source} presses={n})\n"
        )
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(_force_exit_code)


def wait_event(
    event: threading.Event,
    timeout: float | None = None,
    *,
    poll_s: float = 0.25,
) -> bool:
    """Wait for ``event`` or cancel. Returns True if event is set (not cancel).

    Wakes within ~poll_s of a cancel request even if ``event`` never fires.
    """
    if event.is_set():
        return True
    deadline = None if timeout is None else (time.monotonic() + max(0.0, float(timeout)))
    poll = max(0.05, float(poll_s))
    while True:
        if is_cancelled():
            return False
        if event.is_set():
            return True
        slice_s = poll
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                return bool(event.is_set())
            slice_s = min(slice_s, left)
        event.wait(timeout=slice_s)


def sleep(seconds: float, *, poll_s: float = 0.25) -> bool:
    """Sleep up to ``seconds`` unless cancelled. Returns True if full sleep done."""
    if seconds <= 0:
        return not is_cancelled()
    deadline = time.monotonic() + float(seconds)
    poll = max(0.05, float(poll_s))
    while True:
        if is_cancelled():
            return False
        left = deadline - time.monotonic()
        if left <= 0:
            return True
        time.sleep(min(poll, left))


def should_stop() -> bool:
    """Alias used as cancel_callback for register / pace helpers."""
    return is_cancelled()


# ── OS handlers ────────────────────────────────────────────────────────────


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except Exception:
        return f"signal:{signum}"


def _on_signal(signum: int, frame) -> None:  # noqa: ANN001
    # Never raise from here — raising KeyboardInterrupt while main is inside
    # non-reentrant C/locks causes "delayed" or stuck stops.
    try:
        request_cancel(
            reason="interrupt",
            source=_signal_name(int(signum)),
        )
    except Exception:
        # Last resort: mark cancel without messaging
        _cancel.set()


def _install_posix_signals() -> None:
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except Exception:
            pass
    # Windows Ctrl+Break
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        try:
            signal.signal(sigbreak, _on_signal)
        except Exception:
            pass


def _install_windows_console_handler() -> bool:
    """Extra Windows console events (CTRL_CLOSE etc.) with explicit source tags.

    Python already maps Ctrl+C → SIGINT; we still install a handler so CLOSE
    and BREAK are logged and cooperative cancel runs before the process dies.
    Returning True marks the event as handled (prevents default hard kill for
    some events, giving us time to clean Droplets).
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    # CTRL_* event codes
    CTRL_C_EVENT = 0
    CTRL_BREAK_EVENT = 1
    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6

    names = {
        CTRL_C_EVENT: "CTRL_C_EVENT",
        CTRL_BREAK_EVENT: "CTRL_BREAK_EVENT",
        CTRL_CLOSE_EVENT: "CTRL_CLOSE_EVENT",
        CTRL_LOGOFF_EVENT: "CTRL_LOGOFF_EVENT",
        CTRL_SHUTDOWN_EVENT: "CTRL_SHUTDOWN_EVENT",
    }

    HandlerRoutine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    @HandlerRoutine
    def _handler(ctrl_type: int) -> bool:  # type: ignore[misc]
        name = names.get(int(ctrl_type), f"CTRL_UNKNOWN({ctrl_type})")
        try:
            # Debounce merges CTRL_C + SIGINT for the same physical keypress.
            request_cancel(reason="console", source=name)
        except Exception:
            _cancel.set()
        # True = we handled it. Combined with our SIGINT handler + debounce,
        # cooperative cancel starts immediately even if the main thread is in
        # a long C call (signal only runs on main; console handler runs in a
        # system thread on Windows).
        return True

    try:
        # Keep a process-global reference so the callback is not GC'd.
        global _win_ctrl_handler  # noqa: PLW0603
        _win_ctrl_handler = _handler  # type: ignore[name-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not kernel32.SetConsoleCtrlHandler(_win_ctrl_handler, True):
            return False
        return True
    except Exception:
        return False


_win_ctrl_handler = None  # kept alive for ctypes


def install_signal_handlers(*, force: bool = False) -> None:
    """Install interrupt handlers once (idempotent unless force=True)."""
    global _installed
    if _installed and not force:
        return
    _install_posix_signals()
    try:
        _install_windows_console_handler()
    except Exception:
        pass
    _installed = True


def format_status() -> str:
    with _lock:
        return (
            f"cancelled={_cancel.is_set()} presses={_presses} "
            f"source={_source!r} reason={_reason!r}"
        )


def dump_cancel_context() -> str:
    """Short diagnostic string for logs when exiting due to cancel."""
    try:
        stack = ""
        if sys.version_info >= (3, 11):
            # optional: main-thread stack not always available
            pass
        return f"{format_status()}{stack}"
    except Exception:
        return format_status()
