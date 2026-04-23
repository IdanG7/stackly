"""Pydantic types for the fix-loop subpackage.

These models are the wire contract between the CLI (`stackly fix`), the
crash-capture step (via MCP), the claude subprocess wrapper, and the on-disk
artifacts written under ``.stackly/``. Do not add fields to
:class:`AttemptRecord` in later tasks — the schema is final here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# Re-export Phase 1 models so fix/ has a single import site.
from stackly.models import CallFrame, ExceptionInfo, Local, ThreadInfo

__all__ = [
    "AttemptRecord",
    "CallFrame",
    "ClaudeRunResult",
    "CrashCapture",
    "ExceptionInfo",
    "FixResult",
    "Local",
    "ThreadInfo",
]


class CrashCapture(BaseModel):
    """Snapshot of debugger state taken once, just after attach.

    All MCP round-trips happen during :func:`stackly.fix.mcp_client.capture_crash`
    (task 2a.1.2); after that the briefing generator and dispatcher work off this
    single immutable object.
    """

    pid: int
    process_name: str | None = None
    binary_path: str | None = None
    exception: ExceptionInfo | None = None
    callstack: list[CallFrame] = Field(default_factory=list)
    threads: list[ThreadInfo] = Field(default_factory=list)
    locals_: list[Local] = Field(
        default_factory=list,
        description="Trailing underscore avoids the Python `locals` builtin.",
    )
    crash_hash: str = Field(description='8-char hex; "unknown" when capture is degenerate.')


class ClaudeRunResult(BaseModel):
    """Parsed output of a ``claude -p ... --output-format json`` invocation.

    Fields mirror the claude headless JSON schema (RESEARCH.md §6). We also
    capture raw streams for failure diagnostics.
    """

    ok: bool = Field(description="True iff is_error is False AND returncode == 0.")
    is_error: bool = False
    subtype: str = Field(
        description='Success="success"; failures include "empty_output", "unparseable_output", "budget", etc.'
    )
    result: str = Field(default="", description="Final assistant message content.")
    session_id: str = ""
    total_cost_usd: float = 0.0
    input_tokens: int = Field(
        default=0, description="sum of usage.input_tokens + usage.cache_read_input_tokens"
    )
    output_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0
    raw_stdout: str = ""
    raw_stderr: str = ""
    returncode: int = 0


class AttemptRecord(BaseModel):
    """One pass through the autonomous fix loop.

    .. note::

       Schema is **final** at task 2a.0.3. Task 2a.3.7 (test-cmd support)
       uses ``test_ok`` / ``test_output`` but does not amend the model —
       see PLAN.md plan-check fix C2.
    """

    attempt: int = Field(description="1-indexed.")
    claude_result: ClaudeRunResult
    build_ok: bool
    build_output: str
    test_ok: bool | None = Field(
        default=None,
        description="None = --test-cmd not provided; True/False = tests ran with that outcome.",
    )
    test_output: str | None = None
    duration_s: float


class FixResult(BaseModel):
    """Final outcome of a ``stackly fix`` invocation."""

    ok: bool
    mode: Literal["handoff", "auto"]
    crash_hash: str
    patch_path: Path | None = None
    failure_report_path: Path | None = None
    attempts: list[AttemptRecord] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    worktree_path: Path | None = None
    worktree_preserved: bool = Field(
        default=False,
        description="True when the worktree is intentionally kept on disk (failure, or user asked).",
    )
