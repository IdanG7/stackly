"""Run user-provided build/test commands inside a worktree (task 2a.3.2)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def run_command(cmd: str, cwd: Path, timeout: int = 600) -> tuple[bool, str]:
    """Run *cmd* via shell, return ``(success, combined_stdout_stderr)``.

    Parameters
    ----------
    cmd:
        Arbitrary shell command string (may include pipes, ``&&``, etc.).
    cwd:
        Working directory — typically a git worktree root.
    timeout:
        Maximum seconds before the process is killed.

    Returns
    -------
    tuple[bool, str]
        ``(True, output)`` when *returncode == 0*; ``(False, output)``
        otherwise.  On timeout the output ends with a
        ``<command timed out after Ns>`` marker.
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        output = (result.stdout or "") + (result.stderr or "")
        return (result.returncode == 0, output)
    except subprocess.TimeoutExpired as exc:
        partial: str
        if isinstance(exc.stdout, bytes):
            partial = exc.stdout.decode("utf-8", errors="replace")
        else:
            partial = exc.stdout or ""
        return (False, partial + f"\n<command timed out after {timeout}s>")
