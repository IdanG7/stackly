"""Tests for WatchResult discriminated union models. Pure unit — no pybag, no DbgEng."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from stackly.models import (
    ExceptionInfo,
    WatchException,
    WatchResult,
    WatchTargetExited,
    WatchTimedOut,
)


def test_watch_result_discriminated_union_round_trip() -> None:
    adapter = TypeAdapter(WatchResult)

    # WatchException round-trip
    exc_info = ExceptionInfo(
        code=0xC0000005,
        code_name="EXCEPTION_ACCESS_VIOLATION",
        address=0x7FF612341234,
    )
    watch_exc = WatchException(exception=exc_info)
    reloaded_exc = adapter.validate_python(watch_exc.model_dump())
    assert isinstance(reloaded_exc, WatchException)
    assert reloaded_exc.outcome == "exception"
    assert reloaded_exc.exception.code_name == "EXCEPTION_ACCESS_VIOLATION"

    # WatchTimedOut round-trip
    timed_out = WatchTimedOut(elapsed_s=30.0)
    reloaded_timed_out = adapter.validate_python(timed_out.model_dump())
    assert isinstance(reloaded_timed_out, WatchTimedOut)
    assert reloaded_timed_out.outcome == "timed_out"
    assert reloaded_timed_out.elapsed_s == 30.0

    # WatchTargetExited round-trip
    target_exited = WatchTargetExited(elapsed_s=10.0)
    reloaded_target_exited = adapter.validate_python(target_exited.model_dump())
    assert isinstance(reloaded_target_exited, WatchTargetExited)
    assert reloaded_target_exited.outcome == "target_exited"
    assert reloaded_target_exited.elapsed_s == 10.0


def test_watch_result_unknown_outcome_raises_validation_error() -> None:
    adapter = TypeAdapter(WatchResult)
    with pytest.raises(ValidationError):
        adapter.validate_python({"outcome": "unknown_outcome", "elapsed_s": 5.0})
