# DERISK: Stay-Resident Re-Attach After a Crash
# Task 2.5.0.4 — empirically documents what happens when you call
# DebugSession.attach_local(pid) on a PID that has already crashed and been
# detached from.
#
# OBSERVED BEHAVIOR (recorded on first run — authoritative answer for Task 2.5.2.1):
#   Platform  : Windows 11 Pro (build 26200)
#   pybag     : 2.2.16
#   WinDbg    : 10.0.26100.1742 AMD64
#   Outcome   : Re-attach to a crashed PID returns status='failed',
#               message='-805306102' (NTSTATUS 0xC000010A).
#               DbgEng error: "Cannot debug pid <N>, NTSTATUS 0xC000010A
#               (An attempt was made to access an exiting process.)"
#               The crashed process has already been terminated by the OS
#               once pybag terminated/released it, so the PID no longer
#               exists as an attachable target.
#
# IMPLICATION FOR TASK 2.5.2.1:
#   The stay-resident loop should NOT attempt re-attach after a crash.
#   Instead: detect the crash, surface the exception, call detach(), then
#   exit cleanly with a log message.  The --max-crashes > 1 scenario is
#   only feasible if the target is configured to wait for a debugger
#   (e.g. "wait" mode or with a custom exception handler that blocks).

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from stackly.session import DebugSession

_CRASH_APP = Path(__file__).parent / "fixtures" / "crash_app" / "build" / "Debug" / "crash_app.exe"


# ---------------------------------------------------------------------------
# Derisk test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_reattach_after_exception_returns_failed_or_attached(crash_app_path: Path) -> None:
    """Empirically answers: after detaching from a crashed PID, can we re-attach?

    Strategy
    --------
    We use ``UserDbg.create()`` to launch crash_app under the debugger
    (matching test_catch_null_deref_crash_via_create) so we can catch the
    crash *while attached*.  Plain subprocess + attach_local fails because the
    process exits immediately, leaving no attachable PID.

    Steps
    -----
    1. ``UserDbg.create("crash_app null", initial_break=True)`` — process
       created under debugger, paused at entry.
    2. ``dbg.go()`` — resumes; the null dereference fires; pybag blocks until
       the crash event is received.
    3. Confirm crash via session.get_exception() — must be 0xC0000005.
    4. Record the PID (``dbg.pid``).
    5. Terminate the target (so it is definitely dead, matching the
       post-crash state the stay-resident loop will encounter).
    6. Manually clear session._dbg and release the first UserDbg so the
       session is back to "not attached".
    7. ``session.attach_local(pid)`` — attempt re-attach to the dead PID.
    8. Assert one of the two accepted outcomes:
       a. status == "failed"  — expected: process is dead, DbgEng refuses.
       b. status == "attached" — DbgEng allowed re-attaching to the corpse;
          verify get_exception() still reports 0xC0000005.

    Either outcome is valid.  The assertion text records which one occurred so
    Task 2.5.2.1 can code the stay-resident loop accordingly.

    Cleanup note
    ------------
    We do NOT call session.detach() on a crashed process because
    ``UserDbg.detach()`` can hang when the target is already dead.  Instead we
    replicate the safe cleanup pattern from test_catch_null_deref_crash_via_create:
    terminate() → clear session._dbg → Release().
    """
    from stackly.env import ensure_dbgeng_on_path

    ensure_dbgeng_on_path()
    from pybag.userdbg import UserDbg

    # ---- Step 1: create process under debugger ----
    dbg = UserDbg()
    dbg.create(f'"{crash_app_path}" null', initial_break=True)

    session = DebugSession()
    session._dbg = dbg  # type: ignore[attr-defined]
    session._process_name = "crash_app.exe"  # type: ignore[attr-defined]

    crashed_pid: int | None = None
    try:
        # ---- Step 2: run until crash ----
        dbg.go()

        # ---- Step 3: confirm crash visible ----
        exc = session.get_exception()
        assert exc is not None, (
            "Expected an exception after crash_app null, got None."
        )
        assert exc.code == 0xC0000005, (
            f"Expected EXCEPTION_ACCESS_VIOLATION (0xC0000005), got {exc.code:#x}."
        )

        # ---- Step 4: record PID before cleanup ----
        crashed_pid = dbg.pid

        # ---- Step 5-6: terminate and release the first debugger instance ----
        # Use the safe cleanup pattern from test_catch_null_deref_crash_via_create:
        # terminate → clear reference → Release.  Avoids a potential hang from
        # calling detach() on a process that is already dead.
        with contextlib.suppress(Exception):
            dbg.terminate()
        session._dbg = None  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            dbg.Release()

        # ---- Step 7: re-attach attempt ----
        assert crashed_pid is not None
        second_attach = session.attach_local(crashed_pid)

        # ---- Step 8: accept either outcome, record which one happened ----
        if second_attach.status == "failed":
            # Expected path: the target is dead; DbgEng refuses.
            # Task 2.5.2.1 should implement: on re-attach failure, log the
            # message and exit the stay-resident loop cleanly.
            assert second_attach.message, (
                "Re-attach failed but returned no message — stay-resident loop "
                "needs a human-readable reason to surface to the user."
            )
            print(
                f"\n[DERISK RESULT] Re-attach to crashed PID {crashed_pid} returned "
                f"status='failed', message={second_attach.message!r}. "
                "Task 2.5.2.1: implement exit-on-reattach-failure path."
            )

        elif second_attach.status == "attached":
            # Acceptable path: DbgEng allowed re-attaching to the post-mortem target.
            # Verify the crash is still visible so the stay-resident loop has
            # something useful to do.
            exc2 = session.get_exception()
            assert exc2 is not None, (
                "Re-attach succeeded but get_exception() returned None — "
                "the crash event was lost, making re-attach useless for triage."
            )
            assert exc2.code == 0xC0000005, (
                f"Re-attach succeeded but exception code changed: "
                f"first={exc.code:#x}, second={exc2.code:#x}."
            )
            print(
                f"\n[DERISK RESULT] Re-attach to crashed PID {crashed_pid} returned "
                f"status='attached'. Exception still visible: {exc2.code_name!r}. "
                "Task 2.5.2.1: stay-resident loop can inspect and iterate."
            )

        else:
            pytest.fail(
                f"Unexpected second_attach.status={second_attach.status!r}. "
                "AttachResult.status must be 'attached' or 'failed'."
            )

    finally:
        # Close the session (second attach, if it succeeded).
        # If the second attach failed, session._dbg is already None.
        session.close()
