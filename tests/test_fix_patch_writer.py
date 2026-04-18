"""Tests for debugbridge.fix.patch_writer (task 2a.3.3)."""

from __future__ import annotations

from debugbridge.fix.models import AttemptRecord, ClaudeRunResult
from debugbridge.fix.patch_writer import write_failure_report, write_patch


def _stub_claude_result(**overrides: object) -> ClaudeRunResult:
    defaults = {
        "ok": True,
        "is_error": False,
        "subtype": "success",
        "result": "Fixed the null-pointer dereference in foo.cpp line 42.",
        "session_id": "sess-001",
        "total_cost_usd": 0.0312,
        "input_tokens": 500,
        "output_tokens": 120,
        "num_turns": 3,
        "duration_ms": 8500,
        "raw_stdout": "",
        "raw_stderr": "",
        "returncode": 0,
    }
    defaults.update(overrides)
    return ClaudeRunResult(**defaults)  # type: ignore[arg-type]


def _stub_attempt(
    attempt: int = 1, build_ok: bool = True, test_ok: bool | None = None
) -> AttemptRecord:
    return AttemptRecord(
        attempt=attempt,
        claude_result=_stub_claude_result(),
        build_ok=build_ok,
        build_output="Build succeeded." if build_ok else "error: undefined reference to 'bar'",
        test_ok=test_ok,
        test_output="All tests passed." if test_ok else None,
        duration_s=12.5,
    )


def test_write_patch_round_trip(tmp_path):
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
    result = write_patch(tmp_path, "a1b2c3d4", diff)

    expected = tmp_path / ".debugbridge" / "patches" / "crash-a1b2c3d4.patch"
    assert result == expected
    assert expected.exists()
    assert expected.read_text(encoding="utf-8") == diff


def test_write_failure_report(tmp_path):
    attempts = [
        _stub_attempt(attempt=1, build_ok=False),
        _stub_attempt(attempt=2, build_ok=True, test_ok=False),
    ]
    result = write_failure_report(tmp_path, "a1b2c3d4", attempts=attempts, final_msg="gave up")

    expected = tmp_path / ".debugbridge" / "patches" / "crash-a1b2c3d4.failed.md"
    assert result == expected
    assert expected.exists()

    content = expected.read_text(encoding="utf-8")
    assert "Attempt 1" in content
    assert "Attempt 2" in content
    assert "a1b2c3d4" in content
    assert "gave up" in content
    assert "FAILED" in content  # build_ok=False should render as FAILED
    assert "$0.0312" in content  # cost from stub


def test_write_patch_creates_directories(tmp_path):
    deep_repo = tmp_path / "deep" / "repo"
    # deep_repo does NOT exist yet
    assert not deep_repo.exists()

    diff = "diff --git a/y b/y\n"
    result = write_patch(deep_repo, "deadbeef", diff)

    expected = deep_repo / ".debugbridge" / "patches" / "crash-deadbeef.patch"
    assert result == expected
    assert expected.exists()
    assert expected.read_text(encoding="utf-8") == diff
