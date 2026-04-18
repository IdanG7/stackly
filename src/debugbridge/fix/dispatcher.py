"""Fix command dispatcher -- orchestrates hand-off and autonomous modes.

Hand-off mode (``run_handoff``, task 2a.2.2): captures crash state via MCP,
writes a briefing file and MCP config under ``.debugbridge/``, then launches
an interactive Claude Code session so the developer can collaborate on the fix.

Autonomous mode (``run_autonomous``, task 2a.3.5): capture → briefing →
worktree → claude headless loop → build validation → patch/failure.

Architecture constraint (PLAN.md decision #1): this module does NOT import
``debugbridge.session``. All debugger-state access goes through MCP.
"""

from __future__ import annotations

import time
from pathlib import Path

from debugbridge.fix.briefing import (
    append_retry_feedback,
    extract_source_snippets,
    render_briefing,
    write_briefing,
)
from debugbridge.fix.build_runner import run_command
from debugbridge.fix.claude_runner import (
    run_claude_headless,
    run_claude_interactive,
    write_mcp_config,
    write_system_append,
)
from debugbridge.fix.mcp_client import capture_crash, ensure_server_running, shutdown_server
from debugbridge.fix.models import AttemptRecord, FixResult
from debugbridge.fix.patch_writer import write_failure_report, write_patch
from debugbridge.fix.worktree import (
    capture_diff,
    cleanup_worktree_on_failure,
    cleanup_worktree_on_success,
    create_worktree,
    ensure_gitignore,
)


def run_handoff(
    repo: Path,
    pid: int,
    host: str = "127.0.0.1",
    port: int = 8585,
    conn_str: str | None = None,
) -> FixResult:
    """Capture crash, write briefing, launch interactive Claude Code session.

    Flow:
    1. ``ensure_gitignore`` -- add ``.debugbridge/`` to ``.gitignore``.
    2. ``ensure_server_running`` -- spawn ``debugbridge serve`` if not already up.
    3. ``capture_crash`` -- attach to PID via MCP and snapshot crash state.
    4. ``extract_source_snippets`` + ``render_briefing`` + ``write_briefing``
       -- assemble the crash briefing Markdown file.
    5. ``write_mcp_config`` -- write ``mcp-config.json`` for Claude Code.
    6. ``run_claude_interactive`` -- launch claude with ``--mcp-config``,
       ``--strict-mcp-config``, and a positional message referencing the briefing.

    The server is intentionally NOT shut down after claude exits because in
    hand-off mode the user may continue interacting with Claude and the MCP
    server.

    Returns a :class:`FixResult` with ``mode="handoff"``.
    """
    mcp_url = f"http://{host}:{port}/mcp"

    # 1. Setup
    ensure_gitignore(repo)
    debugbridge_dir = repo / ".debugbridge"
    debugbridge_dir.mkdir(parents=True, exist_ok=True)

    # 2. Server
    ensure_server_running(host, port)
    try:
        # 3. Capture
        capture = capture_crash(pid, mcp_url, conn_str)

        # 4. Briefing
        snippets = extract_source_snippets(repo, capture.callstack)
        content = render_briefing(capture, snippets, build_cmd=None)
        briefing_path = debugbridge_dir / "briefings" / f"crash-{capture.crash_hash}.md"
        write_briefing(briefing_path, content)

        # 5. MCP config for Claude
        mcp_config_path = write_mcp_config(debugbridge_dir, host, port)

        # 6. Launch interactive
        briefing_rel = briefing_path.relative_to(repo)
        returncode = run_claude_interactive(repo, briefing_rel, mcp_config_path)

        return FixResult(
            ok=returncode == 0,
            mode="handoff",
            crash_hash=capture.crash_hash,
        )
    finally:
        # Don't shut down server in handoff mode -- user is now interacting.
        # If we spawned it, leave it running for the Claude session.
        pass


