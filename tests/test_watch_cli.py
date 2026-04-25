"""Tests for the `stackly watch` CLI command (task 2.5.2.2).

Uses typer.testing.CliRunner to exercise the watch command without launching
a real process or claude session.
"""

from __future__ import annotations

import re
import subprocess

from typer.testing import CliRunner

from stackly.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes so Rich-formatted output is searchable."""
    return _ANSI_RE.sub("", text)


def test_watch_help_shows_all_flags() -> None:
    """watch --help must exit 0 and show all 14 documented flags."""
    result = runner.invoke(app, ["watch", "--help"])
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
        "--max-crashes",
        "--max-wait-minutes",
        "--poll-seconds",
        "--quiet",
    ]
    for flag in expected_flags:
        assert flag in plain, f"Missing flag {flag!r} in help output:\n{plain}"


def test_watch_rejects_nonexistent_repo() -> None:
    """watch --pid 0 --repo <nonexistent> should fail with clear error."""
    result = runner.invoke(app, ["watch", "--pid", "0", "--repo", "C:/nonexistent/path/that/does/not/exist"])
    assert result.exit_code == 1, f"Expected exit_code=1, got {result.exit_code}\n{result.output}"
    assert "not a git repository" in result.output.lower(), (
        f"Expected 'not a git repository' in output:\n{result.output}"
    )


def test_watch_auto_without_claude_fails(tmp_path_factory, monkeypatch) -> None:
    """watch --auto should fail with clear error when claude CLI is not on PATH."""
    # Create a temporary git repo
    tmp_repo = tmp_path_factory.mktemp("repo")
    subprocess.run(["git", "init", str(tmp_repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_repo), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            **__import__("os").environ,
        },
    )

    # Monkeypatch shutil.which so that which("claude") returns None
    import shutil as _shutil

    original_which = _shutil.which

    def patched_which(name: str, *args, **kwargs):  # type: ignore[override]
        if name == "claude":
            return None
        return original_which(name, *args, **kwargs)

    monkeypatch.setattr(_shutil, "which", patched_which)

    result = runner.invoke(app, ["watch", "--pid", "0", "--repo", str(tmp_repo), "--auto"])
    assert result.exit_code == 1, f"Expected exit_code=1, got {result.exit_code}\n{result.output}"
    assert "claude cli not found" in result.output.lower(), (
        f"Expected 'claude cli not found' in output:\n{result.output}"
    )
