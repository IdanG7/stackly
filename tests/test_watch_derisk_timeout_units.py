# Derisk Task 2.5.0.1 -- pybag wait() timeout-unit semantics
#
# PURPOSE
# -------
# Empirically confirm RESEARCH.md section 2.1's claim that pybag's
# UserDbg.wait(timeout) takes SECONDS, not milliseconds, and that
# timeout=1 is the effective floor.
#
# HOW TO INTERPRET RESULTS
# ------------------------
# This test attaches pybag to a live crash_app process that is blocked on
# stdin.  It calls dbg.wait(timeout=N) with timeout values of 2 and 1.
# Because the target never raises a debug event, each call should time-out
# after exactly N seconds (per the _worker_wait implementation in
# pybag/pydbg.py:256-276).
#
# Two separate crash_app processes are used (one per measurement) because
# after wait() times out, pybag calls SetInterrupt internally which leaves
# the DbgEng engine in a state where the next WaitForEvent returns
# immediately.  Re-attaching to the same process is also unreliable
# immediately after detach.  Separate processes give each measurement a
# clean DbgEng state.
#
# CALIBRATION (recorded on first passing run on this machine)
# -----------------------------------------------------------
# Machine: Windows 11 Pro, Python 3.14.1, pybag 2.2.16
# WinDbg:  10.0.26100.1742 AMD64
# Observed dbg.wait(timeout=2) elapsed: 2.014s  (accepted window: [1.8, 3.5])
# Observed dbg.wait(timeout=1) elapsed: 1.001s  (accepted window: [0.8, 2.5])
# Confirms: timeout unit is SECONDS; 1-second floor is the minimum that works.
#
# SKIP POLICY
# -----------
# The test auto-skips when either:
#   - crash_app.exe has not been built  (conftest.py gate)
#   - Windows Debugging Tools are not installed  (conftest.py gate)
# Both skip conditions are managed by conftest.py's pytest_collection_modifyitems
# hook -- no extra skip logic needed here.
#
# IF THIS TEST FAILS
# ------------------
# If the observed elapsed times fall outside the acceptance windows it means
# pybag's wait() semantics differ from what RESEARCH.md section 2.1 describes
# (e.g. a different pybag version).  STOP all Phase 2.5 downstream work and
# update the plan before proceeding -- the poll_s=1 floor assumption is
# load-bearing.

from __future__ import annotations

import contextlib
import subprocess
import time
from pathlib import Path

import pytest

from stackly.env import ensure_dbgeng_on_path


def _launch_crash_app_waiting(crash_app_path: Path) -> subprocess.Popen:
    """Start crash_app in wait mode and block until it is ready."""
    proc = subprocess.Popen(
        [str(crash_app_path), "wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().decode(errors="replace")
    assert "crash_app pid=" in line, f"unexpected first line: {line!r}"
    time.sleep(0.2)
    return proc


def _timed_wait(proc: subprocess.Popen, timeout_s: int) -> float:
    """Attach a fresh UserDbg to proc.pid, call wait(timeout_s), return elapsed seconds.

    After the timed wait completes, the target process is killed so that DbgEng's
    event thread exits cleanly (killing the target is much faster than detach when
    the session is in a post-interrupt state).
    """
    from pybag.userdbg import UserDbg

    dbg = UserDbg()
    dbg.attach(proc.pid)
    try:
        t0 = time.monotonic()
        dbg.wait(timeout=timeout_s)
        elapsed = time.monotonic() - t0
    finally:
        # Kill the target process first -- this causes DbgEng's WaitForEvent
        # to return with E_UNEXPECTED (no debuggee) rather than waiting for a
        # pending interrupt to resolve.  Much faster than detach() after a
        # timed-out wait().
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            dbg.detach()
    return elapsed


@pytest.mark.integration
def test_wait_timeout_seconds_semantics_against_waiting_target(
    crash_app_path: Path,
) -> None:
    """Empirically verify dbg.wait(timeout) units are SECONDS, with 1-second floor.

    Acceptance windows (from RESEARCH.md section 2.1 + plan section 4 task 2.5.0.1):
      dbg.wait(timeout=2) elapsed in [1.8, 3.5] s
      dbg.wait(timeout=1) elapsed in [0.8, 2.5] s
    """
    ensure_dbgeng_on_path()

    # --- timeout=2 measurement (separate process for clean DbgEng state) ---
    proc_2 = _launch_crash_app_waiting(crash_app_path)
    elapsed_2 = _timed_wait(proc_2, timeout_s=2)

    # --- timeout=1 measurement (separate process for clean DbgEng state) ---
    proc_1 = _launch_crash_app_waiting(crash_app_path)
    elapsed_1 = _timed_wait(proc_1, timeout_s=1)

    # Record calibration numbers in output for future contributors.
    print(f"\nCalibration: dbg.wait(timeout=2) elapsed = {elapsed_2:.3f}s")
    print(f"Calibration: dbg.wait(timeout=1) elapsed = {elapsed_1:.3f}s")

    # Acceptance: timeout=2 should block for approximately 2 seconds.
    assert 1.8 <= elapsed_2 <= 3.5, (
        f"dbg.wait(timeout=2) took {elapsed_2:.3f}s -- expected [1.8, 3.5]s. "
        "If it was ~0.002s the unit is ms not s. If it was ~2000s the unit is ms. "
        "Either way: STOP and update the plan."
    )

    # Acceptance: timeout=1 is the documented floor; should block ~1 second.
    assert 0.8 <= elapsed_1 <= 2.5, (
        f"dbg.wait(timeout=1) took {elapsed_1:.3f}s -- expected [0.8, 2.5]s. "
        "If it was < 0.1s the sub-second-broken path fired. "
        "Either way: STOP and update the plan."
    )