def run_autonomous(
    repo: Path,
    pid: int,
    host: str = "127.0.0.1",
    port: int = 8585,
    build_cmd: str | None = None,
    test_cmd: str | None = None,
    model: str = "sonnet",
    max_attempts: int = 3,
    max_budget_usd: float = 0.75,
    conn_str: str | None = None,
) -> FixResult:
    """Autonomous fix: capture -> worktree -> claude headless -> build -> patch/fail.

    Flow:
    1. ``ensure_gitignore`` + ``ensure_server_running``.
    2. ``capture_crash`` via MCP.
    3. Build briefing, write MCP config + system-append under ``.debugbridge/``.
    4. ``create_worktree`` on a fresh branch.
    5. Copy briefing into worktree so claude can ``@``-reference it.
    6. Loop up to ``max_attempts``:
       - ``run_claude_headless`` in the worktree.
       - If claude errors, break (no build attempt).
       - ``run_command(build_cmd)`` in the worktree.
       - If build passes: ``capture_diff`` -> ``write_patch`` ->
         ``cleanup_worktree_on_success`` -> return ok.
       - If build fails: record attempt, continue to next iteration.
         (Retry feedback -- appending build errors to briefing -- lands in 2a.3.6.)
    7. If all attempts exhausted: ``write_failure_report`` ->
       ``cleanup_worktree_on_failure`` -> return not-ok.

    The server is shut down in the ``finally`` block only if we spawned it
    (``server_proc is not None``).
    """
    mcp_url = f"http://{host}:{port}/mcp"

    # 1. Setup
    ensure_gitignore(repo)
    debugbridge_dir = repo / ".debugbridge"
    debugbridge_dir.mkdir(parents=True, exist_ok=True)

    # 2. Server
    server_proc = ensure_server_running(host, port)

    try:
        # 3. Capture
        capture = capture_crash(pid, mcp_url, conn_str)
        crash_hash = capture.crash_hash

        # 4. Briefing
        snippets = extract_source_snippets(repo, capture.callstack)
        content = render_briefing(capture, snippets, build_cmd=build_cmd)
        briefing_path = debugbridge_dir / "briefings" / f"crash-{crash_hash}.md"
        write_briefing(briefing_path, content)

        # 5. Config files (in repo's .debugbridge/, NOT the worktree)
        mcp_config_path = write_mcp_config(debugbridge_dir, host, port)
        system_append_path = write_system_append(debugbridge_dir)

        # 6. Worktree
        worktree = create_worktree(repo, crash_hash)

        # Copy briefing into worktree so claude can read it via @path
        wt_briefing = worktree / ".debugbridge" / "briefings" / f"crash-{crash_hash}.md"
        wt_briefing.parent.mkdir(parents=True, exist_ok=True)
        wt_briefing.write_text(content, encoding="utf-8")

        attempts: list[AttemptRecord] = []

        for attempt_num in range(1, max_attempts + 1):
            t0 = time.monotonic()

            # Run claude headless in the worktree
            claude_result = run_claude_headless(
                cwd=worktree,
                briefing_path=wt_briefing,
                mcp_config_path=mcp_config_path,
                system_append_path=system_append_path,
                model=model,
                max_budget_usd=max_budget_usd,
                build_cmd=build_cmd,
            )

            if claude_result.is_error:
                duration = time.monotonic() - t0
                attempts.append(
                    AttemptRecord(
                        attempt=attempt_num,
                        claude_result=claude_result,
                        build_ok=False,
                        build_output="(claude errored, build not run)",
                        duration_s=duration,
                    )
                )
                break

            # Build
            build_ok = True
            build_output = ""
            if build_cmd:
                build_ok, build_output = run_command(build_cmd, cwd=worktree)

            duration = time.monotonic() - t0
            attempts.append(
                AttemptRecord(
                    attempt=attempt_num,
                    claude_result=claude_result,
                    build_ok=build_ok,
                    build_output=build_output,
                    duration_s=duration,
                )
            )

            if build_ok:
                # Success -- emit patch
                diff = capture_diff(worktree)
                patch_path = write_patch(repo, crash_hash, diff) if diff else None
                cleanup_worktree_on_success(repo, worktree, crash_hash)

                return FixResult(
                    ok=True,
                    mode="auto",
                    crash_hash=crash_hash,
                    patch_path=patch_path,
                    attempts=attempts,
                    total_cost_usd=sum(a.claude_result.total_cost_usd for a in attempts),
                    total_input_tokens=sum(a.claude_result.input_tokens for a in attempts),
                    total_output_tokens=sum(a.claude_result.output_tokens for a in attempts),
                    worktree_path=worktree,
                    worktree_preserved=False,
                )

            # Build failed -- append feedback for next iteration
            append_retry_feedback(
                wt_briefing,
                attempt_num,
                build_output,
                claude_result.result,
            )

        # All attempts exhausted
        failure_path = write_failure_report(repo, crash_hash, attempts, "all attempts exhausted")
        cleanup_worktree_on_failure(repo, worktree, crash_hash)

        return FixResult(
            ok=False,
            mode="auto",
            crash_hash=crash_hash,
            failure_report_path=failure_path,
            attempts=attempts,
            total_cost_usd=sum(a.claude_result.total_cost_usd for a in attempts),
            total_input_tokens=sum(a.claude_result.input_tokens for a in attempts),
            total_output_tokens=sum(a.claude_result.output_tokens for a in attempts),
            worktree_path=worktree,
            worktree_preserved=True,
        )
    finally:
        if server_proc is not None:
            shutdown_server(server_proc)
