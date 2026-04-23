"""Pydantic model roundtrip tests. Pure unit — no pybag, no DbgEng."""

from __future__ import annotations

from stackly.models import (
    AttachResult,
    Breakpoint,
    CallFrame,
    ExceptionInfo,
    Local,
    StepResult,
    ThreadInfo,
)


def test_attach_result_roundtrip() -> None:
    original = AttachResult(pid=1234, process_name="crash_app.exe", status="attached")
    loaded = AttachResult.model_validate(original.model_dump())
    assert loaded == original


def test_call_frame_optional_fields_accepted() -> None:
    # File and line are Optional — MVP can return frames without them.
    frame = CallFrame(index=0, function="main", module="app", instruction_pointer=0xDEADBEEF)
    assert frame.file is None
    assert frame.line is None


def test_exception_info_roundtrip() -> None:
    exc = ExceptionInfo(
        code=0xC0000005,
        code_name="EXCEPTION_ACCESS_VIOLATION",
        address=0x7FF612341234,
        description="Access violation",
        is_first_chance=True,
        faulting_thread_tid=5678,
    )
    loaded = ExceptionInfo.model_validate(exc.model_dump())
    assert loaded.code_name == "EXCEPTION_ACCESS_VIOLATION"


def test_thread_info_defaults() -> None:
    t = ThreadInfo(id=0, tid=1234, state="stopped")
    assert t.is_current is False
    assert t.frame_count is None


def test_local_truncation_flag() -> None:
    big = Local(name="buf", type="char*", value="x" * 300, truncated=True)
    assert big.truncated


def test_breakpoint_defaults() -> None:
    bp = Breakpoint(id=0, location="myapp!crash_null")
    assert bp.enabled is True
    assert bp.hit_count == 0


def test_step_result_null_frame() -> None:
    r = StepResult(status="exited", current_frame=None)
    assert r.status == "exited"
