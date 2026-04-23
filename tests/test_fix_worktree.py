"""Tests for fix/worktree.py — detection half (2a.2.1) + lifecycle (2a.3.1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from stackly.fix.models import CrashCapture
from stackly.fix.worktree import (
    capture_diff,
    cleanup_worktree_on_failure,
    cleanup_worktree_on_success,
    compute_crash_hash,
    create_worktree,
    detect_dirty,
    ensure_gitignore,
    is_git_repo,
)
from stackly.models import CallFrame, ExceptionInfo


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with a single commit so HEAD exists."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_is_git_repo_false_for_plain_dir(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path) is False


def test_is_git_repo_true_after_init(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    assert is_git_repo(tmp_path) is True


def test_detect_dirty_reports_clean_then_dirty(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    assert detect_dirty(tmp_path) is False

    # Modify a tracked file → dirty.
    (tmp_path / "README.md").write_text("# changed\n", encoding="utf-8")
    assert detect_dirty(tmp_path) is True


def test_ensure_gitignore_appends_entry_when_missing(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n", encoding="utf-8")

    ensure_gitignore(tmp_path)
    content = gitignore.read_text(encoding="utf-8")

    # Pre-existing content preserved.
    assert "node_modules/" in content
    # Debugbridge entry added (accept either "/.stackly/" or ".stackly/").
    assert "/.stackly/" in content or ".stackly/" in content


def test_ensure_gitignore_is_idempotent(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n", encoding="utf-8")

    ensure_gitignore(tmp_path)
    first = gitignore.read_text(encoding="utf-8")
    ensure_gitignore(tmp_path)
    second = gitignore.read_text(encoding="utf-8")
    assert first == second  # Exactly identical; no double-append.


def test_ensure_gitignore_creates_file_when_missing(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    # Ensure no .gitignore exists.
    gitignore = tmp_path / ".gitignore"
    assert not gitignore.exists()

    ensure_gitignore(tmp_path)
    assert gitignore.exists()
    content = gitignore.read_text(encoding="utf-8")
    # Exactly one entry — no stray blank lines or duplicates.
    assert content.count(".stackly") == 1


def test_ensure_gitignore_detects_existing_dotprefixed_entry(tmp_path: Path) -> None:
    """If .gitignore already contains '/.stackly/', don't double-add."""
    _init_git_repo(tmp_path)
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("/.stackly/\n", encoding="utf-8")

    ensure_gitignore(tmp_path)
    content = gitignore.read_text(encoding="utf-8")
    assert content.count(".stackly") == 1


# --- Task 2a.3.1 tests (crash-hash + worktree lifecycle) ---


def _make_capture(
    *,
    code_name: str = "EXCEPTION_ACCESS_VIOLATION",
    module: str = "myapp",
    function: str = "crash_null+0x2a",
) -> CrashCapture:
    return CrashCapture(
        pid=0,
        exception=ExceptionInfo(
            code=0xC0000005,
            code_name=code_name,
            address=0,
            description="",
            is_first_chance=True,
            faulting_thread_tid=None,
        ),
        callstack=[
            CallFrame(
                index=0,
                function=function,
                module=module,
                file=None,
                line=None,
                instruction_pointer=0,
            )
        ],
        threads=[],
        locals_=[],
        crash_hash="",  # unused by compute_crash_hash; computed from exception + frame
    )


def test_compute_crash_hash_is_deterministic_and_8_hex_chars() -> None:
    """Same inputs -> same hash; valid hex."""
    cap = _make_capture()
    h1 = compute_crash_hash(cap)
    h2 = compute_crash_hash(cap)
    assert h1 == h2
    assert len(h1) == 8
    assert all(c in "0123456789abcdef" for c in h1)


def test_compute_crash_hash_differs_by_exception_type() -> None:
    """Different exception -> different hash."""
    a = _make_capture(code_name="EXCEPTION_ACCESS_VIOLATION")
    b = _make_capture(code_name="EXCEPTION_STACK_OVERFLOW")
    assert compute_crash_hash(a) != compute_crash_hash(b)


