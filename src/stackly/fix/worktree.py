"""Git worktree management for the fix subpackage.

Phase 2a scope:
- is_git_repo, detect_dirty, ensure_gitignore (task 2a.2.1)
- compute_crash_hash, create_worktree, capture_diff,
  cleanup_worktree_on_success, cleanup_worktree_on_failure (task 2a.3.1)
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from stackly.fix.models import CrashCapture

__all__ = [
    "capture_diff",
    "cleanup_worktree_on_failure",
    "cleanup_worktree_on_success",
    "compute_crash_hash",
    "create_worktree",
    "detect_dirty",
    "ensure_gitignore",
    "is_git_repo",
]

_GITIGNORE_ENTRY = "/.stackly/"


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a git command with UTF-8 text capture. Never raises on non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def is_git_repo(path: Path) -> bool:
    """Return True iff ``path`` is inside a git working tree.

    Uses ``git rev-parse --is-inside-work-tree``; returns False on any error
    (non-zero exit, git not on PATH, path doesn't exist).
    """
    if not path.exists():
        return False
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=path)
    return result.returncode == 0 and result.stdout.strip() == "true"


def detect_dirty(path: Path) -> bool:
    """Return True iff the working tree has uncommitted changes.

    Uses ``git status --porcelain``: any non-empty output = dirty. Returns False
    (pessimistically "clean") on git error — we don't want to block the fix
    agent over a false positive if git is mis-configured.
    """
    result = _run_git(["status", "--porcelain"], cwd=path)
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def ensure_gitignore(repo: Path) -> None:
    """Idempotently add ``/.stackly/`` to ``repo / '.gitignore'``.

    Creates the file if missing. Does nothing if the entry is already present
    (matching either ``/.stackly/`` or ``.stackly/`` so we don't
    duplicate a pre-existing user entry in a different form).
    """
    gitignore = repo / ".gitignore"

    existing = ""
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8")

    # Check for either form of the entry as a whole line.
    lines = existing.splitlines()
    already = any(
        line.strip() in ("/.stackly/", ".stackly/", "/.stackly", ".stackly")
        for line in lines
    )
    if already:
        return

    # Append; add trailing newline only if the existing file didn't end with one.
    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    gitignore.write_text(prefix + _GITIGNORE_ENTRY + "\n", encoding="utf-8")


def compute_crash_hash(capture: CrashCapture) -> str:
    """Return an 8-char hex hash stable across re-runs of the same crash.

    Formula: sha1(f"{exception_code_name}@{top_module}!{top_function}")[:8].
    Each side defaults to "unknown" when data is missing, so degenerate
    captures still produce a stable (but low-information) hash.
    """
    code_name = capture.exception.code_name if capture.exception is not None else "unknown"

    module = "unknown"
    function = "unknown"
    if capture.callstack:
        top = capture.callstack[0]
        module = top.module or "unknown"
        function = top.function or "unknown"

    payload = f"{code_name}@{module}!{function}".encode()
    return hashlib.sha1(payload).hexdigest()[:8]


def _worktree_path(repo: Path, crash_hash: str) -> Path:
    return repo / ".stackly" / f"wt-{crash_hash}"


def _branch_name(crash_hash: str) -> str:
    return f"stackly/fix-{crash_hash}"


def create_worktree(repo: Path, crash_hash: str) -> Path:
    """Create ``.stackly/wt-<hash>/`` as a git worktree on a fresh branch.

    If a worktree or branch with the same hash already exists (half-cleaned
    from a prior run), they are removed first — same hash always yields a
    fresh worktree.
    """
    wt = _worktree_path(repo, crash_hash)
    branch = _branch_name(crash_hash)

    # Pre-clean any stale worktree registration + directory.
    _run_git(["worktree", "remove", "--force", str(wt)], cwd=repo)
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    # Pre-clean the branch if it exists.
    _run_git(["branch", "-D", branch], cwd=repo)

    wt.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(["worktree", "add", "-b", branch, str(wt), "HEAD"], cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr.strip()}")
    return wt


def capture_diff(worktree: Path) -> str:
    """Return a unified diff of all changes in the worktree vs HEAD.

    Includes unstaged and staged changes (``git diff HEAD``). Untracked files
    are NOT included — add them first if the agent creates new files.
    """
    result = _run_git(["diff", "HEAD"], cwd=worktree)
    if result.returncode != 0:
        return ""
    return result.stdout


def cleanup_worktree_on_success(repo: Path, worktree: Path, crash_hash: str) -> None:
    """Remove the worktree directory and delete its branch.

    Both operations tolerate failure — on Windows, worktree directory handles
    held by any editor / shell cd'd into the worktree can prevent removal.
    In that case, the worktree is left on disk and the user can clean up via
    ``git worktree prune`` later.
    """
    branch = _branch_name(crash_hash)
    _run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo)
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    _run_git(["branch", "-D", branch], cwd=repo)


def cleanup_worktree_on_failure(repo: Path, worktree: Path, crash_hash: str) -> None:
    """No-op: we preserve the worktree on failure for human inspection.

    ``repo`` and ``crash_hash`` are accepted for API symmetry with the success
    path so the dispatcher can call the same way regardless of outcome.
    """
    # Intentionally empty. The worktree stays on disk; the user can examine
    # `git -C <worktree> status` or `git -C <worktree> diff` to inspect what
    # the fix agent attempted.
    _ = (repo, worktree, crash_hash)
