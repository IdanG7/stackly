# Phase 2a — Fix-loop MVP — Research

**Researched:** 2026-04-15
**Domain:** Claude Code subprocess orchestration, MCP client plumbing, Windows git worktrees, crash-context prompt engineering
**Confidence:** HIGH on Claude Code headless mechanics and MCP wiring (official docs); HIGH on git worktree mechanics (upstream Git docs); MEDIUM on Windows-specific subprocess quirks (community report, aligns with existing Phase 1 code); MEDIUM on token-budget estimates (first-order estimate, needs empirical calibration).

## Summary

The phase is plumbing, not invention. Every moving part has a documented, idiomatic answer:

1. **Claude Code headless.** `claude -p` (same binary, no separate "headless" mode) emits JSON with `total_cost_usd`, `usage`, `session_id`, `num_turns`, `result` — everything we need for cost tracking and retry decisions. `--mcp-config` takes a file with `mcpServers` keyed by name (HTTP transport is `"type": "http"`, `"url": "…/mcp"`). MCP tools become tool names `mcp__<server>__<tool>` for `--allowedTools`. Use `--permission-mode bypassPermissions` (or `--dangerously-skip-permissions`) in the autonomous worktree — it still protects `.git`, `.claude`, etc. from overwrite. Set `cwd` on the subprocess to the worktree directory; Claude Code does not have a `--cwd` flag, and bypass mode does not escape its protected-path list.
2. **Interactive hand-off.** `claude "query"` launches an interactive session with the query as the first user message — that's the primary mechanism (not an `--initial-prompt` flag). Pass the briefing via `--append-system-prompt-file` (or as the positional argument that references the briefing file via `@.debugbridge/briefing.md`) and pre-register the MCP server via `--mcp-config` or a project `.mcp.json`.
3. **MCP client from Python.** Exactly the pattern already in `scripts/e2e_smoke.py`: `streamablehttp_client(URL)` + `ClientSession` inside nested `async with`. Reuse one session across multiple tool calls; initialize once. Auto-spawn `debugbridge serve` via `subprocess.Popen` + readiness-wait on the `"Uvicorn running"` line.
4. **Git worktrees.** `git worktree add <path> -b <branch>` from Python `subprocess.run`. Create a unique branch name like `debugbridge/fix-<hash>` from HEAD. Diff against that branch's base commit for the `.patch` output. Cleanup with `git worktree remove --force` on success; leave on failure.
5. **Briefing format.** Markdown with explicit sections (Crash, Stack, Locals, Source, Task, Constraints). Include source files as fenced code blocks — Claude Code is good at ingesting that. Target ~15–25K input tokens for a realistic Windows crash; set `--max-turns 20` and `--max-budget-usd 0.75` as belt-and-suspenders.
6. **Cost tracking.** Parse `total_cost_usd` and `usage` from `--output-format json`. The JSON is printed as the final line; everything before on stdout is log noise. Exit code is 0 on success, non-zero on error (API failures, budget limits, max-turns).
7. **Subprocess hygiene.** Phase 1's `e2e_smoke.py` pattern is correct. On Windows use `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` for the server, standard `SIGTERM` for Claude Code. For the attached crash target, detach via MCP before killing (already handled by stopping the server, which drops pybag).
8. **CLI design.** Add `fix` as a third Typer command next to `serve`/`doctor`/`version`. Rich progress for long phases. Default text summary at end; add `--json` flag for scripting.

**Primary recommendation:** Build Phase 2a as seven atomic components behind a single Typer command: (1) MCP client auto-spawn + capture, (2) briefing generator, (3) worktree manager, (4) Claude Code subprocess wrapper with JSON parsing, (5) build/test runner, (6) diff-to-patch writer, (7) hand-off vs autonomous mode dispatcher. Each component is independently unit-testable with the Claude Code step being the only integration test that needs a live `claude` binary.

---

## Standard Stack

### Core

| Library / Tool | Version | Purpose | Why Standard |
|----------------|---------|---------|--------------|
| `claude` CLI (Claude Code) | 2.x (current release channel) | Headless fix-generation subprocess | Only AI coding agent with MCP support, tool call loop, and structured JSON output out of the box |
| `mcp` Python package | `>=1.27,<1.28` | MCP client to talk to `debugbridge serve` | Already in Phase 1 deps; `streamablehttp_client` is the official transport |
| `typer` + `rich` | (already pinned) | CLI subcommand, progress UI | Already in Phase 1 |
| `git` CLI | >= 2.20 (worktree mature) | Worktree creation, diff | Shelling out is simpler and more reliable than `pygit2`/`GitPython` for worktree ops; no extra dep |
| `subprocess` (stdlib) | n/a | Spawn `debugbridge serve`, `claude`, `git`, build/test commands | No library dependency needed |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `anyio` (already transitive via `mcp`) | — | Async runtime for MCP client | Inherited, don't add explicitly |
| `hashlib` (stdlib) | — | Short hash for worktree/patch names | Use `hashlib.sha1(...).hexdigest()[:8]` |
| `json` (stdlib) | — | Parse Claude Code's `--output-format json` result | Standard |
| `pathlib.Path` (stdlib) | — | Windows-safe path handling | Already used throughout DebugBridge |

### Alternatives Considered

| Instead of | Could Use | Tradeoff / Why not for 2a |
|------------|-----------|---------------------------|
| Claude Code CLI subprocess | `claude-agent-sdk` Python package | **Do NOT use in 2a.** The community report notes that `claude-agent-sdk` "crashes on Windows with Python 3.12 due to an `anyio` asyncio backend incompatibility." DebugBridge is Windows-native. Use CLI subprocess. |
| Claude Code | Anthropic API directly via `anthropic` SDK | Reinventing the tool-loop + MCP client. Phase 2a ships faster with `claude -p`; revisit for Phase 4 cost optimization. |
| `git` CLI shell-outs | `pygit2`, `GitPython`, `dulwich` | `pygit2` needs `libgit2` DLL on Windows (install hassle). `GitPython` wraps `git` anyway. Shelling out is simplest and matches user's git install. |
| `subprocess.Popen` for Claude Code | `asyncio.create_subprocess_exec` | Claude Code writes JSON on stdout at end; we want the whole thing. Blocking `.communicate()` in a thread is simplest. Only use async for the MCP client (which already needs it). |

**Installation:** No new Python deps beyond Phase 1. The `claude` CLI is a user prerequisite (document in README / `debugbridge doctor`). To detect:

```python
import shutil
claude_path = shutil.which("claude")
if not claude_path:
    # Doctor says: install claude-code (https://docs.claude.com/en/docs/claude-code/getting-started)
```

---

## Claude Code Headless — Detailed Mechanics

### 1.1 CLI invocation

Exact command shape for 2a autonomous mode:

```bash
claude -p \
  --output-format json \
  --mcp-config .debugbridge/mcp-config.json \
  --permission-mode bypassPermissions \
  --allowedTools "Read,Edit,Write,Glob,Grep,Bash(cmake *),Bash(git diff *),mcp__debugbridge__*" \
  --max-turns 20 \
  --max-budget-usd 0.75 \
  --model sonnet \
  --append-system-prompt-file .debugbridge/system-append.md \
  "$(cat .debugbridge/briefing.md)"
```

**Why these flags** (from [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)):

| Flag | Effect for us |
|------|---------------|
| `-p` / `--print` | Non-interactive; prints the final result and exits. Required for subprocess orchestration. |
| `--output-format json` | Emits a single JSON object on stdout with `result`, `session_id`, `total_cost_usd`, `usage`, `num_turns`, `duration_ms`, `is_error`, `subtype`. |
| `--mcp-config <path>` | Loads our `debugbridge` MCP server from a JSON file (schema below). Works in headless mode (confirmed in docs). |
| `--permission-mode bypassPermissions` | Skips per-tool approval prompts. Still protects `.git`, `.vscode`, `.idea`, `.husky`, `.claude` from overwrite — which is exactly what we want since the worktree's `.git` must not be touched. `--dangerously-skip-permissions` is the explicit equivalent. |
| `--allowedTools` | Layer of defense if bypass mode is not ideal; explicit allowlist. Syntax: `"Read,Edit,Bash(cmake *),mcp__debugbridge__*"`. |
| `--max-turns N` | Hard cap on agentic turns. Exits non-zero when hit. Prevents runaway. |
| `--max-budget-usd N` | Dollar ceiling; aborts cleanly when exceeded. Belt-and-suspenders with 3-attempt cap. |
| `--model sonnet` | Lock to Sonnet (default is whatever user has configured). For Phase 2a we pick one model; tiered routing is Phase 4. |
| `--append-system-prompt-file` | Adds our "you are a crash-fix agent" instructions to Claude Code's default system prompt (preserves its built-in tooling). |
| positional `"query"` | The user message. For a large briefing, pass `@.debugbridge/briefing.md` instead to reference via file. |

