"""Tests for the `stackly fix` CLI command (task 2a.4.1).

Uses typer.testing.CliRunner to exercise the fix command without launching
a real process or claude session.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from stackly.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes so Rich-formatted output is searchable."""
    return _ANSI_RE.sub("", text)


def test_fix_help_shows_all_flags() -> None:
    """fix --help must exit 0 and show all documented flags."""
    result = runner.invoke(app, ["fix", "--help"])
    assert result.exit_code == 0, f"exit_code={result.exit_code}\n{result.output}"

    plain = _strip_ansi(result.output)
    expected_flags = [
        "--pid",
        "--repo",
        "--conn-str",
        "--build-cmd",
        "--test-cmd",
        "--auto",
        "--host",
        "--port",
        "--model",
        "--max-attempts",
    ]
    for flag in expected_flags:
        assert flag in plain, f"Missing flag {flag!r} in help output:\n{plain}"


def test_fix_rejects_non_git_repo(tmp_path) -> None:
    """fix --pid 0 --repo <non-git-dir> should fail with clear error."""
    result = runner.invoke(app, ["fix", "--pid", "0", "--repo", str(tmp_path)])
    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}"
    assert "not a git repository" in result.output.lower(), (
        f"Expected 'not a git repository' in output:\n{result.output}"
    )
