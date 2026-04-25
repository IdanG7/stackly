"""Derisk: non-exception-break handling in the polling loop.

Task 2.5.0.3 — empirically confirms that after an initial BREAK event
(no *crash* exception), the polling loop can correctly distinguish
debugger-synthetic breaks from real crashes, and that ``dbg.cmd("g")``
resumes the target cleanly without getting stuck.

EMPIRICAL FINDINGS (observed on pybag 2.2.16 + WinDbg 10.0.26100.1742,
Windows 11 Pro):

  INITIAL BREAK SHAPE:
  When attaching with ``initial_break=True``, DbgEng delivers:
    "(pid.tid): Break instruction exception - code 80000003 (first chance)"
  This IS parsed by ``_LASTEVENT_RE`` — it has an exception code and
  "first chance". Code 0x80000003 == EXCEPTION_BREAKPOINT is a synthetic
  debugger event, NOT a real crash.

  CONSEQUENCE FOR TASK 2.5.1.2:
  The production ``wait_for_exception()`` polling loop MUST NOT use
  ``_LASTEVENT_RE`` alone to decide "crash detected". It must additionally
  filter out synthetic exception codes:
    - 0x80000003  EXCEPTION_BREAKPOINT  (attach initial break, int3)
    - 0x80000004  EXCEPTION_SINGLE_STEP (step, trace flag)
  Only codes outside these two should be treated as crash-worthy events.
  Implementation sketch for Task 2.5.1.2::

      _SYNTHETIC_CODES = frozenset({0x80000003, 0x80000004})

      m = _LASTEVENT_RE.search(last_event_output)
      if m and int(m.group("code"), 16) not in _SYNTHETIC_CODES:
          return WatchException(exception=exc)   # real crash
      else:
          dbg.cmd("g", quiet=True)               # synthetic break -- resume

  MODULE-LOAD FLOODING:
  No module-load flooding observed (only a small number of BREAKs at
  startup). SetEngineOptions tuning is NOT required before Task 2.5.1.2.
"""

from __future__ import annotations

import contextlib
import subprocess
import time

import pytest

# _LASTEVENT_RE is the authoritative regex for distinguishing exception-breaks
# from non-exception breaks; we import it directly so this test mirrors exactly
# the pattern that the production polling loop (Task 2.5.1.2) will use.
from stackly.session import _LASTEVENT_RE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_ITERATIONS = 10
_WAIT_TIMEOUT_S = 1  # seconds per dbg.wait() call
_TARGET_LIVE_TICKS = 5  # assert no crash detected after this many ticks