> **Known quirk (Windows)**: The community guide "[Running Claude Code from Windows CLI](https://dstreefkerk.github.io/2026-01-running-claude-code-from-windows-cli/)" warns that long prompts on the command line "fail silently even under Windows's 8191-character limit." **Mitigation:** put the briefing in a file and pass `"Read .debugbridge/briefing.md and proceed"` as the prompt, or use stdin (see §1.3).

### 1.2 The `--mcp-config` JSON file

Schema (from [Claude Code MCP docs](https://code.claude.com/docs/en/mcp)):

```json
{
  "mcpServers": {
    "debugbridge": {
      "type": "http",
      "url": "http://127.0.0.1:8585/mcp"
    }
  }
}
```

For stdio transport (alternative, not our default):

```json
{
  "mcpServers": {
    "debugbridge": {
      "command": "uv",
      "args": ["run", "debugbridge", "serve", "--transport", "stdio"],
      "env": {}
    }
  }
}
```

**Important:** when Claude Code invokes MCP tools, they are namespaced as `mcp__<servername>__<toolname>`. For us that means:

- `mcp__debugbridge__attach_process`
- `mcp__debugbridge__get_exception`
- `mcp__debugbridge__get_callstack`
- `mcp__debugbridge__get_threads`
- `mcp__debugbridge__get_locals`
- `mcp__debugbridge__set_breakpoint`
- `mcp__debugbridge__step_next`
- `mcp__debugbridge__continue_execution`

Wildcard permissions: `mcp__debugbridge__*` allows all tools from our server.

> **Scope note:** `--mcp-config` is fully documented as working in headless (`-p`) mode. For additional isolation, add `--strict-mcp-config` to ignore user-level MCP configs from `~/.claude.json`. Our workflow already pre-captures crash data via our own MCP client before launching `claude`, so Claude Code's use of our MCP server is mostly a formality for follow-up questions.

### 1.3 Passing the prompt — three options

| Method | When | Example |
|--------|------|---------|
| Positional arg | Short prompts (< ~1K chars) | `claude -p "fix the bug"` |
| stdin pipe | Medium prompts | `cat briefing.md \| claude -p "Fix this crash. Briefing above."` |
| `@file.md` reference inside prompt | Any size; Claude reads the file | `claude -p "Read @.debugbridge/briefing.md and fix the crash"` |

**Our choice for 2a:** combine `--append-system-prompt-file` (persistent instructions) with positional arg referencing the briefing via `@path` (handled as a Read). This keeps the command line short on Windows and maps cleanly to our Markdown briefing file.

### 1.4 The JSON output schema

From [the headless docs](https://code.claude.com/docs/en/headless) plus community reference gist, a successful `--output-format json` response looks like:

```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "result": "I added a null check in crash.cpp line 42...",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "total_cost_usd": 0.1834,
  "duration_ms": 28500,
  "duration_api_ms": 24200,
  "num_turns": 7,
  "usage": {
    "input_tokens": 450,
    "output_tokens": 1200,
    "cache_read_input_tokens": 18000,
    "cache_creation_input_tokens": 2100
  },
  "modelUsage": {
    "claude-sonnet-4-6": {
      "inputTokens": 450,
      "outputTokens": 1200,
      "cacheReadInputTokens": 18000,
      "costUSD": 0.1834
    }
  },
  "structured_output": null
}
```

**Fields we care about:**

| Field | Use |
|-------|-----|
| `is_error` | Quick success/failure flag |
| `subtype` | On error: `"error_max_turns"`, `"error_during_execution"`, etc. Use to decide retry vs abort. |
| `result` | The final assistant message (natural language summary of the fix). Print to user. |
| `total_cost_usd` | The number we report: `"est cost $0.18"`. |
| `usage.input_tokens + usage.cache_read_input_tokens` | Total input: `"tokens: 18.5K in"`. |
| `usage.output_tokens` | Output: `"2.1K out"`. |
| `num_turns` | How many agentic turns Claude took (sanity signal). |
| `session_id` | Save it — enables `claude --resume <id>` if we ever want to re-prompt with build error on retry. |

**Exit codes** (inferred from community usage; docs say "exits with an error when [max-turns] limit is reached"):
- `0` = success
- Non-zero = any of: max-turns exceeded, budget exceeded, API error, tool approval denied in non-interactive mode, invalid flags.

Our retry logic reads `is_error` from the JSON (not just exit code) and inspects `subtype` before deciding to retry.

> **Edge case:** If Claude Code fails before producing JSON (e.g. auth error at startup, crash), stdout may be empty. Wrap the `json.loads()` in try/except and fall back to treating empty output + non-zero exit as a hard failure.

### 1.5 Does it ever prompt interactively?

**`--permission-mode bypassPermissions`** + no TTY on stdin = guaranteed silent for almost all operations. The protected paths still block writes to `.git`, `.claude`, etc. but those generate a tool-call failure, not an interactive prompt, in `-p` mode. **Important nuance from [permission-modes docs](https://code.claude.com/docs/en/permission-modes)**: in `dontAsk` mode, "explicit `ask` rules are also denied rather than prompting. This makes the mode fully non-interactive for CI pipelines." We could use `dontAsk` instead of `bypassPermissions` for even stricter semantics — but we'd need to list every tool we want to allow. For 2a, `bypassPermissions` + `--allowedTools` is the pragmatic choice.

The first time a user runs Claude Code with `--dangerously-skip-permissions`, there's a one-time confirmation dialog. **In headless mode this becomes problematic.** Two mitigations:
1. Run `claude --dangerously-skip-permissions` interactively once during `debugbridge doctor` to surface the warning to the user.
2. Set `"skipDangerousModePermissionPrompt": true` in `~/.claude/settings.json` via `--settings` flag or pre-write the file.

### 1.6 Limiting file access to the worktree

**There is no `--cwd` flag and no `--sandbox-root` flag.** Claude Code runs in `subprocess.Popen(cwd=worktree)`'s working directory. What it *will* access:

- Files under the launch directory (normal Read/Edit): ✅
- Files in `--add-dir` additional directories: ✅ (don't pass any)
- Files addressed by absolute path via Bash (`cat /somewhere/else/file`): ✅ unless blocked by a deny rule

**To confine Claude Code to the worktree**:

1. Launch with `cwd=worktree_path` — sets the default working directory for all relative paths.
2. Do **not** pass `--add-dir`.
3. Use `--permission-mode bypassPermissions` + explicit `--allowedTools` that don't include broad shell access (no `Bash(*)` — only `Bash(cmake *)`, `Bash(cargo test *)`, etc. as dictated by `--build-cmd`).
4. Add deny rules for anything outside: Claude Code's Read/Edit follow gitignore-style patterns (see [permission rule syntax](https://code.claude.com/docs/en/permissions#permission-rule-syntax)). `Edit(/src/**)` in a settings file scopes edits to the project's `src/`.
5. **OS-level enforcement (stronger, optional for 2a):** The [Sandboxing feature](https://code.claude.com/docs/en/sandboxing) isolates Bash subprocess filesystem + network access. It's **macOS/Linux/WSL2 only** — not Windows-native. So for 2a we rely on cwd + permission rules only. Document this honestly.

**Bottom line for 2a:** `cwd=worktree` + `bypassPermissions` + narrow `--allowedTools` is "good enough." Absolute worst case, Claude Code writes to the user's tree outside the worktree — but the user can see exactly what changed via `git status` on both the main tree and the worktree. Phase 2b can add Windows Job Object containment if needed.

### 1.7 Size and context limits

- **Prompt/context window:** Claude Sonnet 4.6 = 200K tokens input. Claude Code docs flag "MCP tool output exceeds 10,000 tokens" as a soft warning (override with `MAX_MCP_OUTPUT_TOKENS`). Our briefing + 3–5 source files will be comfortably under 50K.
- **Prompt on command line:** Windows 8191-char cmd-line limit. **Always use `@file` references or stdin for briefings**, never inline on the command line.

### 1.8 MCP tokens: counted or separate?

MCP tool calls consume tokens in the main conversation — they're not separate. Each tool call's result is injected back as a tool-result message Claude sees. The `usage.cache_read_input_tokens` value is where prompt caching kicks in for the static system prompt + tool definitions (critical for Phase 4 cost reduction but already happens automatically). For 2a, treat MCP tokens as regular conversation tokens in cost accounting.

---

## Interactive Claude Code Session with Preloaded Context

### 2.1 The idiomatic pattern

From the CLI reference: **`claude "initial query"` starts an interactive session with the query as the first user message.** That's exactly what hand-off mode wants.

```bash
# Launch Claude Code in the user's repo with the briefing as the first message
claude --mcp-config .debugbridge/mcp-config.json \
       "I've just captured a crash. Read @.debugbridge/briefing.md and propose a fix."
```

The session opens interactively; the user sees Claude already reading the briefing file. Perfect for hand-off mode.

### 2.2 Alternatives considered

| Approach | Verdict |
|----------|---------|
| `--initial-prompt` flag | **Doesn't exist.** The positional argument to `claude` serves this role. |
| Write to a file, print a hint, let user type `/continue` | Worse UX — user has to type. |
| System prompt injection via `--append-system-prompt-file` | Complements the positional-arg approach but doesn't replace it. |

### 2.3 Pre-registering debugbridge MCP for the session

Three ways, in order of preference for our use case:

1. **`--mcp-config` flag** — session-scoped, no persistent state. Best for ad-hoc crash sessions. **Recommended.**
2. **Project `.mcp.json`** — commit into user's repo as `.debugbridge/.mcp.json` or copy to repo root. Persistent but pollutes user's repo.
3. **User-scoped via `claude mcp add`** — sets it in `~/.claude.json`; persists across sessions. Good for users who use debugbridge a lot, but adds state.

For 2a: generate `.debugbridge/mcp-config.json` per run and pass with `--mcp-config`. Clean, no persistent state, no gitignore concerns.

```python
# Python sketch
def write_mcp_config(repo_path: Path, host: str, port: int) -> Path:
    cfg = {
        "mcpServers": {
            "debugbridge": {
                "type": "http",
                "url": f"http://{host}:{port}/mcp",
            }
        }
    }
    path = repo_path / ".debugbridge" / "mcp-config.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
    return path
```

### 2.4 Launching the interactive session from Python

```python
# Hand-off mode: we don't wait for claude to exit. fork-and-detach on Unix,
# use subprocess.DETACHED_PROCESS on Windows.
subprocess.Popen(
    [
        "claude",
        "--mcp-config", str(mcp_config_path),
        f"Read @{briefing_path.relative_to(repo_path)} and propose a fix for this crash.",
    ],
    cwd=str(repo_path),
    # Windows: keep the terminal alive in the foreground of the user's shell.
    # We actually DO want this process to share the terminal — the user is
    # driving it interactively. So no detachment; typer just returns after
    # exec'ing (subprocess.run, not Popen).
)
# Actually for hand-off we want os.execvp on Unix or subprocess.run on Windows
# so the TTY handoff is clean.
```

**Windows handoff note:** `subprocess.run(["claude", ...])` inherits the parent's stdin/stdout/stderr by default, which is what we want — the user's terminal becomes Claude Code's terminal. When `debugbridge fix` returns, the user is already in Claude Code's REPL.

---

## MCP Client from Python (Talking to Our Own Server)

### 3.1 The pattern, confirmed

The code in `scripts/e2e_smoke.py` is the correct template. Key bits:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("http://127.0.0.1:8585/mcp") as (read, write, _session_id_cb):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()

        # Structured call with kwargs
        result = await session.call_tool("attach_process", {"pid": 1234})
        assert result.structuredContent["status"] == "attached"

        exc = await session.call_tool("get_exception", {})
        # Pydantic fields come back via .structuredContent
        stack = await session.call_tool("get_callstack", {"max_frames": 20})
        # ...
```

**Session lifecycle answers:**

- `initialize()` is called **once per `ClientSession`**. Required before any `call_tool`.
- A `ClientSession` **can be reused across many tool calls** — that's its intent. Use the same session for the whole crash-capture sequence.
- Exit via `async with` context manager; do not call `close()` manually.
- The underlying `streamablehttp_client` stream is also a context manager and wraps the socket lifecycle.

### 3.2 Complex args: how tools receive them

`session.call_tool(name, dict)` — the `dict` is JSON-serialized and maps directly onto the tool's Python signature. For `attach_process(pid: int | None = None, process_name: str | None = None, conn_str: str | None = None)`:

```python
# Local attach by PID
await session.call_tool("attach_process", {"pid": 1234})

# Remote attach via dbgsrv
await session.call_tool("attach_process", {
    "pid": 1234,
    "conn_str": "tcp:server=192.168.1.10,port=5555",
})
```

The result's `.structuredContent` is the Pydantic model's `.model_dump()` — plain dict. The `.content` list is the "AI-facing" text summary FastMCP auto-generates.

### 3.3 Wrapping async for a sync CLI

Typer commands are sync by default. Two patterns:

**Option A — one-shot asyncio.run:**

```python
import asyncio

def capture_crash(pid: int, mcp_url: str, conn_str: str | None = None) -> CrashCapture:
    return asyncio.run(_capture_async(pid, mcp_url, conn_str))

async def _capture_async(pid, mcp_url, conn_str) -> CrashCapture:
    async with streamablehttp_client(mcp_url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            attach = await s.call_tool("attach_process",
                {"pid": pid, **({"conn_str": conn_str} if conn_str else {})})
            exc = await s.call_tool("get_exception", {})
            stack = await s.call_tool("get_callstack", {"max_frames": 32})
            threads = await s.call_tool("get_threads", {})
            locals_ = await s.call_tool("get_locals", {"frame_index": 0})
            return CrashCapture(
                attach=attach.structuredContent,
                exception=exc.structuredContent,
                callstack=stack.structuredContent,
                threads=threads.structuredContent,
                locals=locals_.structuredContent,
            )
```

**Option B — keep the session open for follow-ups:** If we want Claude Code's Agent-SDK-via-CLI to be able to hit the same live session (which needs the pybag attach to persist), the pybag session **must live in the server process**, not ours. That's already how it works: `DebugSession` is a server-side singleton. Our CLI's MCP client can close and Claude Code can reopen — the attach persists on the server side.

### 3.4 Auto-spawning `debugbridge serve`

The e2e_smoke pattern works. Improvements for the `fix` command:

```python
def ensure_server_running(host: str, port: int) -> subprocess.Popen | None:
    """Return the spawned process if we started one; None if it was already up."""
    # Probe: is something listening?
    try:
        with socket.create_connection((host, port), timeout=0.5):
            log(f"debugbridge serve already running on {host}:{port}")
            return None
    except OSError:
        pass

    log("spawning debugbridge serve…")
    proc = subprocess.Popen(
        ["uv", "run", "debugbridge", "serve", "--host", host, "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    # Wait for readiness
    deadline = time.time() + 30
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"debugbridge serve exited: code={proc.returncode}")
            continue
        log(f"server: {line.rstrip()}")
        if "Uvicorn running" in line:
            return proc
    raise TimeoutError("debugbridge serve did not become ready within 30s")
```

### 3.5 Shutdown cleanup

```python
def shutdown_server(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
```

The server's shutdown drops pybag, which detaches from the crashed process — this matches Phase 1 behavior.

---

## Git Worktree Management from Python

### 4.1 Canonical commands

From [git-worktree(1)](https://git-scm.com/docs/git-worktree):

```bash
# Create worktree at path, on new branch from HEAD
git worktree add .debugbridge/wt-a1b2c3d4 -b debugbridge/fix-a1b2c3d4

# Create from a specific commit (if we want "fix from latest tag" semantics)
git worktree add .debugbridge/wt-a1b2c3d4 -b debugbridge/fix-a1b2c3d4 main

# List
git worktree list

# Remove (disallows if dirty unless --force)
git worktree remove --force .debugbridge/wt-a1b2c3d4

# Forcibly prune orphaned entries (if directory was deleted externally)
git worktree prune
```

### 4.2 Python orchestration sketch

```python
import subprocess
import hashlib
from pathlib import Path

def compute_crash_hash(crash_capture: dict) -> str:
    """Deterministic 8-char hash from crash fingerprint."""
    fp = f"{crash_capture['exception']['code']}@{crash_capture['callstack'][0]['function']}"
    return hashlib.sha1(fp.encode()).hexdigest()[:8]

def create_worktree(repo: Path, crash_hash: str) -> Path:
    wt_name = f"wt-{crash_hash}"
    wt_path = repo / ".debugbridge" / wt_name
    branch = f"debugbridge/fix-{crash_hash}"

    # Ensure base directory
    (repo / ".debugbridge").mkdir(exist_ok=True)

    # If an old worktree with this hash exists from a failed run, clean it up
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    if str(wt_path) in result.stdout:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=repo, check=False,  # ok if it fails
        )
    # Also delete the branch if it still exists
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo, capture_output=True, check=False,
    )

    subprocess.run(
        ["git", "worktree", "add", str(wt_path), "-b", branch],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    return wt_path

def capture_diff(worktree: Path, base_branch: str = "HEAD") -> str:
    """Return a unified diff of the worktree against its branch-off point.

    Because we created the worktree branch from HEAD at create-time, diffing
    against that HEAD-commit captures only Claude Code's edits.
    """
    # Find the merge-base: the commit the worktree branch was created from
    result = subprocess.run(
        ["git", "merge-base", "HEAD", base_branch],
        cwd=worktree, capture_output=True, text=True, check=True,
    )
    base = result.stdout.strip()

    diff = subprocess.run(
        ["git", "diff", "--binary", base, "HEAD"],
        cwd=worktree, capture_output=True, text=True, check=True,
    )
    # If Claude committed changes, diff = committed changes.
    # If Claude only edited working tree, add index + working tree:
    unstaged = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=worktree, capture_output=True, text=True, check=True,
    )
    return diff.stdout + unstaged.stdout

def write_patch(repo: Path, crash_hash: str, diff: str) -> Path:
    patches_dir = repo / ".debugbridge" / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    path = patches_dir / f"crash-{crash_hash}.patch"
    path.write_text(diff, encoding="utf-8")
    return path

def cleanup_worktree_on_success(repo: Path, wt_path: Path) -> None:
    # Successful run: remove worktree, keep the branch around for 24h
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=repo, check=False,
    )

def cleanup_worktree_on_failure(wt_path: Path) -> None:
    # Failed run: leave everything for inspection. Just log the path.
    log(f"worktree preserved for inspection: {wt_path}")
```

### 4.3 Handling dirty user tree / no-git cases

**Dirty tree**: `git worktree add` does NOT require a clean main tree. It creates a separate checkout, so user's uncommitted changes are untouched. **Log a warning** if the user's tree is dirty, so they know the worktree branches off HEAD (not their working edits), but don't abort.

```python
def detect_dirty(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return bool(result.stdout.strip())

def detect_git_repo(repo: Path) -> bool:
    # Walks up to find .git
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"
```

**No git**: early error. "Phase 2a requires `--repo` to be a git repository (for worktree isolation). Current path <x> is not a git repo. Run `git init` or point `--repo` at an initialized repository."

### 4.4 `.debugbridge/` gitignore

On first use in a repo, append `/.debugbridge/` to the repo's `.gitignore`:

```python
def ensure_gitignore(repo: Path) -> None:
    gi = repo / ".gitignore"
    entry = "/.debugbridge/\n"
    if not gi.exists():
        gi.write_text(entry)
        return
    content = gi.read_text(encoding="utf-8", errors="replace")
    if "/.debugbridge" in content or ".debugbridge/" in content:
        return
    if not content.endswith("\n"):
        content += "\n"
    content += entry
    gi.write_text(content, encoding="utf-8")
```

---

## Crash Briefing Format & Prompt Engineering

### 5.1 Template

Recommended briefing structure (Markdown, deterministic sections):

```markdown
# Crash briefing — <crash_hash>

**Generated:** <ISO-8601 UTC>
**Target:** <binary path>
**PID:** <pid>

## Exception

- **Code:** EXCEPTION_ACCESS_VIOLATION (0xC0000005)
- **Address:** 0x00007ff6a1b2c3d4
- **Module!Symbol:** crash_app!crash_null+0x2a
- **Source:** crash.cpp:42

## Call stack (innermost first)

| # | Function | Module | Source |
|---|----------|--------|--------|
| 0 | `crash_null` | crash_app | crash.cpp:42 |
| 1 | `main` | crash_app | main.cpp:18 |
| ... | ... | ... | ... |

## Locals at frame 0

| Name | Type | Value |
|------|------|-------|
| `p` | `int*` | `0x0000000000000000` |
| ... | ... | ... |

## Source context

### crash.cpp (lines 30–55)

```cpp
void crash_null() {
    int* p = nullptr;
    *p = 42;   // ← crash here
}
```

### main.cpp (lines 10–30)

```cpp
int main() {
    crash_null();
}
```

## Your task

A C++ program crashed with the access violation above. The root cause is
almost certainly in the call-stack frame 0 function shown above.

1. Read the source files referenced in the stack.
2. Propose a minimal fix that addresses the crash without changing unrelated behavior.
3. Apply the edit with the Edit tool.
4. Run the build command: `<BUILD_CMD>` (use the Bash tool).
5. If the build fails, investigate and iterate.
6. When the build succeeds, stop and summarize the fix in one paragraph.

## Constraints

- Only edit files listed in the Source context section. Do not touch unrelated code.
- Do not modify test files or build configuration.
- Do not add dependencies.
- Do not modify `.git`, `.debugbridge`, or any configuration files.
- If you are unsure, prefer the minimal diff over a "better" refactor.

## Available MCP tools

You have access to a live debugger attached to the crashed process via the
`debugbridge` MCP server. If you need more information, call:

- `mcp__debugbridge__get_exception` — full exception record
- `mcp__debugbridge__get_callstack` — deeper stack (up to 64 frames)
- `mcp__debugbridge__get_locals` — locals at any frame (pass `frame_index`)
- `mcp__debugbridge__get_threads` — all threads

Do not call `set_breakpoint`, `step_next`, or `continue_execution` — those
would resume or interfere with the target.
```

### 5.2 Why this structure

- **Top-down:** exception → stack → locals → source → task. Matches how a human debugger reads a crash.
- **Explicit constraints:** keep the fix minimal; protect the user's code.
- **Task is numbered:** Claude Code follows step-numbered instructions more reliably than prose paragraphs.
- **Tools are listed explicitly:** reduces hallucination; gives Claude the exact tool names.

### 5.3 Source file inclusion strategy

From the stack, collect up to **5 files** and for each include **±15 lines around the referenced line**. Rationale:

- A realistic C++ crash stack has 5–10 frames; we rarely need more than the top 3 files to find root cause.
- 30 lines × 5 files × ~80 chars ≈ 12K chars ≈ 3K tokens. Plus stack table (~0.5K), locals (~0.5K), task/constraints (~0.5K), MCP tool docs (already in Claude Code's context).
- **Estimated briefing size: 5–8K tokens** for typical C++ crashes.

If we add Claude Code's default system prompt + tool definitions + our append ≈ 10–15K tokens baseline. Plus what Claude Code reads autonomously during the fix (Read tool calls), realistic total input for a successful fix: **15–25K tokens**, output **1–3K tokens**. Cost on Sonnet ≈ **$0.05–0.15 per successful attempt** (with prompt caching on the system prompt). Our 3-attempt cap bounds worst case at ~$0.45.

> **Calibration needed:** These are first-order estimates. Phase 2a's integration test should emit cost numbers so we have real data for the README.

### 5.4 System prompt: append or replace?

**Append.** Claude Code's default system prompt includes tool descriptions, working-directory awareness, git instructions, and style guidance. Replacing it loses all that. Use `--append-system-prompt-file .debugbridge/system-append.md`:

```markdown
<!-- .debugbridge/system-append.md -->
You are a crash-fix agent working inside an isolated git worktree. Your only
goal is to produce a minimal diff that fixes the crash described in the user's
briefing. Do not refactor, restructure, add tests, or make stylistic changes
unrelated to the crash. Assume the rest of the codebase is correct. Report
findings concisely at the end; no lengthy explanations unless explicitly asked.
```

### 5.5 User vs system prompt

- Crash-specific data (stack, locals, source snippets) → **user message** (positional argument / `@file` reference). Claude Code doesn't cache the user message, but that's fine — each crash is unique.
- Generic "you are a crash-fix agent" instructions → **append-system-prompt**. Cacheable, same across all runs.

---

## Anthropic API Cost Tracking

**Claude Code emits all cost/token data in its JSON output.** No need for the `anthropic` SDK directly in Phase 2a.

Parsing:

```python
def parse_claude_output(stdout: str) -> dict:
    """Parse the JSON object from claude -p --output-format json.

    Claude Code sometimes prints log lines before the final JSON. The JSON
    object is always the last line on stdout. Strategy: find the last
    balanced top-level JSON object.
    """
    # Common case: stdout is a single JSON object
    stripped = stdout.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Fallback: scan backwards for last line that parses
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"could not parse JSON from claude output: {stdout[:500]}")


def format_cost(parsed: dict) -> str:
    cost = parsed.get("total_cost_usd", 0.0)
    u = parsed.get("usage", {})
    inp = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    out = u.get("output_tokens", 0)
    return f"tokens: {inp/1000:.1f}K in / {out/1000:.1f}K out, est cost ${cost:.2f}"
```

---

## Subprocess Management for Long-Running Agent

### 7.1 The full lifecycle

Pseudocode for `debugbridge fix --auto`:

```python
def fix_autonomous(pid, repo, conn_str, build_cmd, test_cmd):
    # 1. Start server if needed
    server_proc = ensure_server_running("127.0.0.1", 8585)

    try:
        # 2. Capture crash via MCP
        crash = asyncio.run(capture_crash(pid, "http://127.0.0.1:8585/mcp", conn_str))

        # 3. Prep worktree + files
        crash_hash = compute_crash_hash(crash)
        wt_path = create_worktree(repo, crash_hash)
        briefing_path = write_briefing(wt_path, crash)
        mcp_config_path = write_mcp_config(wt_path, "127.0.0.1", 8585)
        system_append_path = write_system_append(wt_path)

        # 4. Claude Code loop (max 3 attempts)
        for attempt in range(1, 4):
            result = run_claude(wt_path, briefing_path, mcp_config_path, system_append_path)
            if result.is_error:
                log(f"attempt {attempt}: Claude Code error: {result.subtype}")
                break  # don't retry on auth errors, max-turns, etc.

            # 5. Run build
            build_ok, build_output = run_build(wt_path, build_cmd)
            if build_ok:
                # 6. Optionally run tests
                if test_cmd:
                    test_ok, test_output = run_test(wt_path, test_cmd)
                    if not test_ok:
                        append_retry_feedback(briefing_path, "Tests failed:", test_output)
                        continue
                log(f"attempt {attempt}: build + tests passed")
                break
            else:
                log(f"attempt {attempt}: build failed, retrying with output as feedback")
                append_retry_feedback(briefing_path, "Build failed:", build_output)

        # 7. Emit patch or failure report
        if build_ok:
            diff = capture_diff(wt_path)
            patch_path = write_patch(repo, crash_hash, diff)
            cleanup_worktree_on_success(repo, wt_path)
            return FixResult(ok=True, patch=patch_path, cost=result.cost)
        else:
            failure_path = write_failure_report(repo, crash_hash, build_output, result)
            cleanup_worktree_on_failure(wt_path)  # leaves it
            return FixResult(ok=False, report=failure_path, cost=result.cost)

    finally:
        shutdown_server(server_proc)
```

### 7.2 Ctrl-C handling

Register a SIGINT handler that:
1. Sends `SIGTERM` to any running Claude Code subprocess.
2. Calls `shutdown_server(server_proc)` (which detaches pybag from the target).
3. **Does NOT remove the worktree** — user can inspect.
4. Exits non-zero with a summary.

```python
def install_signal_handlers(state: FixState):
    def on_interrupt(signum, frame):
        log("interrupted; cleaning up…")
        if state.claude_proc and state.claude_proc.poll() is None:
            state.claude_proc.terminate()
            try:
                state.claude_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                state.claude_proc.kill()
        shutdown_server(state.server_proc)
        sys.exit(130)
    signal.signal(signal.SIGINT, on_interrupt)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, on_interrupt)
```

### 7.3 Detaching pybag cleanly

**This is already handled by server shutdown.** When `debugbridge serve` exits, `DebugSession.__del__` / process exit tears down pybag's `UserDbg`, which calls `DetachProcesses()` implicitly. Proof from Phase 1's `e2e_smoke.py` comment: *"DbgEng holds the attached process; … we just stop the server (which detaches implicitly) and then kill."*

**If we didn't auto-spawn the server** (user already has one running), we must NOT stop it at the end. Instead, call a dedicated `detach` MCP tool before exiting — **but that tool doesn't exist yet**. Phase 2a note: add `detach_process` to the MCP surface if the user's workflow requires keeping the server up across fix runs. For the MVP, auto-spawn is the primary path.

### 7.4 Windows specifics

From the community report and Phase 1's e2e_smoke.py:

- **Use `CREATE_NEW_PROCESS_GROUP`** for `debugbridge serve` so we can send it `CTRL_BREAK_EVENT` cleanly. ✅ already in Phase 1 pattern.
- **Do NOT use `CREATE_NEW_PROCESS_GROUP` for `claude`** if we're piping stdin — community report says "breaks stdin piping." We're not piping stdin (using positional arg), so either way is fine. For simplicity, don't pass creationflags to the `claude` subprocess.
- **Always set `encoding="utf-8"`** on subprocess calls. Default `cp1252` mangles Unicode in Claude's output.
- **Command-line length:** keep positional arg short; rely on `@file` references.

---

## CLI Design

### 8.1 Shape of the `fix` subcommand

```python
@app.command()
def fix(
    pid: int = typer.Option(..., "--pid", help="PID of the attached (crashed) Windows process."),
    repo: Path = typer.Option(
        Path.cwd(),
        "--repo",
        help="Path to a git repository. Default: current directory.",
        exists=True, file_okay=False, dir_okay=True, resolve_path=True,
    ),
    conn_str: str | None = typer.Option(
        None, "--conn-str",
        help='Remote dbgsrv connection string, e.g. "tcp:server=192.168.1.10,port=5555".',
    ),
    build_cmd: str | None = typer.Option(
        None, "--build-cmd",
        help="Build command run inside the worktree after Claude Code's edit pass.",
    ),
    test_cmd: str | None = typer.Option(
        None, "--test-cmd",
        help="Test command run after successful build (--auto mode only).",
    ),
    auto: bool = typer.Option(
        False, "--auto",
        help="Autonomous mode: headless Claude Code + build validation + patch output. "
             "Default: hand-off to interactive Claude Code.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="MCP server host for auto-spawn."),
    port: int = typer.Option(8585, "--port", help="MCP server port for auto-spawn."),
    model: str = typer.Option("sonnet", "--model", help="Claude model alias or full ID."),
    max_attempts: int = typer.Option(3, "--max-attempts", help="Retry cap for --auto."),
    json_output: bool = typer.Option(
        False, "--json", help="Emit final result as JSON for scripting."
    ),
) -> None:
    """Capture a crash and either launch Claude Code interactively (default)
    or run it headlessly and produce a validated patch (--auto)."""
    # ... validation & dispatch
```

### 8.2 Rich output for long phases

```python
from rich.progress import Progress, SpinnerColumn, TextColumn

with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as p:
    t = p.add_task("Attaching to process…", total=None)
    capture = asyncio.run(capture_crash(pid, url, conn_str))
    p.update(t, description=f"Captured: {capture.exception.code} @ {capture.callstack[0].function}")

    t2 = p.add_task("Creating worktree…", total=None)
    wt = create_worktree(repo, hash_)
    p.update(t2, description=f"Worktree: {wt.relative_to(repo)}")

    if auto:
        t3 = p.add_task("Running Claude Code (this can take 30–90s)…", total=None)
        result = run_claude(...)
        p.update(t3, description=f"Done in {result.duration_ms/1000:.1f}s, {result.num_turns} turns")
```

### 8.3 Final summary block

Text (default):

```
[debugbridge] ✓ fix complete
  crash:    EXCEPTION_ACCESS_VIOLATION @ crash_app!crash_null+0x2a
  worktree: .debugbridge/wt-a1b2c3d4 (removed)
  patch:    .debugbridge/patches/crash-a1b2c3d4.patch
  build:    passed on attempt 1
  turns:    7
  tokens:   18.5K in / 2.1K out
  cost:     $0.18

  apply with: git apply .debugbridge/patches/crash-a1b2c3d4.patch
```

JSON (`--json` flag):

```json
{
  "ok": true,
  "crash": {
    "code": "EXCEPTION_ACCESS_VIOLATION",
    "symbol": "crash_app!crash_null+0x2a",
    "hash": "a1b2c3d4"
  },
  "patch": ".debugbridge/patches/crash-a1b2c3d4.patch",
  "attempts": 1,
  "turns": 7,
  "tokens": {"input": 18500, "output": 2100},
  "cost_usd": 0.18,
  "duration_ms": 42300
}
```

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Tool-calling loop around Anthropic API | A Python wrapper around `anthropic.messages.create` with manual tool dispatch | `claude -p` CLI subprocess | Claude Code already handles multi-turn tool loops, MCP tool routing, prompt caching, and retry. Reinventing = 200+ lines of fragile code. |
| JSON Schema validation of Claude Code output | Hand-written Pydantic model from CLI docs | Parse loosely with `.get()` defaults; add a `ClaudeResult` dataclass with only fields we use | Schema is documented but not strictly versioned; stay permissive. |
| Parsing git worktree output | Regex against `git worktree list` | `git worktree list --porcelain` + simple string parsing | `--porcelain` is stable by design, made for scripting. |
| Unified diff generation | Custom diff code | `git diff` command | We already have git. Diff is 40+ years mature. |
| MCP protocol client | Raw HTTP + JSON-RPC | `mcp.client.streamable_http.streamablehttp_client` | The official SDK handles session management, content type negotiation, SSE streaming, error propagation. Already a Phase 1 dep. |
| Windows subprocess lifecycle | Custom Win32 wrappers | `subprocess.Popen` + `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` | Phase 1's `e2e_smoke.py` already nailed this pattern. |
| Crash-type classification | A classifier model (tiered routing) | Defer to Phase 4 | Phase 2a uses single Sonnet calls; classification optimization is post-launch. |
| Process-on-remote-machine detection | Our own dbgsrv parsing | `conn_str` pass-through to `attach_process` | Phase 1 already handles remote attach. We don't add logic here. |
| Cost aggregation across attempts | Custom accumulator | Sum `total_cost_usd` from each attempt's JSON | Nothing to build. |

**Key insight:** The temptation in 2a is to "improve" Claude Code by wrapping parts of it. Resist. Every wrapper is a Claude Code feature we won't get for free (prompt caching, tool tracing, stream-json events, session resumption via `--resume`). Shell out to `claude -p` and trust its output.

---

## Common Pitfalls

### Pitfall 1: Breaking the Windows command-line length ceiling

**What goes wrong:** Passing a 30K-character briefing directly as the positional argument to `claude -p` on Windows fails silently — Claude runs on an empty prompt.

**Why it happens:** Windows has a ~8191-char `cmd.exe` limit; `CreateProcessW` accepts longer but with awkward semantics when arguments are quoted. Long PowerShell invocations can silently truncate.

**How to avoid:** Always write the briefing to `.debugbridge/briefing.md` and reference it via `@.debugbridge/briefing.md` in a short positional arg. Never put the briefing text in `argv`.

**Warning signs:** Claude Code returns a generic response like "I'd be happy to help — what would you like me to do?" despite a detailed crash.

### Pitfall 2: `--permission-mode bypassPermissions` first-run prompt

**What goes wrong:** The first time a user runs Claude Code with `--dangerously-skip-permissions`, it prompts interactively for confirmation. In a headless subprocess, this hangs until stdin EOF or timeout.

**Why it happens:** Claude Code's safety layer wants explicit acknowledgement before bypass mode.

**How to avoid:**
- `debugbridge doctor` runs `claude --dangerously-skip-permissions --help` once interactively to surface and acknowledge the warning.
- Alternatively, set `"skipDangerousModePermissionPrompt": true` in `~/.claude/settings.json` via `--settings` flag.

**Warning signs:** `claude -p` subprocess hangs ~30s then exits non-zero with no JSON output.

### Pitfall 3: Worktree left behind from a failed run

**What goes wrong:** A previous crashed attempt left `.debugbridge/wt-a1b2c3d4` on disk. Re-running `fix` on the same crash (same hash) fails at `git worktree add` because the path exists.

**Why it happens:** We leave worktrees on failure for inspection (by design).

**How to avoid:** Before `git worktree add`, detect existing worktree at the target path and either (a) auto-remove with `--force` if `--retry` is passed, or (b) abort with clear error message suggesting manual cleanup:

```
Error: worktree already exists at .debugbridge/wt-a1b2c3d4
This is from a previous failed attempt. Options:
  1. Inspect: cd .debugbridge/wt-a1b2c3d4
  2. Clean up: git worktree remove --force .debugbridge/wt-a1b2c3d4
  3. Retry with cleanup: debugbridge fix --retry-clean …
```

### Pitfall 4: MCP tool names without the `mcp__` prefix

**What goes wrong:** We pass `--allowedTools "attach_process,get_exception"` and Claude Code refuses to call them.

**Why it happens:** MCP tools are namespaced `mcp__<server>__<tool>` in the permission system. Without the prefix, the rule doesn't match.

**How to avoid:** Always use `mcp__debugbridge__<tool>` or the wildcard `mcp__debugbridge__*` in `--allowedTools`.

### Pitfall 5: `--output-format json` mixed with log noise

**What goes wrong:** We call `json.loads(stdout)` and it fails because there's an informational log line before the JSON.

**Why it happens:** Claude Code may print banner/warning lines on stderr (fine, we redirect) or occasionally on stdout (less common but happens with auth refresh messages).

**How to avoid:** Robust parse — try whole stdout first, fall back to last line, log the raw stdout on failure for debugging. Code in §6.

### Pitfall 6: Pybag session survives after fix exits

**What goes wrong:** `debugbridge fix` exits, but the target process is still paused/attached because the MCP server is still running (we didn't spawn it; it was already up).

**Why it happens:** We don't know if we spawned the server. If we didn't, we shouldn't stop it — but pybag's attach persists until something explicitly detaches.

**How to avoid:**
- If we spawned the server, stop it at exit (already does detach).
- If we didn't, add a `detach_process` MCP tool (Phase 2a work) and call it on exit.
- Document in README: "if DebugBridge server was already running, the target process remains attached after `fix` exits — use a client to call `continue_execution` or restart the server to detach."

### Pitfall 7: Pre-existing `~/.claude.json` MCP servers interfering

**What goes wrong:** User has a conflicting MCP server named `debugbridge` in `~/.claude.json`. Our `--mcp-config` is layered on top, but scope hierarchy is `local > project > user`, so there can be subtle override issues.

**How to avoid:** Add `--strict-mcp-config` flag to our `claude -p` invocation. Ignores all other MCP configs except the one we pass. Clean slate.

### Pitfall 8: UTF-8 encoding on Windows subprocess

**What goes wrong:** `subprocess.run([...], text=True)` uses `cp1252` by default on Windows; Claude Code may emit Unicode (em-dashes, smart quotes, non-ASCII file paths). Parsing blows up with `UnicodeDecodeError`.

**How to avoid:** Always pass `encoding="utf-8", errors="replace"` to subprocess calls reading Claude Code output. Also set the subprocess env: `env={"PYTHONIOENCODING": "utf-8", **os.environ}`.

---

## Code Examples

### Complete `run_claude` wrapper

```python
# debugbridge/fixer/claude_runner.py
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClaudeRunResult:
    ok: bool              # not is_error AND returncode == 0
    is_error: bool
    subtype: str | None
    result: str | None    # the final assistant message
    session_id: str | None
    total_cost_usd: float
    input_tokens: int
    output_tokens: int
    num_turns: int
    duration_ms: int
    raw_stdout: str
    raw_stderr: str
    returncode: int


def run_claude(
    cwd: Path,
    briefing_path: Path,
    mcp_config_path: Path,
    system_append_path: Path,
    model: str = "sonnet",
    max_turns: int = 20,
    max_budget_usd: float = 0.75,
    extra_allowed_bash: list[str] | None = None,
) -> ClaudeRunResult:
    """Run `claude -p` headlessly and parse its JSON output."""
    allowed = [
        "Read", "Edit", "Write", "Glob", "Grep",
        "mcp__debugbridge__*",
    ]
    for pattern in (extra_allowed_bash or []):
        allowed.append(f"Bash({pattern})")

    briefing_rel = briefing_path.relative_to(cwd).as_posix()
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", ",".join(allowed),
        "--max-turns", str(max_turns),
        "--max-budget-usd", f"{max_budget_usd:.2f}",
        "--model", model,
        "--append-system-prompt-file", str(system_append_path),
        f"Read @{briefing_rel} and produce the minimal fix.",
    ]

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=600,  # 10 min hard ceiling per attempt
    )

    parsed = _parse_claude_json(proc.stdout)
    return ClaudeRunResult(
        ok=(proc.returncode == 0 and not parsed.get("is_error", False)),
        is_error=parsed.get("is_error", proc.returncode != 0),
        subtype=parsed.get("subtype"),
        result=parsed.get("result"),
        session_id=parsed.get("session_id"),
        total_cost_usd=float(parsed.get("total_cost_usd", 0.0)),
        input_tokens=(
            parsed.get("usage", {}).get("input_tokens", 0)
            + parsed.get("usage", {}).get("cache_read_input_tokens", 0)
        ),
        output_tokens=parsed.get("usage", {}).get("output_tokens", 0),
        num_turns=parsed.get("num_turns", 0),
        duration_ms=parsed.get("duration_ms", 0),
        raw_stdout=proc.stdout,
        raw_stderr=proc.stderr,
        returncode=proc.returncode,
    )


def _parse_claude_json(stdout: str) -> dict:
    stripped = stdout.strip()
    if not stripped:
        return {"is_error": True, "subtype": "empty_output"}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"is_error": True, "subtype": "unparseable_output", "_raw": stripped[:500]}
```

### Briefing generator sketch

```python
# debugbridge/fixer/briefing.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

from debugbridge.fixer.models import CrashCapture


def render_briefing(capture: CrashCapture, source_snippets: dict[Path, str]) -> str:
    exc = capture.exception
    frame0 = capture.callstack[0] if capture.callstack else None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    out: list[str] = []
    out.append(f"# Crash briefing — {capture.crash_hash}")
    out.append("")
    out.append(f"**Generated:** {now}")
    out.append(f"**PID:** {capture.pid}")
    if capture.binary_path:
        out.append(f"**Target:** `{capture.binary_path}`")
    out.append("")
    out.append("## Exception")
    out.append("")
    out.append(f"- **Code:** {exc.code_name} ({exc.code_hex})")
    out.append(f"- **Address:** {exc.address}")
    if frame0:
        out.append(f"- **Module!Symbol:** `{frame0.module}!{frame0.function}`")
        if frame0.file and frame0.line:
            out.append(f"- **Source:** `{frame0.file}:{frame0.line}`")
    out.append("")

    out.append("## Call stack (innermost first)")
    out.append("")
    out.append("| # | Function | Module | Source |")
    out.append("|---|----------|--------|--------|")
    for i, f in enumerate(capture.callstack[:16]):
        src = f"`{f.file}:{f.line}`" if f.file else "—"
        out.append(f"| {i} | `{f.function}` | {f.module} | {src} |")
    out.append("")

    out.append("## Locals at frame 0")
    out.append("")
    out.append("| Name | Type | Value |")
    out.append("|------|------|-------|")
    for v in capture.locals_[:20]:
        out.append(f"| `{v.name}` | `{v.type}` | `{v.value}` |")
    out.append("")

    out.append("## Source context")
    out.append("")
    for path, snippet in source_snippets.items():
        ext = path.suffix.lstrip(".") or "text"
        out.append(f"### `{path}`")
        out.append("")
        out.append(f"```{ext}")
        out.append(snippet.rstrip())
        out.append("```")
        out.append("")

    out.append(dedent("""\
        ## Your task

        A native Windows program crashed with the access violation above. The
        root cause is almost certainly in the top call-stack frame.

        1. Read the source files referenced above.
        2. Propose a minimal fix that addresses the crash without changing
           unrelated behavior.
        3. Apply the edit with the Edit tool.
        4. If a build command was provided, run it via Bash after editing.
        5. If the build fails, investigate and iterate.
        6. When the build succeeds, stop and summarize the fix in one paragraph.

        ## Constraints

        - Only edit files listed in the Source context section.
        - Do not modify test files or build configuration.
        - Do not add dependencies.
        - Do not modify `.git`, `.debugbridge`, or any configuration files.
        - If unsure, prefer the minimal diff over a "better" refactor.

        ## Available MCP tools

        You have access to a live debugger attached to the crashed process via
        the `debugbridge` MCP server. If you need more information, call:

        - `mcp__debugbridge__get_exception` — full exception record
        - `mcp__debugbridge__get_callstack` — deeper stack (up to 64 frames)
        - `mcp__debugbridge__get_locals` — locals at any frame (pass `frame_index`)
        - `mcp__debugbridge__get_threads` — all threads

        Do NOT call `set_breakpoint`, `step_next`, or `continue_execution` —
        those would resume or interfere with the target.
        """))
    return "\n".join(out)


def extract_source_snippets(
    repo: Path, callstack: list, context_lines: int = 15, max_files: int = 5
) -> dict[Path, str]:
    """Pull ±context_lines around each stack frame's source line.

    Deduplicates by file: if two frames reference the same file, merge into a
    single wider snippet.
    """
    ranges: dict[Path, list[tuple[int, int]]] = {}
    for f in callstack:
        if not (f.file and f.line):
            continue
        # Relative to repo root; skip frames pointing outside the repo.
        try:
            src = Path(f.file)
            if not src.is_absolute():
                src = repo / src
            src = src.resolve()
            if not src.is_relative_to(repo.resolve()):
                continue
            if not src.exists():
                continue
            lo = max(1, f.line - context_lines)
            hi = f.line + context_lines
            ranges.setdefault(src, []).append((lo, hi))
        except (ValueError, OSError):
            continue
        if len(ranges) >= max_files:
            break

    out: dict[Path, str] = {}
    for src, chunks in ranges.items():
        # Merge overlapping ranges
        chunks.sort()
        merged: list[tuple[int, int]] = []
        for lo, hi in chunks:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        lines = src.read_text(encoding="utf-8", errors="replace").splitlines()
        buf: list[str] = []
        for lo, hi in merged:
            buf.append(f"// lines {lo}-{min(hi, len(lines))}")
            buf.extend(lines[lo - 1 : hi])
            buf.append("")
        out[src.relative_to(repo)] = "\n".join(buf)
    return out
```

### Full hand-off flow sketch

```python
# debugbridge/fixer/handoff.py
import os
import subprocess
import sys
from pathlib import Path

def run_handoff(repo: Path, briefing: Path, mcp_config: Path) -> int:
    """Exec (replace process) into interactive claude with the briefing queued.
    Returns exit code of claude session."""
    cmd = [
        "claude",
        "--mcp-config", str(mcp_config),
        "--strict-mcp-config",
        f"I've just captured a crash. Read @{briefing.relative_to(repo).as_posix()} and propose a fix.",
    ]
    if os.name == "nt":
        # Windows: can't exec cleanly; use subprocess.run to share the TTY.
        return subprocess.run(cmd, cwd=str(repo)).returncode
    else:
        os.execvp("claude", cmd)  # no return on success
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| "SDK-only headless mode" | `claude -p` CLI flag | Consolidated in 2025 agent-SDK docs | All CLI flags work with `-p`; docs note *"The CLI was previously called 'headless mode.' The `-p` flag and all CLI options work the same way."* |
| MCP SSE transport | Streamable HTTP | April 2026 (`"transport": "sse"` deprecated) | Use `"type": "http"` with `/mcp` URL path. Our Phase 1 server already does this. |
| Claude Code SDK | Claude Agent SDK (rebranded) | Renamed 2025-2026 | Python package is `claude-agent-sdk` (not `claude-code-sdk`). **Does not work cleanly on Windows + Python 3.12** — subprocess CLI is preferred. |
| Hand-written tool loops on `anthropic.messages.create` | `claude -p` or Agent SDK | 2024-2025 | Don't hand-roll the tool loop. Use Claude Code. |

**Deprecated / outdated to avoid:**

- SSE MCP transport (use Streamable HTTP).
- `claude-code-sdk` (renamed to `claude-agent-sdk`, but we're not using it anyway).
- `Bash(ls*)` without space — prefer `Bash(ls *)` for word boundary.
- `--permission-mode plan` in autonomous flow — it's read-only, Claude can't edit.

---

## Open Questions

### 1. Does `--max-budget-usd` count MCP-tool-input tokens?

**What we know:** Claude Code's cost tracking rolls up all model API calls, including the prompts that include MCP tool results. MCP tool *execution* (running our Python code) costs $0 — that's local.

**What's unclear:** If we return a 30K-token tool result from `get_callstack`, that gets injected into the next turn's input. Is that billed? Almost certainly yes (they're model-context input tokens). Precise behavior around `MAX_MCP_OUTPUT_TOKENS` threshold isn't documented.

**Recommendation for 2a:** Assume all tokens are billed (conservative). Set `--max-budget-usd 0.75` per attempt, which comfortably covers 3 × Sonnet turns with 25K input each. Validate with real data after integration test.

### 2. Does `--dangerously-skip-permissions` truly skip everything in headless?

**What we know:** Docs state that "protected paths" (`.git`, `.claude`, etc.) still prompt. In headless with bypass, a blocked write should fail the tool call — but we don't know if it converts to a silent deny or if it surfaces as `is_error: true`.

**Recommendation:** Integration test with a crash where Claude would be tempted to `.gitignore` something. Observe behavior.

### 3. What exactly is the `subtype` enum on error?

**What we know:** `"error_max_turns"` was mentioned in community examples. Docs list `subtype` fields for `system/api_retry` events in stream-json (`authentication_failed`, `billing_error`, `rate_limit`, `invalid_request`, `server_error`, `max_output_tokens`, `unknown`). Result-level `subtype` likely uses similar values.

**Recommendation:** Don't over-match. Treat any `is_error: true` as failure; log `subtype` as a string; only retry on retryable ones (build-failure feedback, NOT `authentication_failed`).

### 4. Will Claude Code attempt to use non-allowed tools and then abort?

**What we know:** In `bypassPermissions`, any tool call runs. In default mode with `--allowedTools`, non-allowed tools cause a prompt (silent deny in `-p`). It's not documented whether Claude retries with a different tool or gives up.

**Recommendation:** Use `bypassPermissions` and rely on `--allowedTools` being permissive within reason. If we ship with `dontAsk` mode later (stricter), we can measure this.

### 5. Session resumption across attempts — worth it?

**What we know:** We capture `session_id` from attempt 1's output. `claude -p --resume <id> "Build failed, try again: <build output>"` can continue with full prior context.

**Recommendation for 2a:** Use `--resume` across retries within a single `fix` invocation. Saves tokens on re-reading the briefing + source files. Set this as a stretch goal — the naive approach (fresh call with "retry + last build output appended to briefing") is simpler to implement.

---

## Sources

### Primary (HIGH confidence)

- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) — full flag list, headless options, system prompt flags
- [Claude Code headless guide](https://code.claude.com/docs/en/headless) — `-p` examples, output formats, bare mode, stream-json event types
- [Claude Code MCP docs](https://code.claude.com/docs/en/mcp) — `--mcp-config` schema, scopes, Windows `cmd /c` quirk, streamable HTTP transport
- [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes) — bypassPermissions, dontAsk, protected paths
- [Claude Code permissions / rule syntax](https://code.claude.com/docs/en/permissions) — `mcp__<server>__<tool>` naming, Bash/Read/Edit rules, gitignore-style patterns
- [Claude Code settings](https://code.claude.com/docs/en/settings) — settings file hierarchy, `skipDangerousModePermissionPrompt`
- [Claude Code common workflows](https://code.claude.com/docs/en/common-workflows) — `claude "query"` initial prompt pattern, `--worktree` flag, `@file` references, pipe stdin
- [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk) — subprocess CLI vs Python SDK tradeoffs
- [git-worktree(1)](https://git-scm.com/docs/git-worktree) — canonical worktree commands
- Existing code: `scripts/e2e_smoke.py`, `src/debugbridge/server.py`, `src/debugbridge/tools.py`, `src/debugbridge/cli.py` — the in-repo pattern for MCP client + subprocess orchestration

### Secondary (MEDIUM confidence — verified with official docs)

- [Using Claude Code Programmatically (Jacob F gist)](https://gist.github.com/JacobFV/2c4a75bc6a835d2c1f6c863cfcbdfa5a) — complete JSON output schema example, stream-json event shape. Fields match official docs; specific value examples used here are illustrative.
- [Running Claude Code from Windows CLI (dstreefkerk)](https://dstreefkerk.github.io/2026-01-running-claude-code-from-windows-cli/) — Windows subprocess pitfalls (cp1252 encoding, cmd-line length, `claude-agent-sdk` + Python 3.12 issue, `CREATE_NEW_PROCESS_GROUP` + stdin conflict). Consistent with Phase 1's own subprocess patterns.

### Tertiary (LOW confidence — flagged for validation)

- Token-budget estimates (5–8K briefing, 15–25K total input, $0.05–0.15/attempt) — first-order based on typical C++ crash size + Sonnet 4.6 pricing. **Validate empirically in the first integration test run.**
- Exact list of `result.subtype` error values — community-reported `error_max_turns`, not exhaustively documented.

---

## Metadata

**Confidence breakdown:**
- Claude Code headless mechanics: HIGH — full CLI reference + headless guide + SDK docs
- MCP client patterns: HIGH — Phase 1 already exercises the exact pattern
- Git worktree mechanics: HIGH — upstream Git docs, mature feature
- Windows subprocess quirks: MEDIUM — community report + Phase 1 code agreement, but no single authoritative source
- Briefing structure / prompt engineering: MEDIUM — based on general LLM best practices, not formally measured for our specific crash-fix task
- Token-budget estimates: LOW — first-order guess; needs empirical calibration
- Exact error subtypes: LOW — only partially documented

**Research date:** 2026-04-15
**Valid until:** 2026-07-15 (3 months — Claude Code releases every 1–2 weeks; re-verify MCP config schema, flag additions, and JSON schema if work on 2a extends beyond the phase exit)
