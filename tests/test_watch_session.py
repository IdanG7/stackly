"""Unit tests for DebugSession.wait_for_exception() — no live pybag, no DbgEng.

All tests use a fake UserDbg stub injected via session._dbg. No pybag import
anywhere in this file — the stub is a pure Python object.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from stackly.models import WatchException, WatchTargetExited, WatchTimedOut
from stackly.session import DebugSession, DebugSessionError

# ---------------------------------------------------------------------------
# Fake UserDbg stub helpers
# ---------------------------------------------------------------------------

_LASTEVENT_REAL_AV = (
    "Last event: 1234.5678: Access violation - code c0000005 (first chance)\n"
    "  debugger time: Thu Apr 15 23:35:45.123 2026\n"
)

_LASTEVENT_BREAKPOINT = (
    "Last event: 1234.5678: Break instruction exception - code 80000003 (first chance)\n"
    "  debugger time: Thu Apr 15 23:35:45.123 2026\n"
)

_LASTEVENT_EMPTY = ""


class _FakeDbg:
    """Minimal fake of pybag's UserDbg, usable as session._dbg without importing pybag."""

    def __init__(
        self,
        pid: int = 1234,
        statuses: list[str] | None = None,
        lastevent_responses: dict[int, str] | None = None,
    ) -> None:
        self.pid = pid
        # statuses[i] is what exec_status() returns on the i-th call
        self._statuses: list[str] = statuses or []
        self._status_idx = 0
        # lastevent_responses: tick-index → .lastevent output string
        self._lastevent_responses: dict[int, str] = lastevent_responses or {}
        self._tick = 0
        self.cmd_calls: list[tuple[Any, ...]] = []

    def wait(self, timeout: int = 1) -> int:
        """Simulate pybag wait — sleeps for a fraction of timeout to keep tests fast."""
        # Use a small sleep to let time.monotonic() advance; dividing by 10 keeps
        # tests under 1 s even with poll_s=1.
        time.sleep(timeout * 0.05)
        return 0

    def exec_status(self) -> str:
        if self._status_idx < len(self._statuses):
            status = self._statuses[self._status_idx]
        else:
            status = "GO"
        self._status_idx += 1
        self._tick += 1
        return status

    def cmd(self, command: str, quiet: bool = False) -> str:
        self.cmd_calls.append((command, quiet))
        if command == ".lastevent":
            tick = self._tick - 1  # exec_status() already advanced tick
            return self._lastevent_responses.get(tick, _LASTEVENT_EMPTY)
        if command == ".exr -1":
            return "ExceptionAddress: 00007ff612341234"
        return ""


# ---------------------------------------------------------------------------
# Test 1 — timeout returns WatchTimedOut
# ---------------------------------------------------------------------------


def test_wait_for_exception_timeout_returns_watchtimedout() -> None:
    """Stub always returns GO; after timeout_s, WatchTimedOut is returned."""
    stub = _FakeDbg(pid=42, statuses=[])
    session = DebugSession()
    session._dbg = stub  # type: ignore[attr-defined]

    result = session.wait_for_exception(pid=42, poll_s=1, timeout_s=2)

    assert isinstance(result, WatchTimedOut), f"expected WatchTimedOut, got {result!r}"
    # We allowed poll_s=1 ticks, so elapsed should be at least 1 tick's worth.
    # With the stub's 5% sleep-per-tick the actual elapsed is tiny, but
    # result.elapsed_s should reflect the full timeout window.
    assert result.elapsed_s >= 1.9, f"elapsed_s too short: {result.elapsed_s}"
    assert result.elapsed_s <= 2.5, f"elapsed_s too long: {result.elapsed_s}"


# ---------------------------------------------------------------------------
# Test 2 — target exited returns WatchTargetExited
# ---------------------------------------------------------------------------


def test_wait_for_exception_returns_watchtargetexited_on_no_debuggee() -> None:
    """Stub returns NO_DEBUGGEE after 1 GO tick; WatchTargetExited expected."""
    stub = _FakeDbg(pid=42, statuses=["GO", "NO_DEBUGGEE"])
    session = DebugSession()
    session._dbg = stub  # type: ignore[attr-defined]

    result = session.wait_for_exception(pid=42, poll_s=1, timeout_s=30)

    assert isinstance(result, WatchTargetExited), f"expected WatchTargetExited, got {result!r}"
    assert result.elapsed_s >= 0.0