# Exception codes that are debugger-synthetic and must NOT be treated as crashes.
# 0x80000003 = EXCEPTION_BREAKPOINT (attach initial break, int3)
# 0x80000004 = EXCEPTION_SINGLE_STEP (trace flag, single-step breakpoint)
_SYNTHETIC_CODES: frozenset[int] = frozenset({0x80000003, 0x80000004})


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_wait_loop_survives_initial_break_without_returning_exception(
    crash_app_waiting: subprocess.Popen,
) -> None:
    """Polling loop on a non-crashing target must NOT detect any crash exception.

    Steps:
    1. Attach to ``crash_app wait`` (blocked on stdin, will not crash).
    2. Run an inline mini polling loop (up to _POLL_ITERATIONS ticks, 1 s each).
    3. On each BREAK: inspect ``.lastevent`` with _LASTEVENT_RE.
       - If it matches AND the code is NOT in _SYNTHETIC_CODES => real crash
         detected => FAIL (should not happen on crash_app wait).
       - If it matches but the code IS in _SYNTHETIC_CODES => synthetic debugger
         break (initial break, module-load breakpoint) => ``g`` to resume.
       - If it does NOT match => non-exception break => ``g`` to resume.
    4. After _TARGET_LIVE_TICKS ticks without a crash, assert:
       a. crash_detected is False.
       b. The target process is still alive.
       c. At least one BREAK (the initial attach break) was seen and resumed.

    KEY FINDING documented in the module docstring above: the attach initial
    break fires as ``code 80000003 (first chance)`` which _LASTEVENT_RE DOES
    match. The production polling loop MUST filter out _SYNTHETIC_CODES.
    """
    from stackly.env import ensure_dbgeng_on_path

    ensure_dbgeng_on_path()
    from pybag.userdbg import UserDbg

    dbg = UserDbg()
    dbg.attach(crash_app_waiting.pid, initial_break=True)

    crash_detected = False
    breaks_seen = 0
    synthetic_breaks = 0
    ticks_completed = 0

    try:
        start = time.monotonic()

        for _tick in range(_POLL_ITERATIONS):
            # pybag's wait() blocks until the next debug event or the timeout
            # elapses. Return value is the DEBUG_WAIT_* constant; we don't
            # inspect it here -- exec_status() tells us what we need.
            with contextlib.suppress(Exception):
                dbg.wait(timeout=_WAIT_TIMEOUT_S)

            status: str = dbg.exec_status()
            ticks_completed += 1

            if "NO_DEBUGGEE" in status.upper():
                # Target exited unexpectedly -- abort early. This is not a test
                # failure here (the crash_app wait mode should stay alive) but
                # we note it and break out.
                break

            if "BREAK" in status.upper():
                breaks_seen += 1
                last_event_output: str = dbg.cmd(".lastevent", quiet=True)

                m = _LASTEVENT_RE.search(last_event_output)
                if m:
                    code = int(m.group("code"), 16)
                    if code in _SYNTHETIC_CODES:
                        # Debugger-synthetic break (initial attach break, int3,
                        # single-step). Resume and continue polling.
                        synthetic_breaks += 1
                        dbg.cmd("g", quiet=True)
                    else:
                        # A non-synthetic exception code was detected on a
                        # non-crashing target. This is unexpected.
                        crash_detected = True
                        break
                else:
                    # No exception shape at all -- resume.
                    dbg.cmd("g", quiet=True)

            if ticks_completed >= _TARGET_LIVE_TICKS:
                # We have enough data -- stop early to keep the test snappy.
                break

        elapsed = time.monotonic() - start

        # ------------------------------------------------------------------
        # Module-load flooding check (informational, not a hard failure).
        # If >= 10 breaks/sec observed, recommend SetEngineOptions tuning.
        # ------------------------------------------------------------------
        if elapsed > 0 and breaks_seen / elapsed >= 10:
            import warnings

            warnings.warn(
                f"Module-load flooding detected: {breaks_seen} BREAKs in "
                f"{elapsed:.1f}s ({breaks_seen / elapsed:.1f}/s). "
                "Add SetEngineOptions(DEBUG_ENGOPT_IGNORE_LOADER_BREAKPOINT) "
                "in Task 2.5.1.2 before shipping the production polling loop.",
                stacklevel=2,
            )

        # ------------------------------------------------------------------
        # Core assertions
        # ------------------------------------------------------------------

        # 1. No real crash exception detected during the polling loop.
        assert not crash_detected, (
            "Polling loop detected a non-synthetic exception on crash_app wait. "
            f"breaks_seen={breaks_seen}, synthetic_breaks={synthetic_breaks}. "
            "This is a false positive -- check _SYNTHETIC_CODES filtering."
        )

        # 2. We completed at least _TARGET_LIVE_TICKS polling iterations, which
        #    proves the loop did not get stuck on a single BREAK event.
        assert ticks_completed >= _TARGET_LIVE_TICKS, (
            f"Loop completed only {ticks_completed} ticks (wanted {_TARGET_LIVE_TICKS}). "
            "The target may have exited early or pybag wait() misbehaved."
        )

        # 3. Target process is still alive (poll() returns None for live procs).
        assert crash_app_waiting.poll() is None, (
            "crash_app wait process exited unexpectedly during the polling loop."
        )

        # 4. At least one BREAK was observed (the initial attach break).
        #    This confirms the resume path (dbg.cmd("g")) was exercised.
        assert breaks_seen >= 1, (
            "Expected at least one BREAK (initial attach break). "
            f"breaks_seen={breaks_seen}. "
            "If zero breaks were seen the attach or wait() call may be broken."
        )

    finally:
        with contextlib.suppress(Exception):
            dbg.detach()
        with contextlib.suppress(Exception):
            dbg.Release()
