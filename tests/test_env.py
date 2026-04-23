"""Tests for env.py — Debugging Tools detection. No pybag import."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from stackly.env import (
    CANONICAL_DEBUGGERS_X64,
    check_claude_bypass_acknowledged,
    check_claude_cli,
    check_debugging_tools,
    ensure_dbgeng_on_path,
)


def test_check_returns_structured_result() -> None:
    result = check_debugging_tools()
    # Shape only — don't assert ok=True because CI will run without tools.
    assert isinstance(result.found, dict)
    assert isinstance(result.missing, list)
    assert result.ok == (not result.missing)


def test_check_all_missing_produces_guidance() -> None:
    fake_nonexistent = Path(r"C:\definitely\not\here")
    with (
        patch("stackly.env.shutil.which", return_value=None),
        patch("stackly.env.CANONICAL_DEBUGGERS_X64", fake_nonexistent),
    ):
        result = check_debugging_tools()
    assert not result.ok
    assert "dbgsrv.exe" in result.missing
    assert result.guidance is not None
    assert "Windows SDK" in result.guidance


def test_check_claude_cli_reports_missing() -> None:
    """When `claude` is not on PATH, the check should fail with install guidance."""
    with patch("stackly.env.shutil.which", return_value=None):
        result = check_claude_cli()
    assert result.ok is False
    assert result.found == {}
    assert result.missing == ["claude"]
    assert result.guidance is not None
    # PLAN.md Task 2a.0.2 specifies install URL must point at docs.claude.com.
    assert "claude.com" in result.guidance


def test_check_claude_cli_reports_found() -> None:
    """When `claude` is on PATH, the check should pass with the resolved path."""
    fake_path = "C:/fake/path/claude.exe"
    with patch("stackly.env.shutil.which", return_value=fake_path):
        result = check_claude_cli()
    assert result.ok is True
    assert result.found == {"claude": fake_path}
    assert result.missing == []
    assert result.guidance is None


def test_check_claude_bypass_acknowledged_reads_settings_json(tmp_path: Path) -> None:
    """Covers three cases: no file, bypass key present, bypass key absent."""
    # Case A: settings.json does not exist.
    missing_path = tmp_path / "absent-settings.json"
    result_a = check_claude_bypass_acknowledged(settings_path=missing_path)
    assert result_a.ok is False
    assert result_a.guidance is not None
    assert "claude --dangerously-skip-permissions" in result_a.guidance

    # Case B: settings.json exists with the bypass key enabled.
    ack_path = tmp_path / "acknowledged.json"
    ack_path.write_text(
        json.dumps({"skipDangerousModePermissionPrompt": True}),
        encoding="utf-8",
    )
    result_b = check_claude_bypass_acknowledged(settings_path=ack_path)
    assert result_b.ok is True
    assert result_b.guidance is None
    assert result_b.found == {"claude-bypass-ack": str(ack_path)}

    # Case C: settings.json exists but the bypass key is absent.
    other_path = tmp_path / "other-settings.json"
    other_path.write_text(json.dumps({"someOtherKey": "value"}), encoding="utf-8")
    result_c = check_claude_bypass_acknowledged(settings_path=other_path)
    assert result_c.ok is False
    assert result_c.guidance is not None


def test_ensure_dbgeng_on_path_idempotent() -> None:
    """Calling ensure_dbgeng_on_path twice should not duplicate the entry."""
    if not CANONICAL_DEBUGGERS_X64.exists():
        # On a machine without Debugging Tools, ensure_dbgeng_on_path is a no-op.
        ensure_dbgeng_on_path()
        return
    original_path = os.environ.get("PATH", "")
    try:
        ensure_dbgeng_on_path()
        first_count = os.environ["PATH"].count(str(CANONICAL_DEBUGGERS_X64))
        ensure_dbgeng_on_path()
        second_count = os.environ["PATH"].count(str(CANONICAL_DEBUGGERS_X64))
        assert first_count == second_count
        assert first_count >= 1
    finally:
        os.environ["PATH"] = original_path