# ---------------------------------------------------------------------------
# Test 3 — real exception on BREAK returns WatchException
# ---------------------------------------------------------------------------


def test_wait_for_exception_returns_watchexception_on_break_with_lastevent() -> None:
    """Stub fires BREAK with AV lastevent; WatchException with AV code expected."""
    # exec_status() is called AFTER wait(); tick 0 = first exec_status call
    stub = _FakeDbg(
        pid=42,
        statuses=["BREAK"],
        lastevent_responses={0: _LASTEVENT_REAL_AV},
    )
    session = DebugSession()
    session._dbg = stub  # type: ignore[attr-defined]

    result = session.wait_for_exception(pid=42, poll_s=1, timeout_s=30)

    assert isinstance(result, WatchException), f"expected WatchException, got {result!r}"
    assert result.exception.code_name == "EXCEPTION_ACCESS_VIOLATION"
    assert result.exception.code == 0xC0000005


# ---------------------------------------------------------------------------
# Test 4 — non-exception BREAK is resumed, then target exits
# ---------------------------------------------------------------------------


def test_wait_for_exception_resumes_on_nonexception_break() -> None:
    """BREAK with empty .lastevent → dbg.cmd('g') called; then NO_DEBUGGEE → WatchTargetExited."""
    stub = _FakeDbg(
        pid=42,
        statuses=["BREAK", "NO_DEBUGGEE"],
        lastevent_responses={0: _LASTEVENT_EMPTY},
    )
    session = DebugSession()
    session._dbg = stub  # type: ignore[attr-defined]

    result = session.wait_for_exception(pid=42, poll_s=1, timeout_s=30)

    assert isinstance(result, WatchTargetExited), f"expected WatchTargetExited, got {result!r}"

    # Verify that dbg.cmd("g", quiet=True) was called exactly once for the resume
    resume_calls = [c for c in stub.cmd_calls if c[0] == "g"]
    assert len(resume_calls) == 1, f"expected 1 'g' cmd call, got {resume_calls}"


# ---------------------------------------------------------------------------
# Test 5 — synthetic exception code (EXCEPTION_BREAKPOINT) is resumed
# ---------------------------------------------------------------------------


def test_wait_for_exception_resumes_on_synthetic_exception_code() -> None:
    """BREAK with 0x80000003 (attach-break) must be filtered; then NO_DEBUGGEE → WatchTargetExited.

    This is the Wave-0 derisk finding: the initial attach-break fires as an
    exception code that _LASTEVENT_RE matches, but it's synthetic and must NOT
    produce WatchException.
    """
    stub = _FakeDbg(
        pid=42,
        statuses=["BREAK", "NO_DEBUGGEE"],
        lastevent_responses={0: _LASTEVENT_BREAKPOINT},
    )
    session = DebugSession()
    session._dbg = stub  # type: ignore[attr-defined]

    result = session.wait_for_exception(pid=42, poll_s=1, timeout_s=30)

    assert isinstance(result, WatchTargetExited), (
        f"expected WatchTargetExited (synthetic break filtered), got {result!r}"
    )

    # g must have been called once to resume the synthetic break
    resume_calls = [c for c in stub.cmd_calls if c[0] == "g"]
    assert len(resume_calls) == 1, f"expected 1 'g' cmd call, got {resume_calls}"


# ---------------------------------------------------------------------------
# Test 6 — mismatched PID raises DebugSessionError
# ---------------------------------------------------------------------------


def test_wait_for_exception_rejects_mismatched_pid() -> None:
    """Stub pid=1234 but caller asks for pid=9999 → DebugSessionError."""
    stub = _FakeDbg(pid=1234)
    session = DebugSession()
    session._dbg = stub  # type: ignore[attr-defined]

    with pytest.raises(DebugSessionError, match=r"attached to pid=1234"):
        session.wait_for_exception(pid=9999, poll_s=1, timeout_s=10)
