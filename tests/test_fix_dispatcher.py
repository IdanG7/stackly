"""Tests for fix/dispatcher.py (tasks 2a.2.2, 2a.3.5).

Hand-off dispatcher: capture crash via MCP, write briefing, launch interactive Claude.
Autonomous dispatcher: capture → briefing → worktree → claude headless → build → patch/fail.
All external calls (capture_crash, subprocess.run) are monkeypatched — no real server
or claude binary needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from debugbridge.fix.models import ClaudeRunResult, CrashCapture
from debugbridge.models import CallFrame, ExceptionInfo


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Initialize a bare-minimum git repo so ensure_gitignore / is_git_repo work."""
    subprocess.run(
        ["git", "init"],
        cwd=str(tmp_path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(tmp_path),
        capture_output=True,
        check=True,
    )
    return tmp_path


def _canned_capture() -> CrashCapture:
    """Return a minimal CrashCapture matching the pattern from test_fix_models.py."""
    return CrashCapture(
        pid=42,
        process_name="crash_app.exe",
        binary_path="D:/x/crash_app.exe",
        exception=ExceptionInfo(
            code=0xC0000005,
            code_name="EXCEPTION_ACCESS_VIOLATION",
            address=0x7FF612341234,
            description="Access violation",
            is_first_chance=True,
            faulting_thread_tid=5678,
        ),
        callstack=[
            CallFrame(
                index=0,
                function="crash_null",
                module="crash_app",
                instruction_pointer=0xDEAD,
            ),
        ],
        threads=[],
        locals_=[],
        crash_hash="a1b2c3d4",
    )


def test_handoff_writes_briefing_and_invokes_claude_with_correct_args(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_handoff must:
    1. Write briefing to .debugbridge/briefings/crash-<hash>.md
    2. Write mcp-config to .debugbridge/mcp-config.json
    3. Invoke claude with --mcp-config, --strict-mcp-config, and a positional
       message referencing the briefing via @path.
    4. Return FixResult(ok=True, mode="handoff", crash_hash=...).
    """
    canned = _canned_capture()

    # Monkeypatch capture_crash to return canned data without MCP.
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.capture_crash",
        lambda pid, mcp_url, conn_str=None: canned,
    )

    # Monkeypatch ensure_server_running to no-op (returns None = server was already up).
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: None,
    )

    # Record subprocess.run calls from run_claude_interactive.
    recorded_calls: list[dict] = []
    original_run = subprocess.run

    def fake_subprocess_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        # Only intercept "claude" calls — let git commands through.
        if cmd and cmd[0] == "claude":
            recorded_calls.append({"args": cmd, "kwargs": kwargs})
            return subprocess.CompletedProcess(args=cmd, returncode=0)
        return original_run(*args, **kwargs)

    monkeypatch.setattr("debugbridge.fix.claude_runner.subprocess.run", fake_subprocess_run)

    from debugbridge.fix.dispatcher import run_handoff

    result = run_handoff(
        repo=git_repo,
        pid=42,
        host="127.0.0.1",
        port=8585,
    )

    # 1. Briefing file exists at the expected path.
    briefing_path = git_repo / ".debugbridge" / "briefings" / f"crash-{canned.crash_hash}.md"
    assert briefing_path.exists(), f"Briefing not found at {briefing_path}"
    briefing_content = briefing_path.read_text(encoding="utf-8")
    assert "crash-a1b2c3d4" in briefing_content

    # 2. MCP config exists.
    mcp_config_path = git_repo / ".debugbridge" / "mcp-config.json"
    assert mcp_config_path.exists(), f"MCP config not found at {mcp_config_path}"

    # 3. Claude was invoked with the right args.
    assert len(recorded_calls) == 1, f"Expected 1 claude call, got {len(recorded_calls)}"
    argv = recorded_calls[0]["args"]
    assert argv[0] == "claude", f"First arg must be 'claude', got {argv[0]}"
    assert "--mcp-config" in argv, f"--mcp-config not in argv: {argv}"
    assert "--strict-mcp-config" in argv, f"--strict-mcp-config not in argv: {argv}"
    # The positional message must reference the briefing via @path.
    positional_msg = argv[-1]
    assert "@.debugbridge/briefings/crash-" in positional_msg, (
        f"Positional message must contain @.debugbridge/briefings/crash-: {positional_msg}"
    )

    # 4. FixResult shape.
    assert result.ok is True
    assert result.mode == "handoff"
    assert result.crash_hash == canned.crash_hash


# ---------------------------------------------------------------------------
# Autonomous loop tests (task 2a.3.5)
# ---------------------------------------------------------------------------


def _canned_claude_result(ok: bool = True) -> ClaudeRunResult:
    """Return a canned ClaudeRunResult for monkeypatching."""
    return ClaudeRunResult(
        ok=ok,
        is_error=False,
        subtype="success",
        result="fixed null check",
        total_cost_usd=0.12,
        input_tokens=15000,
        output_tokens=2000,
        num_turns=5,
        duration_ms=30000,
        returncode=0,
        session_id="s1",
    )


def _patch_autonomous_deps(monkeypatch: pytest.MonkeyPatch, build_ok: bool = True) -> None:
    """Monkeypatch all external dependencies for run_autonomous."""
    canned = _canned_capture()

    # capture_crash — return canned capture without MCP
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.capture_crash",
        lambda pid, mcp_url, conn_str=None: canned,
    )

    # ensure_server_running — no-op, return None (server already up)
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.ensure_server_running",
        lambda host="127.0.0.1", port=8585, startup_timeout_s=30.0: None,
    )

    # shutdown_server — no-op
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.shutdown_server",
        lambda proc, grace_s=5.0: None,
    )

    # run_claude_headless — return canned success result
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.run_claude_headless",
        lambda **kwargs: _canned_claude_result(ok=True),
    )

    # run_command — return build_ok
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.run_command",
        lambda cmd, cwd, timeout=600: (build_ok, "build ok" if build_ok else "build error"),
    )

    # capture_diff — return a fake diff
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.capture_diff",
        lambda worktree: (
            "diff --git a/foo.c b/foo.c\n--- a/foo.c\n+++ b/foo.c\n@@ -1 +1 @@\n-bad\n+good\n"
        ),
    )

    # create_worktree — create a real subdirectory (skip git worktree add)
    def fake_create_worktree(repo: Path, crash_hash: str) -> Path:
        wt = repo / ".debugbridge" / f"wt-{crash_hash}"
        wt.mkdir(parents=True, exist_ok=True)
        # Make it look like a git repo for build_runner etc.
        (wt / ".git").write_text("gitdir: fake", encoding="utf-8")
        return wt

    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.create_worktree",
        fake_create_worktree,
    )

    # cleanup_worktree_on_success — just remove the directory
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.cleanup_worktree_on_success",
        lambda repo, worktree, crash_hash: None,
    )

    # cleanup_worktree_on_failure — no-op (preserve worktree)
    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.cleanup_worktree_on_failure",
        lambda repo, worktree, crash_hash: None,
    )


def test_auto_loop_single_attempt_success(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_autonomous: single attempt succeeds → ok=True, 1 attempt, patch written."""
    _patch_autonomous_deps(monkeypatch, build_ok=True)

    from debugbridge.fix.dispatcher import run_autonomous

    result = run_autonomous(
        repo=git_repo,
        pid=42,
        host="127.0.0.1",
        port=8585,
        build_cmd="make",
        max_attempts=3,
    )

    assert result.ok is True
    assert result.mode == "auto"
    assert len(result.attempts) == 1
    assert result.attempts[0].build_ok is True
    assert result.crash_hash == "a1b2c3d4"

    # Patch file must exist under .debugbridge/patches/
    patch_path = git_repo / ".debugbridge" / "patches" / "crash-a1b2c3d4.patch"
    assert patch_path.exists(), f"Patch file not found at {patch_path}"
    assert "diff --git" in patch_path.read_text(encoding="utf-8")

    # result.patch_path should point to the patch
    assert result.patch_path is not None
    assert result.patch_path.exists()


def test_auto_loop_build_failure_exhausts_attempts(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_autonomous: build always fails, max_attempts=1 → ok=False, failure report."""
    _patch_autonomous_deps(monkeypatch, build_ok=False)

    from debugbridge.fix.dispatcher import run_autonomous

    result = run_autonomous(
        repo=git_repo,
        pid=42,
        host="127.0.0.1",
        port=8585,
        build_cmd="make",
        max_attempts=1,
    )

    assert result.ok is False
    assert result.mode == "auto"
    assert len(result.attempts) == 1
    assert result.attempts[0].build_ok is False
    assert result.crash_hash == "a1b2c3d4"

    # Failure report must exist
    fail_path = git_repo / ".debugbridge" / "patches" / "crash-a1b2c3d4.failed.md"
    assert fail_path.exists(), f"Failure report not found at {fail_path}"
    assert result.failure_report_path is not None


def test_auto_loop_does_not_touch_main_tree(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_autonomous must never modify files outside .debugbridge/ in the repo."""
    _patch_autonomous_deps(monkeypatch, build_ok=True)

    # Create a sentinel file in the repo root
    sentinel = git_repo / "SENTINEL.txt"
    sentinel.write_text("DO NOT TOUCH", encoding="utf-8")

    # Record initial state of the repo root (excluding .debugbridge)
    initial_files = {
        f.name for f in git_repo.iterdir() if f.name not in (".git", ".debugbridge", ".gitignore")
    }

    from debugbridge.fix.dispatcher import run_autonomous

    result = run_autonomous(
        repo=git_repo,
        pid=42,
        host="127.0.0.1",
        port=8585,
        build_cmd="make",
        max_attempts=3,
    )

    assert result.ok is True

    # Sentinel file must be unchanged
    assert sentinel.exists(), "SENTINEL.txt was deleted"
    assert sentinel.read_text(encoding="utf-8") == "DO NOT TOUCH", "SENTINEL.txt was modified"

    # No new files in repo root outside .debugbridge/ and .gitignore
    final_files = {
        f.name for f in git_repo.iterdir() if f.name not in (".git", ".debugbridge", ".gitignore")
    }
    assert final_files == initial_files, (
        f"New files appeared in repo root: {final_files - initial_files}"
    )


# ---------------------------------------------------------------------------
# Retry-feedback loop tests (task 2a.3.6)
# ---------------------------------------------------------------------------


def test_auto_loop_retries_on_build_failure_with_appended_output(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_autonomous with build fail on attempt 1, pass on attempt 2.

    Second claude call's briefing must contain the first build's error output
    under a "Previous attempt" section.
    """
    _patch_autonomous_deps(monkeypatch, build_ok=True)  # base patches

    # run_command: fail first call, pass second
    build_call_count = 0

    def fake_run_command(cmd, cwd, timeout=600):
        nonlocal build_call_count
        build_call_count += 1
        if build_call_count == 1:
            return (False, "ld: undefined symbol x")
        return (True, "build ok")

    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.run_command",
        fake_run_command,
    )

    # run_claude_headless: record the briefing content each time
    briefing_snapshots: list[str] = []

    def fake_claude_headless(**kwargs):
        briefing_path = kwargs.get("briefing_path")
        if briefing_path and Path(briefing_path).exists():
            briefing_snapshots.append(Path(briefing_path).read_text(encoding="utf-8"))
        return _canned_claude_result(ok=True)

    monkeypatch.setattr(
        "debugbridge.fix.dispatcher.run_claude_headless",
        fake_claude_headless,
    )

    from debugbridge.fix.dispatcher import run_autonomous

    result = run_autonomous(
        repo=git_repo,
        pid=42,
        host="127.0.0.1",
        port=8585,
        build_cmd="make",
        max_attempts=3,
    )

    assert result.ok is True
    assert len(result.attempts) == 2

    # First briefing should NOT contain "Previous attempt"
    assert "Previous attempt" not in briefing_snapshots[0]

    # Second briefing should contain the build error from attempt 1
    assert "Previous attempt" in briefing_snapshots[1]
    assert "ld: undefined symbol x" in briefing_snapshots[1]
