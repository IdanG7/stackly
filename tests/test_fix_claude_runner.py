"""Tests for fix/claude_runner.py writer helpers (task 2a.1.5).

Subprocess-wrapping tests land in 2a.3.4. This file tests only the two
helpers that write claude-config artifacts to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from debugbridge.fix.claude_runner import write_mcp_config, write_system_append


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