def test_compute_crash_hash_differs_by_top_frame() -> None:
    a = _make_capture(function="crash_null+0x2a")
    b = _make_capture(function="crash_throw+0x10")
    assert compute_crash_hash(a) != compute_crash_hash(b)


def test_compute_crash_hash_handles_degenerate_capture() -> None:
    """No exception AND no callstack -> stable 'unknown' hash, still 8 chars."""
    cap = CrashCapture(pid=0, crash_hash="")
    h = compute_crash_hash(cap)
    assert len(h) == 8
    # Calling twice with the same degenerate input is stable.
    assert h == compute_crash_hash(CrashCapture(pid=0, crash_hash=""))


def test_compute_crash_hash_degenerate_differs_from_populated() -> None:
    """Ensure the 'unknown' formula doesn't collide with any real input we'd likely see."""
    degen = CrashCapture(pid=0, crash_hash="")
    real = _make_capture()
    assert compute_crash_hash(degen) != compute_crash_hash(real)


def test_create_worktree_creates_branch_and_directory(tmp_path: Path) -> None:
    """create_worktree adds a git worktree under .stackly/wt-<hash>/ on a new branch."""
    _init_git_repo(tmp_path)
    hash_ = "a1b2c3d4"
    wt = create_worktree(tmp_path, hash_)

    # Worktree path location
    assert wt == tmp_path / ".stackly" / f"wt-{hash_}"
    assert wt.exists()
    assert (wt / ".git").exists() or (wt / ".git").is_file()  # Git worktree marker file

    # Branch was created
    branch_check = subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "--list", f"stackly/fix-{hash_}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"stackly/fix-{hash_}" in branch_check.stdout


def test_create_worktree_cleans_up_stale_same_hash(tmp_path: Path) -> None:
    """Re-running create_worktree with the same hash removes the prior worktree first."""
    _init_git_repo(tmp_path)
    hash_ = "deadbeef"
    wt1 = create_worktree(tmp_path, hash_)
    # Leave a marker file in the worktree so we can detect recreation
    (wt1 / "STALE_MARKER").write_text("stale", encoding="utf-8")

    wt2 = create_worktree(tmp_path, hash_)
    assert wt2 == wt1
    assert wt2.exists()
    # Fresh worktree should not have the stale marker
    assert not (wt2 / "STALE_MARKER").exists()


def test_capture_diff_returns_empty_for_unmodified_worktree(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    wt = create_worktree(tmp_path, "cafebabe")
    diff = capture_diff(wt)
    assert diff == ""


def test_capture_diff_returns_unified_diff_for_modified_worktree(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    wt = create_worktree(tmp_path, "12345678")
    # Modify a tracked file
    (wt / "README.md").write_text("# changed in worktree\n", encoding="utf-8")

    diff = capture_diff(wt)
    assert "diff --git" in diff
    assert "README.md" in diff
    assert "# changed in worktree" in diff


def test_cleanup_worktree_on_success_removes_worktree_and_branch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    hash_ = "aabbccdd"
    wt = create_worktree(tmp_path, hash_)
    assert wt.exists()

    cleanup_worktree_on_success(tmp_path, wt, hash_)
    assert not wt.exists()

    # Branch should also be removed
    branches = subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "--list", f"stackly/fix-{hash_}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"stackly/fix-{hash_}" not in branches.stdout


def test_cleanup_worktree_on_failure_preserves_worktree(tmp_path: Path) -> None:
    """cleanup_worktree_on_failure() is a no-op on the filesystem - just logs / returns."""
    _init_git_repo(tmp_path)
    hash_ = "bbccddee"
    wt = create_worktree(tmp_path, hash_)
    cleanup_worktree_on_failure(tmp_path, wt, hash_)
    # Worktree still on disk for inspection
    assert wt.exists()
