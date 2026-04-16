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
from pathlib import Path

__all__ = ["write_mcp_config", "write_system_append"]


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
# TODO(task 2a.3.4): add run_claude_headless(cwd, briefing_path, mcp_config_path,
#                                            system_append_path, model, max_turns,
#                                            max_budget_usd, build_cmd) -> ClaudeRunResult
# TODO(task 2a.3.4): add _parse_claude_json(stdout) -> dict for the headless JSON schema
