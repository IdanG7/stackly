"""Claude Code subprocess wrappers + config-file writers (task 2a.1.5 — writer half).

Two helpers land here now:
- write_mcp_config: emits ``mcp-config.json`` matching Claude Code's
  ``--mcp-config`` schema so the headless / interactive claude run can
  reach our MCP server.
- write_system_append: emits ``system-append.md``, appended to Claude
  Code's system prompt via ``--append-system-prompt``, establishing the
  crash-fix-agent role and guardrails.

The ``run_claude_headless`` / ``run_claude_interactive`` subprocess
wrappers land in tasks 2a.2.2 and 2a.3.4.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from debugbridge.fix.models import ClaudeRunResult

__all__ = [
    "_build_claude_run_result",
    "_parse_claude_json",
    "run_claude_headless",
    "write_mcp_config",
    "write_system_append",
]


_SYSTEM_APPEND_BODY = """\
You are a crash-fix agent operating inside the `debugbridge` toolchain.

Your job:
1. Read the crash briefing referenced in the first user message.
2. Use the `mcp__debugbridge__*` tools if you need live debugger state
   beyond what the briefing contains (stack beyond the initial frames,
   locals for other frames, thread list). Do NOT call
   `mcp__debugbridge__detach_process` or `mcp__debugbridge__continue_execution`
   -- those would release or resume the target process and break the session.
3. Read only files that appear in the briefing's Source context section or
   that you can justify are in the direct call chain to the crash site.
4. Propose the minimal fix that addresses the root cause. Do not refactor
   unrelated code, do not add tests for unrelated behavior, do not touch
   third-party code or the standard library.
5. After applying your fix, run the user-provided build command exactly
   as given in the briefing. Report the result succinctly.

Constraints:
- Never modify files outside the working directory Claude Code has been
  launched into (that directory is already a disposable git worktree).
- Prefer surgical edits over rewrites. If the fix is one line, one line
  is correct.
- Describe the root cause in one or two sentences at the end of your
  reply so the human reviewer can evaluate the diff quickly.
"""


def write_mcp_config(target_dir: Path, host: str, port: int) -> Path:
    """Write ``target_dir / "mcp-config.json"`` describing our MCP server.

    Matches Claude Code's ``--mcp-config`` expected schema (RESEARCH.md 1.2):
    ``{"mcpServers": {"debugbridge": {"type": "http", "url": ...}}}``.

    Creates ``target_dir`` if it doesn't exist. Returns the written file path.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "mcpServers": {
            "debugbridge": {
                "type": "http",
                "url": f"http://{host}:{port}/mcp",
            }
        }
    }
    out = target_dir / "mcp-config.json"
    out.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return out


def write_system_append(target_dir: Path) -> Path:
    """Write ``target_dir / "system-append.md"`` -- the crash-fix-agent role block.

    Appended to Claude Code's system prompt via ``--append-system-prompt``.
    Content is static (no template substitution) because the dynamic parts
    (crash details, build command) live in the briefing file instead.

    Creates ``target_dir`` if it doesn't exist. Returns the written file path.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / "system-append.md"
    out.write_text(_SYSTEM_APPEND_BODY, encoding="utf-8")
    return out


# TODO(task 2a.2.2): add run_claude_interactive(repo, briefing_rel, mcp_config_path) -> int


def _parse_claude_json(stdout: str) -> dict | None:
    """Extract the last JSON object from ``claude -p --output-format json`` output.

    Claude may print warnings, progress text, or other noise before the JSON
    payload.  We scan backwards for the last line that starts with ``{`` and
    attempt to parse it.  Returns ``None`` if no valid JSON is found.
    """
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def _build_claude_run_result(
    parsed: dict | None,
    returncode: int,
    raw_stdout: str,
    raw_stderr: str,
) -> ClaudeRunResult:
    """Build a :class:`ClaudeRunResult` from parsed JSON or from failure state."""
    if parsed is None:
        subtype = "empty_output" if not raw_stdout.strip() else "unparseable_output"
        return ClaudeRunResult(
            ok=False,
            is_error=True,
            subtype=subtype,
            result="",
            session_id="",
            total_cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            num_turns=0,
            duration_ms=0,
            raw_stdout=raw_stdout,
            raw_stderr=raw_stderr,
            returncode=returncode,
        )

    usage = parsed.get("usage", {})
    input_tokens = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)

    is_error = parsed.get("is_error", False)
    return ClaudeRunResult(
        ok=not is_error and returncode == 0,
        is_error=is_error,
        subtype=parsed.get("subtype", "unknown"),
        result=parsed.get("result", ""),
        session_id=parsed.get("session_id", ""),
        total_cost_usd=parsed.get("total_cost_usd", 0.0),
        input_tokens=input_tokens,
        output_tokens=usage.get("output_tokens", 0),
        num_turns=parsed.get("num_turns", 0),
        duration_ms=parsed.get("duration_ms", 0),
        raw_stdout=raw_stdout,
        raw_stderr=raw_stderr,
        returncode=returncode,
    )


def run_claude_headless(
    cwd: Path,
    briefing_path: Path,
    mcp_config_path: Path,
    system_append_path: Path,
    model: str = "sonnet",
    max_turns: int = 20,
    max_budget_usd: float = 0.75,
    build_cmd: str | None = None,
) -> ClaudeRunResult:
    """Run ``claude -p`` headless and parse the JSON output.

    Builds the full CLI argv with ``--output-format json``,
    ``--strict-mcp-config``, ``--permission-mode bypassPermissions``, and
    scoped ``--allowedTools`` (never broad ``Bash(*)``).

    Returns a :class:`ClaudeRunResult` — always, even on timeout.
    """
    allowed_tools = ["Read", "Edit", "Write", "Glob", "Grep", "mcp__debugbridge__*"]
    if build_cmd:
        first_word = build_cmd.split()[0] if build_cmd.split() else "build"
        allowed_tools.append(f"Bash({first_word} *)")

    try:
        briefing_rel = briefing_path.relative_to(cwd).as_posix()
    except ValueError:
        briefing_rel = str(briefing_path)

    cmd = [
        "claude",
        "-p",
        f"Read @{briefing_rel} and produce the minimal fix.",
        "--output-format",
        "json",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--append-system-prompt",
        str(system_append_path),
        "--allowedTools",
        ",".join(allowed_tools),
        "--max-budget-usd",
        str(max_budget_usd),
        "--permission-mode",
        "bypassPermissions",
    ]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            creationflags=creationflags,
        )
        parsed = _parse_claude_json(result.stdout)
        return _build_claude_run_result(parsed, result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired as exc:
        stdout = (
            (exc.stdout or b"").decode("utf-8", errors="replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            (exc.stderr or b"").decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return ClaudeRunResult(
            ok=False,
            is_error=True,
            subtype="timeout",
            result="",
            session_id="",
            total_cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            num_turns=0,
            duration_ms=600_000,
            raw_stdout=stdout,
            raw_stderr=stderr,
            returncode=-1,
        )
