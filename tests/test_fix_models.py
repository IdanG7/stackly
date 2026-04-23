"""Pydantic model roundtrip tests for the fix subpackage. Pure unit — no pybag, no MCP."""

from __future__ import annotations

from pathlib import Path

from stackly.fix.models import (
    AttemptRecord,
    ClaudeRunResult,
    CrashCapture,
    FixResult,
)
from stackly.models import CallFrame, ExceptionInfo, Local, ThreadInfo


def test_crash_capture_round_trip() -> None:
    """Phase 2a.0.3 primary acceptance: CrashCapture .model_dump() .model_validate() round-trip."""
    capture = CrashCapture(
        pid=1234,
        process_name="crash_app.exe",
        binary_path="D:/x/crash_app.exe",
        exception=ExceptionInfo(
            code=0xC0000005,
            code_name="EXCEPTION_ACCESS_VIOLATION",
            address=0x7FF612341234,
            description="Access violation",
            is_first_chance=True,
            faulting_thread_tid=5678,
        ),
        callstack=[
            CallFrame(
                index=0, function="crash_null", module="crash_app", instruction_pointer=0xDEAD
            ),
        ],
        threads=[ThreadInfo(id=0, tid=5678, state="break", is_current=True)],
        locals_=[Local(name="bad_pointer", type="int*", value="nullptr")],
        crash_hash="a1b2c3d4",
    )
    restored = CrashCapture.model_validate(capture.model_dump())
    assert restored == capture


def test_crash_capture_accepts_degenerate_state() -> None:
    """No exception, no stack, no locals — still a valid CrashCapture (wait-mode process)."""
    capture = CrashCapture(pid=0, crash_hash="unknown")
    assert capture.exception is None
    assert capture.callstack == []
    assert capture.threads == []
    assert capture.locals_ == []


def test_claude_run_result_defaults() -> None:
    """ClaudeRunResult tracks both success and failure shapes."""
    ok = ClaudeRunResult(
        ok=True,
        is_error=False,
        subtype="success",
        result="fix applied",
        session_id="abc",
        total_cost_usd=0.15,
        input_tokens=18000,
        output_tokens=2100,
        num_turns=5,
        duration_ms=42000,
        returncode=0,
    )
    restored = ClaudeRunResult.model_validate(ok.model_dump())
    assert restored == ok
    assert ok.raw_stdout == ""
    assert ok.raw_stderr == ""


def test_attempt_record_test_fields_optional() -> None:
    """test_ok / test_output default to None when --test-cmd wasn't provided.

    This is the schema enforced by plan-check fix C2 — no retroactive mutation.
    """
    claude_result = ClaudeRunResult(
        ok=True,
        is_error=False,
        subtype="success",
        result="",
        session_id="x",
        total_cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        num_turns=0,
        duration_ms=0,
        returncode=0,
    )
    attempt = AttemptRecord(
        attempt=1,
        claude_result=claude_result,
        build_ok=True,
        build_output="build passed",
        duration_s=3.5,
    )
    assert attempt.test_ok is None
    assert attempt.test_output is None

    # With tests run
    attempt_with_tests = AttemptRecord(
        attempt=2,
        claude_result=claude_result,
        build_ok=True,
        build_output="build passed",
        test_ok=False,
        test_output="1 test failed",
        duration_s=10.2,
    )
    assert attempt_with_tests.test_ok is False
    assert attempt_with_tests.test_output == "1 test failed"


def test_fix_result_round_trip_both_modes() -> None:
    """FixResult must round-trip for both handoff and auto modes."""
    handoff = FixResult(
        ok=True,
        mode="handoff",
        crash_hash="abc12345",
        patch_path=None,
        failure_report_path=None,
        attempts=[],
        total_cost_usd=0.0,
        total_input_tokens=0,
        total_output_tokens=0,
        worktree_path=None,
        worktree_preserved=False,
    )
    restored_h = FixResult.model_validate(handoff.model_dump(mode="json"))
    assert restored_h.mode == "handoff"
    assert restored_h.patch_path is None

    auto = FixResult(
        ok=True,
        mode="auto",
        crash_hash="abc12345",
        patch_path=Path(".stackly/patches/crash-abc12345.patch"),
        failure_report_path=None,
        attempts=[],
        total_cost_usd=0.18,
        total_input_tokens=18500,
        total_output_tokens=2100,
        worktree_path=Path(".stackly/wt-abc12345"),
        worktree_preserved=False,
    )
    restored_a = FixResult.model_validate(auto.model_dump(mode="json"))
    assert restored_a.mode == "auto"
    assert str(restored_a.patch_path).endswith("crash-abc12345.patch")


def test_fix_models_import_without_pybag_or_mcp() -> None:
    """fix/__init__.py and fix/models.py must be importable without Debugging Tools or MCP loaded.

    Phase 1 invariant — protects the lazy-pybag-import path.
    """
    import sys

    import stackly.fix
    import stackly.fix.models

    # Touch the modules so the imports aren't pruned and to prove they load.
    assert stackly.fix is not None
    assert stackly.fix.models is not None
    # Verify nothing pulled in pybag.
    assert "pybag" not in sys.modules
