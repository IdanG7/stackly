"""Tests for build/test runner (task 2a.3.2)."""

from __future__ import annotations

from debugbridge.fix.build_runner import run_command


def test_run_command_success(tmp_path):
    ok, output = run_command(
        "python -c \"print('ok'); import sys; sys.exit(0)\"",
        cwd=tmp_path,
        timeout=5,
    )
    assert ok is True
    assert "ok" in output


def test_run_command_failure(tmp_path):
    ok, _output = run_command(
        'python -c "import sys; sys.exit(7)"',
        cwd=tmp_path,
        timeout=5,
    )
    assert ok is False


def test_run_command_timeout(tmp_path):
    ok, output = run_command(
        'python -c "import time; time.sleep(10)"',
        cwd=tmp_path,
        timeout=1,
    )
    assert ok is False
    assert "timed out" in output.lower()
