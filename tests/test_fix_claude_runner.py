"""Tests for fix/claude_runner.py (tasks 2a.1.5 + 2a.3.4).

Writer helpers (write_mcp_config, write_system_append) tested first.
Headless subprocess parser + result builder tests added in 2a.3.4.
"""

from __future__ import annotations

import json
from pathlib import Path

from debugbridge.fix.claude_runner import (
    _build_claude_run_result,
    _parse_claude_json,
    write_mcp_config,
    write_system_append,
)


def test_write_mcp_config_schema(tmp_path: Path) -> None:
    """mcp-config.json must match the exact schema Claude Code expects for HTTP MCP servers."""
    target = tmp_path / "scratch"
    result_path = write_mcp_config(target, host="127.0.0.1", port=8585)

    # Return value points at the file actually written.
    assert result_path == target / "mcp-config.json"
    assert result_path.exists()

    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert data == {
        "mcpServers": {
            "debugbridge": {
                "type": "http",
                "url": "http://127.0.0.1:8585/mcp",
            }
        }
    }


def test_write_mcp_config_uses_non_default_host_port(tmp_path: Path) -> None:
    """Verifies URL construction honours non-default host:port."""
    result_path = write_mcp_config(tmp_path, host="10.0.0.5", port=9999)
    data = json.loads(result_path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["debugbridge"]["url"] == "http://10.0.0.5:9999/mcp"


def test_write_mcp_config_creates_parent_directories(tmp_path: Path) -> None:
    """When target_dir doesn't exist, write_mcp_config should create it."""
    deep = tmp_path / "a" / "b" / "c"
    assert not deep.exists()
    result_path = write_mcp_config(deep, host="127.0.0.1", port=8585)
    assert result_path.exists()
    assert result_path.parent == deep


def test_write_system_append_contains_crash_fix_agent_phrase(tmp_path: Path) -> None:
    """system-append.md must identify the agent role so Claude stays on task."""
    result_path = write_system_append(tmp_path)
    assert result_path == tmp_path / "system-append.md"
    assert result_path.exists()

    content = result_path.read_text(encoding="utf-8")
    # PLAN.md 2a.1.5 acceptance criterion: must contain the phrase "crash-fix agent".
    assert "crash-fix agent" in content
    # UTF-8 + trailing newline per the acceptance criterion.
    assert content.endswith("\n")


def test_write_system_append_creates_parent_directories(tmp_path: Path) -> None:
    """When target_dir doesn't exist, write_system_append should create it."""
    deep = tmp_path / "x" / "y"
    write_system_append(deep)
    assert (deep / "system-append.md").exists()


# ---------------------------------------------------------------------------
# Task 2a.3.4 — _parse_claude_json + _build_claude_run_result
# ---------------------------------------------------------------------------


def test_parse_claude_json_handles_noise_prefix() -> None:
    """Parser must extract the JSON object even when preceded by warning lines."""
    stdout = (
        "WARN: auth cache refreshed\n"
        '{"type":"result","subtype":"success","is_error":false,"result":"ok",'
        '"total_cost_usd":0.05,"usage":{"input_tokens":1,"output_tokens":2,'
        '"cache_read_input_tokens":10},"num_turns":1,"duration_ms":100,'
        '"session_id":"abc"}\n'
    )
    parsed = _parse_claude_json(stdout)
    assert parsed is not None
    assert parsed["total_cost_usd"] == 0.05


def test_parse_claude_json_returns_none_for_empty() -> None:
    """Empty stdout must yield None, not raise."""
    assert _parse_claude_json("") is None


def test_parse_claude_json_returns_none_for_garbage() -> None:
    """Non-JSON stdout must yield None."""
    assert _parse_claude_json("not json at all") is None


def test_build_claude_run_result_from_parsed_json() -> None:
    """Well-formed parsed dict must map 1:1 to ClaudeRunResult fields."""
    parsed = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "total_cost_usd": 0.05,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 10,
        },
        "num_turns": 1,
        "duration_ms": 100,
        "session_id": "abc",
    }
    result = _build_claude_run_result(parsed, returncode=0, raw_stdout="...", raw_stderr="")
    assert result.ok is True
    assert result.is_error is False
    assert result.subtype == "success"
    assert result.total_cost_usd == 0.05
    assert result.input_tokens == 11  # 1 + 10 cache_read
    assert result.output_tokens == 2
    assert result.num_turns == 1
    assert result.session_id == "abc"
