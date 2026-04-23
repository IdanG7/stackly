"""E2E integration test for stackly fix (task 2a.4.2).

Marked @integration and @slow -- skipped in CI, opt-in locally.
The 'fake build' variant uses monkeypatched claude to avoid API costs.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from stackly.cli import app

runner = CliRunner()


def test_fix_rejects_non_git_repo(tmp_path):
    """fix --pid 0 --repo <non-git-dir> should fail with clear error."""
    result = runner.invoke(app, ["fix", "--pid", "0", "--repo", str(tmp_path)])
    assert result.exit_code != 0
    assert "not a git repository" in result.output.lower()


@pytest.mark.integration
@pytest.mark.slow
def test_fix_auto_rejects_missing_claude(tmp_path, monkeypatch):
    """fix --auto with no claude on PATH should fail with clear error.

    This test creates a git repo but removes claude from PATH.
    """
    import subprocess

    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path),
        capture_output=True,
        check=True,
    )

    # Ensure claude is NOT on PATH for this test
    monkeypatch.setattr("shutil.which", lambda name: None)

    result = runner.invoke(app, ["fix", "--pid", "0", "--repo", str(tmp_path), "--auto"])
    assert result.exit_code != 0
    assert "claude" in result.output.lower()
