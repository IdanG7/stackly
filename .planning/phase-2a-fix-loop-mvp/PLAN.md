# Phase 2a — Fix-loop MVP — Executable Plan

**Plan date:** 2026-04-15
**Phase goal (one sentence):** A developer runs `debugbridge fix --pid N --repo PATH` on a live crashed Windows process and gets back either an interactive Claude Code session preloaded with crash context, or (with `--auto`) a validated `.patch` file — without DebugBridge ever touching the developer's main working tree.
**Source of truth:** `GOAL.md` (acceptance criteria), `RESEARCH.md` (implementation sketches), `phase-1-tech.md` (inherited plumbing).

---

## 1. Context

Phase 1 shipped a Streamable-HTTP MCP server exposing 8 tools that read DbgEng debugger state from a live Windows process. Phase 2a adds the first *consumer* of that server — a `fix` CLI command that (a) spawns the server if not running, (b) captures crash state via MCP, (c) writes a Markdown briefing, and (d) either hands the briefing off to an interactive `claude` session (default) or drives a headless `claude -p` subprocess against an isolated git worktree until a user-provided build command passes, at which point it emits a `.patch` file. Everything writes to `$REPO/.debugbridge/`; the user's main tree is never modified.

Explicitly deferred: crash auto-detection (Phase 2.5), PyPI publish (2c), public launch (2b), non-Windows adapters (Phase 3). Also deferred within 2a itself: tiered Haiku→Sonnet→Opus routing (Phase 4), prompt-cache optimization, Windows Job Object sandboxing, session resumption via `claude --resume` (optional stretch — see Task 2a.3.10).

## 2. Architecture decisions (pinned)

These are locked for Phase 2a. Changing any of them requires updating this plan first.

1. **Agent speaks MCP to our own server (dog-fooding).** The `fix` command auto-spawns `debugbridge serve` if not listening, then connects as an MCP client exactly like `scripts/e2e_smoke.py` does. No `from debugbridge.session import DebugSession` in the fix code path. Proves the MCP surface before customers exercise it.
2. **Claude Code via CLI subprocess, not the Python SDK.** The `claude-agent-sdk` package is reported to crash on Windows + Python 3.12 (RESEARCH.md §Alternatives). Shell out to `claude` / `claude -p` and parse `--output-format json`.
3. **Git worktree isolation.** Autonomous mode creates `.debugbridge/wt-<hash>/` via `git worktree add -b debugbridge/fix-<hash>`. Claude Code is launched with `cwd=worktree_path`. User's main tree is untouched even when dirty.
4. **Hand-off + autonomous share the capture pipeline.** Both modes go through `capture_crash()` → `render_briefing()` → `write_mcp_config()`. They only diverge at the dispatch step.
5. **User-provided `--build-cmd` / `--test-cmd`.** No auto-detection in v1 (deliberately boring — matches GOAL.md criterion 5). These commands run inside the worktree; stdout+stderr are captured for retry feedback.
6. **Repo defaults to cwd with `--repo` override.** Typer resolves to `Path.cwd()` when `--repo` is omitted.
7. **All writes inside `$REPO/.debugbridge/`.** Subdirs: `wt-<hash>/` (worktrees, removed on success), `patches/` (kept), `briefings/` (kept), `mcp-config.json`, `system-append.md`. On first use `.debugbridge/` is added to the repo's `.gitignore`.
8. **Hard 3-attempt retry cap.** Autonomous mode: attempt 1 → build → if fail, attempt 2 with build output appended to briefing → build → if fail, attempt 3 → if still failing, emit `.failed.md` and exit non-zero.

### Critical gap surfaced by RESEARCH.md

RESEARCH.md §7.3 identifies that our current server lacks a way for a client to explicitly detach pybag from the target process without killing the server. Today's cleanup only works because `ensure_server_running()` spawns the server itself (so shutting the server down detaches pybag implicitly). If the user already had `debugbridge serve` running, `fix` has no way to cleanly release the target on exit — pybag stays attached, the target stays paused.

**Task 2a.0.1 adds a `detach_process` MCP tool** to close this gap. Because the tool is new (not a signature change to existing tools), it is compatible with GOAL.md's constraint "No change to the existing 8 MCP tools' public signatures."

## 3. Component breakdown

All production code lives under `src/debugbridge/fix/` (new subpackage). This keeps Phase 1's surface area untouched and makes it trivial to review Phase 2a in isolation.

```
src/debugbridge/fix/
├── __init__.py           # re-exports for CLI wiring only
├── models.py             # CrashCapture, BriefingInputs, ClaudeRunResult, FixResult
├── mcp_client.py         # auto-spawn debugbridge serve + capture crash via MCP
├── briefing.py           # Markdown generator + source-snippet extractor
├── worktree.py           # git worktree add/remove/diff + crash-hash + gitignore
├── claude_runner.py      # subprocess wrappers for `claude` (interactive) and `claude -p` (headless)
├── build_runner.py       # run --build-cmd / --test-cmd inside worktree, capture output
├── patch_writer.py       # diff-to-file + .failed.md writer
├── dispatcher.py         # handoff vs autonomous control flow + retry loop + signal handlers
└── doctor.py             # `debugbridge doctor` extensions for claude CLI / bypass-permission prompt
```

Component-by-component responsibility:

1. **MCP client auto-spawn + capture** (`mcp_client.py`): Checks whether a server is listening on `--host`/`--port` via a TCP-probe (`socket.create_connection`). If not, spawns `uv run debugbridge serve` with `CREATE_NEW_PROCESS_GROUP` and waits for the `"Uvicorn running"` stdout line (pattern stolen from `scripts/e2e_smoke.py:52–62`). Opens a `streamablehttp_client` + `ClientSession`, issues `attach_process`, `get_exception`, `get_callstack(max_frames=32)`, `get_threads`, `get_locals(frame_index=0)`, folds results into a `CrashCapture` Pydantic model. On exit, if we spawned the server, sends `CTRL_BREAK_EVENT`; if we didn't, calls `mcp__debugbridge__detach_process`. Exposes `ensure_server_running()`, `capture_crash()`, `shutdown_server_or_detach()`.

2. **Briefing generator** (`briefing.py`): Pure function `render_briefing(capture: CrashCapture, snippets: dict[Path, str], build_cmd: str | None) -> str`. Emits Markdown with the RESEARCH.md §5.1 template: Exception, Call stack (innermost 16 frames), Locals at frame 0, Source context (±15 lines × up to 5 files, dedup overlapping), Your task (numbered), Constraints, Available MCP tools. Source-snippet extractor walks the stack, resolves each `file` to a repo-relative path, skips frames pointing outside the repo (stdlib / kernel) — if there are no in-repo frames, still emits a "Source context: no in-repo frames resolved" placeholder rather than failing.

3. **Worktree manager** (`worktree.py`): Wraps `git worktree add/remove/list`, `git rev-parse`, `git diff`. Functions: `is_git_repo(path)`, `detect_dirty(path)`, `ensure_gitignore(path)`, `compute_crash_hash(capture) -> str`, `create_worktree(repo, crash_hash) -> Path`, `capture_diff(worktree) -> str`, `cleanup_worktree_on_success()`, `cleanup_worktree_on_failure()`. Hash is `hashlib.sha1(f"{exc.code_name}@{frame0.module}!{frame0.function}".encode()).hexdigest()[:8]` — same crash → same hash → re-runs reuse the same patch filename.

4. **Claude Code subprocess wrapper** (`claude_runner.py`): Two entry points. `run_claude_headless(...)` executes the command documented in RESEARCH.md §1.1, captures stdout, parses the last JSON object, returns a `ClaudeRunResult` dataclass with `ok`, `is_error`, `subtype`, `result`, `session_id`, `total_cost_usd`, `input_tokens`, `output_tokens`, `num_turns`, `duration_ms`, `raw_stdout`, `raw_stderr`, `returncode`. `run_claude_interactive(...)` inherits TTY via `subprocess.run([...], cwd=repo)` — on Windows we do not `os.execvp` because the terminal hand-off is cleaner via `run()`. Writes `.debugbridge/mcp-config.json` + `.debugbridge/system-append.md` lazily via helpers.

5. **Build/test runner** (`build_runner.py`): `run_command(cmd: str, cwd: Path, timeout: int) -> tuple[bool, str]`. Splits with `shlex.split(cmd, posix=False)` (Windows `cmd.exe` quoting) — or if that's fragile, shell out via `subprocess.run(cmd, shell=True, ...)` with `text=True, encoding="utf-8", errors="replace"`. Captures stdout+stderr merged, returns `(returncode == 0, combined_output)`. Timeout default 10 min; on timeout, treats as failure and returns stderr `"<command timed out after Ns>"`. No library needed.

6. **Diff-to-patch writer** (`patch_writer.py`): `write_patch(repo, crash_hash, diff) -> Path` writes to `.debugbridge/patches/crash-<hash>.patch`. `write_failure_report(repo, crash_hash, ctx) -> Path` writes `.debugbridge/patches/crash-<hash>.failed.md` with attempt history, last build output, Claude Code's `result` text per attempt, cost totals.

7. **Hand-off vs autonomous dispatcher** (`dispatcher.py`): Top-level `fix_command(args)` invoked by the CLI. Validates inputs (git repo check, claude-on-PATH check via `shutil.which`, Windows platform check), orchestrates server → capture → briefing → dispatch on `--auto`. In `--auto` mode: creates worktree, runs attempt loop (up to `--max-attempts`), runs build, feeds failure back to briefing, emits patch or failure report. In hand-off mode: writes briefing + mcp-config in `.debugbridge/`, calls `run_claude_interactive()`. Installs `SIGINT`/`SIGBREAK` handlers that terminate the child Claude process (if any), detach pybag, and leave the worktree for inspection.

## 4. Atomic task list (the executable part)

Task-size legend: **XS** ≤ 10 min, **S** 10–30 min, **M** 30–60 min (flagged for splitting if reached).

Global note on tests: all new integration tests use `@pytest.mark.integration` and rely on `tests/conftest.py`'s existing auto-skip gate (Phase 1 pattern). New unit tests go in `tests/test_fix_*.py` and do NOT import pybag, MCP, or `claude`.

---

### Phase 2a.0 — Prep (server-side gaps first)

#### Task 2a.0.1 — Add `detach_process` MCP tool

- **Files:** `src/debugbridge/session.py` (add `detach()` method), `src/debugbridge/tools.py` (add `detach_process` tool), `tests/test_session_integration.py` (new test).
- **Failing test to write first:** `tests/test_session_integration.py::test_detach_process_releases_target` — attach to a waiting `crash_app`, assert `session._dbg is not None`, call `session.detach()`, assert `session._dbg is None` and a subsequent `get_callstack()` raises `DebugSessionError("Not attached…")`.
- **Action:** `DebugSession.detach()` is a thin public wrapper that takes the lock and calls `_close_locked()`. The MCP tool `detach_process() -> None` calls it. Matches the naming of `attach_process` (already a tool).
- **Acceptance criteria:**
  - `session.call_tool("detach_process", {})` succeeds after a successful `attach_process`.
  - After `detach_process`, subsequent tool calls like `get_callstack` return an MCP error (DebugSessionError wrapped).
  - Re-attaching after detach works.
  - Integration test passes locally; auto-skips in CI.
  - `scripts/e2e_smoke.py`'s `expected` tool-set is updated to 9 tools (add `"detach_process"`).
- **Size:** S
- **Dependencies:** none (pure Phase 1 extension).

#### Task 2a.0.2 — Update `debugbridge doctor` to check for `claude` CLI and acknowledge bypass-permission prompt

- **Files:** `src/debugbridge/cli.py` (extend `doctor` command), `src/debugbridge/env.py` (add `check_claude_cli()` and `check_claude_bypass_acknowledged()`), `tests/test_env.py` (new unit test using `shutil.which` monkeypatch).
- **Failing test to write first:** `tests/test_env.py::test_check_claude_cli_reports_missing` — monkeypatch `shutil.which("claude")` to return `None`; assert `check_claude_cli().ok is False` and `check_claude_cli().guidance` mentions `https://docs.claude.com/en/docs/claude-code/getting-started`.
- **Action:** `check_claude_cli()` returns an `EnvCheckResult`-shaped dataclass using `shutil.which("claude")`. `check_claude_bypass_acknowledged()` reads `~/.claude/settings.json` (when it exists) and returns `ok=True` if `"skipDangerousModePermissionPrompt": true`, `ok=False` otherwise with guidance `"Run once interactively: claude --dangerously-skip-permissions --help"`. The `doctor` command's Rich table grows two rows: `claude CLI`, `claude bypass ack'd`.
- **Acceptance criteria:**
  - `debugbridge doctor` output includes a "claude CLI" row with status `found`/`missing`.
  - `debugbridge doctor` exit code remains 0 only if BOTH Windows Debugging Tools AND claude CLI are present (bypass-ack is a warning, not a hard fail).
  - Unit test with mocked `shutil.which` passes.
  - No new deps.
- **Size:** S
- **Dependencies:** none.

#### Task 2a.0.3 — Extend Pydantic models for the fix subpackage

- **Files:** `src/debugbridge/fix/__init__.py` (empty re-export stub), `src/debugbridge/fix/models.py` (new), `tests/test_fix_models.py` (new).
- **Failing test to write first:** `tests/test_fix_models.py::test_crash_capture_round_trip` — build a `CrashCapture` with mock Pydantic-compatible dicts for exception/callstack/threads/locals, `.model_dump()` then `.model_validate()`, assert round-trips identically.
- **Action:** Define four Pydantic v2 models in `fix/models.py`:
  - `CrashCapture`: `pid: int`, `process_name: str | None`, `binary_path: str | None`, `exception: ExceptionInfo | None`, `callstack: list[CallFrame]`, `threads: list[ThreadInfo]`, `locals_: list[Local]` (note trailing underscore; `locals` is a builtin), `crash_hash: str` (post-capture-computed).
  - `ClaudeRunResult`: dataclass-like Pydantic model with the fields documented in RESEARCH.md §6 code sketch.
  - `AttemptRecord`: `attempt: int`, `claude_result: ClaudeRunResult`, `build_ok: bool`, `build_output: str`, `test_ok: bool | None = None` (None means test_cmd not run), `test_output: str | None = None`, `duration_s: float`. Schema is final at 2a.0.3 — later tasks use the fields but do not amend the model.
  - `FixResult`: `ok: bool`, `mode: Literal["handoff", "auto"]`, `crash_hash: str`, `patch_path: Path | None`, `failure_report_path: Path | None`, `attempts: list[AttemptRecord]`, `total_cost_usd: float`, `total_input_tokens: int`, `total_output_tokens: int`, `worktree_path: Path | None`, `worktree_preserved: bool`.
  - Re-export the existing `ExceptionInfo`, `CallFrame`, `ThreadInfo`, `Local` from `debugbridge.models` so `fix.models` is the single import site for fix-subpackage code.
- **Acceptance criteria:**
  - All four models validate and round-trip.
  - `fix/__init__.py` exists and is importable without pulling pybag, MCP, or `claude` (pure Python).
  - Ruff + pyright clean.
- **Size:** S
- **Dependencies:** none.

---

### Phase 2a.1 — Capture (MCP client + briefing)

#### Task 2a.1.1 — Auto-spawn `debugbridge serve` with readiness detection

- **Files:** `src/debugbridge/fix/mcp_client.py` (new — server-management half only), `tests/test_fix_mcp_client.py` (new).
- **Failing test to write first:** `tests/test_fix_mcp_client.py::test_ensure_server_running_detects_existing` — bind a dummy socket to `127.0.0.1:8585`, call `ensure_server_running("127.0.0.1", 8585)`, assert it returns `None` (we did not spawn), assert no subprocess was created.
- **Action:** Implement `ensure_server_running(host, port) -> subprocess.Popen | None` and `shutdown_server(proc)` following RESEARCH.md §3.4–3.5 code sketches verbatim. TCP-probe first (`socket.create_connection(timeout=0.5)`), then `subprocess.Popen(["uv", "run", "debugbridge", "serve", "--host", host, "--port", str(port)], creationflags=CREATE_NEW_PROCESS_GROUP, stdout=PIPE, stderr=STDOUT, text=True, bufsize=1, encoding="utf-8", errors="replace")`, line-by-line readiness scan for `"Uvicorn running"` with 30s deadline, raise `TimeoutError` on miss. Shutdown: `CTRL_BREAK_EVENT` on Windows + 5s `wait()` + `kill()` fallback.
- **Acceptance criteria:**
  - TCP-probe-detection test passes without spawning any subprocess.
  - Second unit test (`test_ensure_server_running_timeout`) uses a mocked `Popen` that never emits `"Uvicorn running"` and asserts `TimeoutError` after a shortened deadline.
  - No `subprocess.Popen` is called when a server is already listening.
  - UTF-8 encoding used everywhere; no `cp1252`.
- **Size:** S
- **Dependencies:** none.

#### Task 2a.1.2 — Capture crash via MCP (`capture_crash` async function)

- **Files:** `src/debugbridge/fix/mcp_client.py` (extend), `tests/test_fix_mcp_client.py` (extend).
- **Failing test to write first:** `tests/test_fix_mcp_client.py::test_capture_crash_end_to_end` (marked `@pytest.mark.integration`) — fixture: spawn `crash_app wait`, spawn server, call `capture_crash(pid, "http://127.0.0.1:8585/mcp")`, assert `capture.pid == pid`, `capture.callstack` has ≥ 1 frame, `capture.threads` has ≥ 1 thread, `capture.crash_hash` is an 8-char lowercase hex string.
- **Action:** Follow RESEARCH.md §3.3 Option A. Async function opens `streamablehttp_client`, creates `ClientSession`, calls `initialize()`. **Immediately after `initialize()`, call `list_tools` and assert the result contains at least `attach_process` and `detach_process`; if not, raise `DebugSessionError("Port {port} is bound by a non-DebugBridge MCP server; pass --port N or stop the other process")` — this closes risk R9.** Then call `attach_process`, `get_exception`, `get_callstack(max_frames=32)`, `get_threads`, `get_locals(frame_index=0)`. Builds and returns a `CrashCapture` from `.structuredContent` fields. Sync wrapper `capture_crash(pid, mcp_url, conn_str=None) -> CrashCapture` uses `asyncio.run(_capture_async(...))`. **Crash-hash computation lives in `worktree.py` (task 2a.3.1), not here — import it via `from debugbridge.fix.worktree import compute_crash_hash` and call after the MCP capture completes.** Defaults to `"unknown"` for either side of the hash formula if the capture is degenerate.
- **Acceptance criteria:**
  - For a `wait`-mode (non-crashed) process, `capture.exception` is `None` and `crash_hash` is derived from the stack's frame 0 alone (`"{module}!{function}"` hash) so re-runs still stabilize.
  - For a `null`-mode crashed process, `capture.exception.code_name == "EXCEPTION_ACCESS_VIOLATION"`.
  - Integration test passes locally; auto-skips in CI.
  - No direct `from debugbridge.session import DebugSession` import in this module.
  - **Tool-presence check:** unit test that mocks `list_tools` to return only `["ping"]` asserts `capture_crash` raises `DebugSessionError` with message containing `"non-DebugBridge"` (R9 mitigation).
- **Size:** M — flagged as a candidate to split if it goes over 60 min; reasonable split is (a) async capture function, (b) tool-presence check + sync wrapper. But the pieces are small individually and tightly coupled; keep as one unless blocked.
- **Dependencies:** 2a.0.3 (needs `CrashCapture`), 2a.1.1 (uses `ensure_server_running` from same module), 2a.3.1 (imports `compute_crash_hash` from `worktree.py`).

#### Task 2a.1.3 — Source-snippet extractor

- **Files:** `src/debugbridge/fix/briefing.py` (new), `tests/test_fix_briefing.py` (new).
- **Failing test to write first:** `tests/test_fix_briefing.py::test_extract_source_snippets_merges_overlapping_ranges` — build a fake `callstack` with two frames at `crash.cpp:40` and `crash.cpp:45` and context=10; assert exactly one dict entry, spanning lines 30–55 (merged), with correct line content.
- **Action:** Implement `extract_source_snippets(repo: Path, callstack: list[CallFrame], context_lines: int = 15, max_files: int = 5) -> dict[Path, str]` per RESEARCH.md §5.5 code sketch. Key behaviors: skip frames with `file is None` or `line is None`; resolve absolute → `repo.resolve()`-relative; skip files outside the repo via `Path.is_relative_to`; skip nonexistent files; merge overlapping ranges; cap at `max_files` distinct files. Read files with `encoding="utf-8", errors="replace"`. Each snippet string starts with a `// lines LO-HI` comment line (neutral across languages — C/C++/C# line comments render harmlessly in other langs).
- **Acceptance criteria:**
  - Merge test passes (two overlapping ranges → one merged range).
  - Non-overlapping ranges test passes (two separate ranges → two `// lines` blocks in one file's snippet).
  - Frame pointing outside repo is silently dropped.
  - Non-existent file is silently dropped.
  - Result stays under `max_files` even when stack has 10+ frames.
- **Size:** S
- **Dependencies:** 2a.0.3 (uses `CallFrame`).

#### Task 2a.1.4 — Briefing Markdown renderer

- **Files:** `src/debugbridge/fix/briefing.py` (extend), `tests/test_fix_briefing.py` (extend).
- **Failing test to write first:** `tests/test_fix_briefing.py::test_render_briefing_includes_all_sections` — construct a `CrashCapture` with exception, 3-frame stack, 2 locals, 1 snippet; call `render_briefing(capture, snippets, build_cmd="cmake --build build")`; assert output contains, in order: `"# Crash briefing — "`, `"## Exception"`, `"## Call stack"`, `"## Locals at frame 0"`, `"## Source context"`, `"## Your task"`, `"## Constraints"`, `"## Available MCP tools"`, and that the build command appears verbatim under "Your task". Also assert no literal `None` string slips into the rendered output.
- **Action:** Port RESEARCH.md §5.5 `render_briefing` code sketch verbatim (it's ~90 lines, already documented). Small deltas:
  - When `capture.exception is None`, replace the "## Exception" section with `"No exception on last event (process paused without crash)."`.
  - When `snippets` is empty, emit `"_No in-repo source files referenced by the stack — agent should use MCP to call get_callstack/get_locals for more frames._"`.
  - The `"## Your task"` numbered list substitutes the build command into step 4; if `build_cmd is None`, step 4 becomes `"4. Do not attempt to build (no build command provided)."`.
  - The `"## Available MCP tools"` section always lists the 9 tools including `detach_process` but explicitly says `"Do NOT call detach_process or continue_execution — those would release or resume the target."`
- **Acceptance criteria:**
  - Structural test passes.
  - Render of a minimal capture (no exception, no stack, no locals, no snippets) does not crash and produces a Markdown file ≥ 400 bytes.
  - No Python `None` or `"None"` appears in the output.
  - File-write helper `write_briefing(path, content)` encodes `utf-8` with `\n` line endings.
- **Size:** S
- **Dependencies:** 2a.0.3, 2a.1.3.

#### Task 2a.1.5 — MCP-config and system-append file generators

- **Files:** `src/debugbridge/fix/claude_runner.py` (new — writer helpers only, subprocess wrapper lands in 2a.3.4), `tests/test_fix_claude_runner.py` (new).
- **Failing test to write first:** `tests/test_fix_claude_runner.py::test_write_mcp_config_schema` — call `write_mcp_config(tmp_path, host="127.0.0.1", port=8585)`, read the written JSON, assert it equals `{"mcpServers": {"debugbridge": {"type": "http", "url": "http://127.0.0.1:8585/mcp"}}}`.
- **Action:** Two functions:
  - `write_mcp_config(target_dir: Path, host: str, port: int) -> Path` — writes `target_dir / "mcp-config.json"` per RESEARCH.md §1.2. Returns the path.
  - `write_system_append(target_dir: Path) -> Path` — writes `target_dir / "system-append.md"` with the content from RESEARCH.md §5.4 (the one-paragraph "you are a crash-fix agent" instruction). Returns the path.
- **Acceptance criteria:**
  - JSON schema test passes.
  - System-append test asserts the file ends with a trailing newline and contains the phrase `"crash-fix agent"`.
  - Both helpers create parent directories if missing.
- **Size:** XS
- **Dependencies:** none.

---

### Phase 2a.2 — Hand-off mode

#### Task 2a.2.1 — Gitignore + first-run setup for `.debugbridge/`

- **Files:** `src/debugbridge/fix/worktree.py` (new — just gitignore + git detection to start), `tests/test_fix_worktree.py` (new).
- **Failing test to write first:** `tests/test_fix_worktree.py::test_ensure_gitignore_appends_entry` — create a tmp repo with a `.gitignore` containing `"node_modules/\n"`, call `ensure_gitignore(tmp_path)`, assert the file now contains both `node_modules/` and `/.debugbridge/`, and calling again is idempotent.
- **Action:** Implement `is_git_repo(path: Path) -> bool`, `detect_dirty(path: Path) -> bool`, and `ensure_gitignore(repo: Path) -> None` per RESEARCH.md §4.3–4.4 code sketches. Use `subprocess.run(["git", ...], cwd=path, capture_output=True, text=True, encoding="utf-8", errors="replace")` for git probes. Check both `/.debugbridge` and `.debugbridge/` patterns for existing entries.
- **Acceptance criteria:**
  - `ensure_gitignore` idempotency test passes (running twice produces the same file).
  - Missing-gitignore test passes (runs from a fresh path, creates a new `.gitignore` with a single entry).
  - `is_git_repo(tmp_path)` returns `False` for a tmp dir; `True` after `subprocess.run(["git", "init"], cwd=tmp_path)`.
- **Size:** S
- **Dependencies:** none.

#### Task 2a.2.2 — Hand-off dispatcher and interactive Claude launch

- **Files:** `src/debugbridge/fix/dispatcher.py` (new — hand-off path only, autonomous path lands in 2a.3.8), `src/debugbridge/fix/claude_runner.py` (extend with `run_claude_interactive`), `tests/test_fix_dispatcher.py` (new).
- **Failing test to write first:** `tests/test_fix_dispatcher.py::test_handoff_writes_briefing_and_invokes_claude_with_correct_args` — monkeypatch `capture_crash` to return a canned `CrashCapture`, monkeypatch `subprocess.run` to record its argv + cwd and return `CompletedProcess(returncode=0)`; call `run_handoff(repo, pid, host, port)`; assert the briefing file exists at `.debugbridge/briefings/crash-<hash>.md`, the mcp-config exists at `.debugbridge/mcp-config.json`, and the recorded argv starts with `["claude", "--mcp-config", ..., "--strict-mcp-config"]` followed by a positional string containing `"@.debugbridge/briefings/crash-"`.
- **Action:** `run_claude_interactive(repo, briefing_rel, mcp_config_path) -> int` runs:
  ```python
  cmd = [
      "claude",
      "--mcp-config", str(mcp_config_path),
      "--strict-mcp-config",
      f"I've just captured a crash. Read @{briefing_rel.as_posix()} and propose a fix.",
  ]
  return subprocess.run(cmd, cwd=str(repo)).returncode
  ```
  Matches RESEARCH.md §2.4 + §2.1. `run_handoff(repo, pid, host, port, conn_str=None)` in dispatcher: ensures server, captures crash, extracts source snippets, writes briefing + mcp-config, prints the Rich summary header, then hands off via `run_claude_interactive`. On exit, if we auto-spawned the server it's left running (user is now interacting — closing would break the session); we document this in the final summary.
- **Acceptance criteria:**
  - Dispatcher argv-shape test passes.
  - Briefing file is written under `.debugbridge/briefings/` (not the repo root — keep clutter out).
  - When `--strict-mcp-config` is present (RESEARCH.md Pitfall 7 mitigation), user-level MCP servers don't interfere.
  - Windows handoff uses `subprocess.run(...).returncode`, not `os.execvp`.
  - Dispatcher returns `FixResult(ok=True, mode="handoff", ...)` after claude exits zero.
- **Size:** M — flagged. If this crosses 45 min, split into (a) `run_claude_interactive` + tests, (b) `run_handoff` + tests.
- **Dependencies:** 2a.0.3, 2a.1.2, 2a.1.4, 2a.1.5, 2a.2.1.

---

### Phase 2a.3 — Autonomous mode

#### Task 2a.3.1 — Crash-hash + worktree create/cleanup

- **Files:** `src/debugbridge/fix/worktree.py` (extend), `tests/test_fix_worktree.py` (extend).
- **Failing test to write first:** `tests/test_fix_worktree.py::test_create_worktree_from_tmp_git_repo` — init a tmp git repo, make one commit, call `create_worktree(repo, "a1b2c3d4")`, assert `.debugbridge/wt-a1b2c3d4/` exists, assert a branch `debugbridge/fix-a1b2c3d4` exists (`git branch --list`), assert re-calling with the same hash cleans up and recreates successfully.
- **Action:** Port RESEARCH.md §4.2 `create_worktree`, `capture_diff`, `cleanup_worktree_on_success`, `cleanup_worktree_on_failure` verbatim. Also add `compute_crash_hash(capture: CrashCapture) -> str` here (imported by `mcp_client.capture_crash` — acceptable: no cycle, `worktree.py` doesn't import `mcp_client`).
- **Acceptance criteria:**
  - Create-worktree test passes.
  - Re-run-after-stale-worktree test passes (simulate leftover `.debugbridge/wt-<hash>/` → `create_worktree` removes and recreates).
  - `capture_diff` against an unmodified worktree returns `""`.
  - `capture_diff` against a modified worktree returns a non-empty unified diff with `diff --git` header.
  - `cleanup_worktree_on_failure` is a no-op on the filesystem (just logs).
- **Size:** S
- **Dependencies:** 2a.2.1 (shares the same file), 2a.0.3 (uses `CrashCapture`).

#### Task 2a.3.2 — Build/test runner

- **Files:** `src/debugbridge/fix/build_runner.py` (new), `tests/test_fix_build_runner.py` (new).
- **Failing test to write first:** `tests/test_fix_build_runner.py::test_run_command_returncode_and_output` — run `run_command("python -c \"print('ok'); import sys; sys.exit(0)\"", cwd=tmp_path, timeout=5)`, assert return is `(True, output)` and `"ok"` is in output; then `run_command("python -c \"import sys; sys.exit(7)\"", ...)`, assert `(False, _)`.
- **Action:** `run_command(cmd: str, cwd: Path, timeout: int = 600) -> tuple[bool, str]` using `subprocess.run(cmd, cwd=str(cwd), shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"})`. `shell=True` on purpose — the user's `--build-cmd` / `--test-cmd` is arbitrary, may include shell operators, and Windows `cmd.exe` handles their quoting. Combined `stdout + stderr` returned as a single string (build errors typically land on stderr; Claude needs both).
- **Acceptance criteria:**
  - Success test passes.
  - Failure test passes.
  - Timeout test passes (`run_command("python -c \"import time; time.sleep(10)\"", cwd, timeout=1)` returns `(False, output_with_timeout_marker)`).
  - Output is UTF-8 regardless of Windows default codepage.
- **Size:** S
- **Dependencies:** none.

#### Task 2a.3.3 — Diff-to-patch writer and failure report

- **Files:** `src/debugbridge/fix/patch_writer.py` (new), `tests/test_fix_patch_writer.py` (new).
- **Failing test to write first:** `tests/test_fix_patch_writer.py::test_write_patch_and_failure_report` — write a stub diff string via `write_patch(tmp_repo, "a1b2c3d4", "diff --git a/x b/x\n...")`, assert the file exists at `.debugbridge/patches/crash-a1b2c3d4.patch`, and its content round-trips exactly (bytes). Then `write_failure_report(tmp_repo, "a1b2c3d4", attempts=[...])` and assert `.debugbridge/patches/crash-a1b2c3d4.failed.md` exists and contains the strings `"Attempt 1"`, `"build output"`, and the crash hash.
- **Action:** Two functions per RESEARCH.md §4.2 + §7.1. `write_patch(repo, crash_hash, diff) -> Path` writes UTF-8 with `errors="replace"`. `write_failure_report(repo, crash_hash, attempts: list[AttemptRecord], final_msg: str) -> Path` writes a Markdown report listing each attempt: its `ClaudeRunResult.result` text, its build output (truncated to 2K chars each), cost. Creates `.debugbridge/patches/` if missing.
- **Acceptance criteria:**
  - Round-trip test for `write_patch`.
  - Failure-report test passes structural checks.
  - Both functions return `Path` objects pointing to existing files.
- **Size:** S
- **Dependencies:** 2a.0.3 (uses `AttemptRecord`).

#### Task 2a.3.4 — Claude Code headless subprocess wrapper

- **Files:** `src/debugbridge/fix/claude_runner.py` (extend), `tests/test_fix_claude_runner.py` (extend).
- **Failing test to write first:** `tests/test_fix_claude_runner.py::test_parse_claude_json_handles_noise_prefix` — pass `"WARN: auth cache refreshed\n{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"ok\",\"total_cost_usd\":0.05,\"usage\":{\"input_tokens\":1,\"output_tokens\":2,\"cache_read_input_tokens\":10},\"num_turns\":1,\"duration_ms\":100,\"session_id\":\"abc\"}\n"` to `_parse_claude_json`, assert `parsed["total_cost_usd"] == 0.05`.
- **Action:** Port RESEARCH.md §Complete run_claude wrapper code verbatim into `claude_runner.run_claude_headless(cwd, briefing_path, mcp_config_path, system_append_path, model="sonnet", max_turns=20, max_budget_usd=0.75, build_cmd=None) -> ClaudeRunResult`. Key details:
  - `--allowedTools` list: `"Read,Edit,Write,Glob,Grep,mcp__debugbridge__*"` plus `Bash({tokenized_build_cmd} *)` when `build_cmd` is provided (e.g. `Bash(cmake *)`). Do NOT include broad `Bash(*)`.
  - Positional arg: `f"Read @{briefing_path.relative_to(cwd).as_posix()} and produce the minimal fix."` (short — avoids Windows cmd-line limit per Pitfall 1).
  - `encoding="utf-8", errors="replace"` and `PYTHONIOENCODING=utf-8` (Pitfall 8).
  - 10-minute per-attempt timeout (`timeout=600`).
  - `--strict-mcp-config` always set (Pitfall 7).
- **Acceptance criteria:**
  - JSON-noise-prefix parse test passes.
  - Empty-stdout test passes: returns `ClaudeRunResult(ok=False, is_error=True, subtype="empty_output", ...)`.
  - Unparseable-output test passes: returns `subtype="unparseable_output"`.
  - Successful parse: fields map 1:1 to the JSON; `input_tokens` = `usage.input_tokens + usage.cache_read_input_tokens`.
  - Unit tests use stub stdout strings; no actual `claude` subprocess is invoked.
- **Size:** M — flagged. If it trips 45 min, split into (a) `_parse_claude_json` + tests, (b) `run_claude_headless` command construction + argv-shape tests. Argv shape is verifiable without running claude by monkeypatching `subprocess.run`.
- **Dependencies:** 2a.0.3, 2a.1.5.

#### Task 2a.3.5 — Autonomous attempt loop (no retry feedback yet)

- **Files:** `src/debugbridge/fix/dispatcher.py` (extend), `tests/test_fix_dispatcher.py` (extend).
- **Failing test to write first:** `tests/test_fix_dispatcher.py::test_auto_loop_single_attempt_success` — monkeypatch `run_claude_headless` to return a canned `ok=True` result, monkeypatch `run_command` to return `(True, "build passed")`, monkeypatch `capture_crash` to return canned data; call `run_autonomous(...)`; assert `result.ok is True`, `len(result.attempts) == 1`, `result.patch_path` exists (stubbed diff written).
- **Action:** `run_autonomous(repo, pid, host, port, build_cmd, test_cmd, model, max_attempts, conn_str)` in dispatcher:
  1. Ensure server, capture crash, extract snippets, write briefing, write mcp-config + system-append inside the worktree's `.debugbridge/` (actually: the `system-append` and `mcp-config` go in `$REPO/.debugbridge/` — outside the worktree — the worktree only needs the briefing; pass absolute paths to Claude).
  2. Create worktree.
  3. Single attempt (retry feedback is 2a.3.6):
     - `run_claude_headless(cwd=worktree, ...)`.
     - If `is_error` → break loop, emit failure report.
     - `run_command(build_cmd, cwd=worktree)`.
     - If `ok` → `capture_diff(worktree)` → `write_patch` → `cleanup_worktree_on_success` → return ok.
     - If not → append to attempts; next loop iter.
  4. After loop: if no build ever passed, `write_failure_report`, `cleanup_worktree_on_failure`, return `ok=False`.
- **Acceptance criteria:**
  - Single-attempt-success test passes.
  - Single-attempt-build-failure-exhausts-attempts test passes (max_attempts=1, build always fails → `FixResult.ok=False`, failure report written, worktree preserved).
  - `run_autonomous` never touches the user's main tree — proven by a test that initializes a tmp git repo, writes a sentinel file, runs the monkeypatched autonomous loop, asserts the sentinel file is unchanged and no new files exist outside `.debugbridge/`.
- **Size:** M — flagged. Retry feedback (2a.3.6) is intentionally separated to keep each task ≤ 45 min.
- **Dependencies:** 2a.1.2, 2a.1.4, 2a.1.5, 2a.2.1, 2a.3.1, 2a.3.2, 2a.3.3, 2a.3.4.

#### Task 2a.3.6 — Retry-feedback loop (append build output to briefing on failure)

- **Files:** `src/debugbridge/fix/dispatcher.py` (extend), `src/debugbridge/fix/briefing.py` (extend), `tests/test_fix_dispatcher.py` (extend), `tests/test_fix_briefing.py` (extend).
- **Failing test to write first:** `tests/test_fix_dispatcher.py::test_auto_loop_retries_on_build_failure_with_appended_output` — `run_command` returns `(False, "ld: undefined symbol x")` first, `(True, "ok")` second; `run_claude_headless` is monkeypatched to record the briefing file's content each time it's called; assert the second call sees a briefing that contains the string `"ld: undefined symbol x"` under a "## Previous attempt" section; assert `result.ok is True`, `len(result.attempts) == 2`.
- **Action:** Add `append_retry_feedback(briefing_path: Path, attempt_num: int, build_output: str, claude_result_text: str | None) -> None` to `briefing.py`. It appends a new section to the briefing file: `\n\n## Previous attempt {N}\n\nClaude proposed:\n> {claude_result_text}\n\nBuild failed:\n\n```\n{build_output[:2000]}\n```\n\nPlease produce a different fix taking this build error into account.\n`. In `dispatcher.run_autonomous`, after a failed build, call this helper before next iteration. Hard cap enforced by `range(1, max_attempts + 1)` loop; after the final attempt's build still failing, no new feedback is appended (pointless).
- **Acceptance criteria:**
  - Retry-feedback test passes: second claude call sees the appended build output.
  - `result.attempts[0].build_ok is False` and `result.attempts[1].build_ok is True`.
  - Test for `append_retry_feedback` truncates build output >2000 chars with a `"[output truncated]"` marker.
- **Size:** S
- **Dependencies:** 2a.3.5.

#### Task 2a.3.7 — Test-command runs after successful build (optional, `--test-cmd`)

- **Files:** `src/debugbridge/fix/dispatcher.py` (extend), `tests/test_fix_dispatcher.py` (extend).
- **Failing test to write first:** `tests/test_fix_dispatcher.py::test_auto_loop_runs_test_cmd_after_build` — `build_cmd` + `test_cmd` both provided, build always passes, tests pass on attempt 1; assert the recorded call order is `[claude, build, test]` in that order and `result.ok is True`.
- **Action:** In `run_autonomous`, when `build_ok` is `True` AND `test_cmd` is not None, run `run_command(test_cmd, cwd=worktree)`. If tests pass → emit patch (same as build-only success). If tests fail → treat same as build failure: append feedback (`"## Previous attempt {N}\n\nBuild passed but tests failed:\n..."`), continue loop. **Uses the `test_ok` and `test_output` fields already defined on `AttemptRecord` at 2a.0.3** — no model mutation. Fill `test_ok=True` on pass, `False` on fail, leave as default `None` when `--test-cmd` not provided. `build_ok` retains its meaning (build step alone); overall success at the dispatcher level is `build_ok and (test_ok in (True, None))`.
- **Acceptance criteria:**
  - Build-passes-test-fails test passes: second claude call sees feedback with test output, not build output.
  - Build-passes-no-test-cmd test passes: emits patch after just the build step.
  - `AttemptRecord.test_ok` is `True`/`False`/`None` per the actual state.
- **Size:** S
- **Dependencies:** 2a.3.6 (retry loop).

#### Task 2a.3.8 — Signal handlers (Ctrl-C) and cleanup

- **Files:** `src/debugbridge/fix/dispatcher.py` (extend), `tests/test_fix_dispatcher.py` (extend).
- **Failing test to write first:** `tests/test_fix_dispatcher.py::test_sigint_terminates_claude_and_preserves_worktree` — start a fake long-running claude subprocess (use `subprocess.Popen` on `python -c "import time; time.sleep(30)"`), record it in the dispatcher's state, call the installed handler with `signal.SIGINT`, assert the subprocess is terminated within 5s, assert the worktree (tmp path) still exists on disk, assert the test's process exit-intent code is 130.
- **Action:** Implement RESEARCH.md §7.2 `install_signal_handlers(state)` and wire it up at the top of `run_autonomous`. State is a simple dataclass tracking `claude_proc: subprocess.Popen | None`, `server_proc: subprocess.Popen | None`, `worktree_path: Path | None`, `did_spawn_server: bool`. Handler terminates claude, then shuts the server if we spawned it (which detaches pybag), then exits 130. Leaves worktree on disk. Installs `SIGINT` on all platforms and additionally `SIGBREAK` on Windows.
- **Acceptance criteria:**
  - Signal-handler test passes.
  - Handler is idempotent — calling twice doesn't double-terminate.
  - After handler runs, `state.claude_proc.poll()` is not None.
- **Size:** S
- **Dependencies:** 2a.3.5.

#### Task 2a.3.9 — Cost-summary aggregation + Rich progress output

- **Files:** `src/debugbridge/fix/dispatcher.py` (extend), `tests/test_fix_dispatcher.py` (extend).
- **Failing test to write first:** `tests/test_fix_dispatcher.py::test_fix_result_aggregates_cost_across_attempts` — two stubbed attempts with costs 0.08 and 0.12, assert `result.total_cost_usd == pytest.approx(0.20, abs=0.001)`, `result.total_input_tokens == sum(...)`, `result.total_output_tokens == sum(...)`.
- **Action:** In `run_autonomous`, after the loop, aggregate `total_cost_usd`, `total_input_tokens`, `total_output_tokens` across `attempts[]`. Render the final summary block per RESEARCH.md §8.3 (text by default, JSON if `--json`):
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
  Use Rich `Progress(SpinnerColumn(), TextColumn(...))` for the attach → capture → worktree → claude → build phases (RESEARCH.md §8.2).
- **Acceptance criteria:**
  - Cost-aggregation test passes.
  - Summary output contains expected literal strings.
  - `--json` branch produces parseable JSON with shape documented in RESEARCH.md §8.3.
  - Rich progress is optional (emits plain prints in a no-TTY env).
- **Size:** S
- **Dependencies:** 2a.3.5, 2a.3.6, 2a.3.7.

#### Task 2a.3.10 — (Stretch, optional) `claude --resume` across attempts

- **Files:** `src/debugbridge/fix/claude_runner.py` (extend), `src/debugbridge/fix/dispatcher.py` (extend).
- **Failing test to write first:** `tests/test_fix_dispatcher.py::test_auto_loop_uses_session_resume_on_retry` — second `run_claude_headless` invocation includes `--resume <session_id_from_attempt_1>` and a short prompt like `"Build failed: ... try again"` instead of the full briefing.
- **Action:** Pull `session_id` from attempt N's JSON, pass to attempt N+1 as `--resume <sid>` with a concise retry prompt. Reduces tokens on retry. Skipped if this task is not yet implemented; the naive approach in 2a.3.6 is sufficient.
- **Acceptance criteria:**
  - Second-attempt argv includes `--resume`.
  - Feature-flag defaults `False`; enable via internal constant `USE_SESSION_RESUME = False` (flip to True after calibration).
- **Size:** S
- **Dependencies:** 2a.3.6.
- **Note:** Explicitly a stretch. Skip if Phase 2a schedule is tight. GOAL.md acceptance criterion 6 (cost tracking) is satisfied without this.

---

### Phase 2a.4 — Integration (CLI wiring, end-to-end tests, docs)

#### Task 2a.4.1 — `debugbridge fix` Typer subcommand

- **Files:** `src/debugbridge/cli.py` (extend), `tests/test_cli.py` (new — pure unit test using `typer.testing.CliRunner`).
- **Failing test to write first:** `tests/test_cli.py::test_fix_help_shows_all_flags` — use `CliRunner().invoke(app, ["fix", "--help"])`; assert exit_code == 0; assert stdout contains each of `--pid`, `--repo`, `--conn-str`, `--build-cmd`, `--test-cmd`, `--auto`, `--host`, `--port`, `--model`, `--max-attempts`, `--json`.
- **Action:** Add a `fix` command to `cli.py` per RESEARCH.md §8.1. The command is a thin wrapper that imports `from debugbridge.fix.dispatcher import run_handoff, run_autonomous` **lazily** (inside the function, not at module top) to preserve the Phase 1 invariant that `cli.py` doesn't load heavy deps at import time. Validates: `repo.exists()` + is a git repo + claude CLI on PATH + Windows platform (warn, don't error, for cross-platform dev paths). Dispatches to `run_autonomous(...)` if `--auto`, else `run_handoff(...)`. Prints JSON if `--json`, Rich summary otherwise.
- **Acceptance criteria:**
  - Help test passes.
  - `debugbridge fix --pid 0 --repo C:/nonexistent` exits non-zero with a clear "not a git repository" error.
  - `debugbridge fix --pid 0 --repo .` (with `claude` missing from PATH) exits non-zero with a clear "claude CLI not found" error.
  - Help text is discoverable via `debugbridge --help`.
- **Size:** S
- **Dependencies:** 2a.2.2, 2a.3.5, 2a.3.9.

#### Task 2a.4.2 — End-to-end integration test against `crash_app null`

- **Files:** `tests/test_fix_e2e.py` (new), `tests/conftest.py` (extend with a `crash_app_null_crashed` fixture if helpful), `scripts/e2e_fix_smoke.py` (new — analog to `e2e_smoke.py`).
- **Failing test to write first:** `tests/test_fix_e2e.py::test_fix_auto_produces_patch` (marked `@pytest.mark.integration` and `@pytest.mark.slow` — the CI profile skips `slow` too). Launches `crash_app null` via `UserDbg.create(...initial_break=True)` in a tmp worktree of the `tests/fixtures/crash_app` subrepo (need to `git init` that subdirectory for the test if not already a repo — or use a tmp repo that contains a minimal CMake file + crash.cpp). Alternatively, because setting up a real build is heavy, invoke `debugbridge fix` with a **fake** `--build-cmd` that always succeeds (`python -c "import sys; sys.exit(0)"`) — this still exercises the full dispatcher loop end-to-end including real claude invocation. Asserts a `.patch` file ≥ 1 byte exists at `.debugbridge/patches/crash-*.patch` after the fix run; cost > 0; `result.ok is True`.
- **Action:** The "fake-build" variant is the pragmatic integration test. Real build validation (`cmake --build ...`) is additionally exercised manually via `scripts/e2e_fix_smoke.py`, which is the human demo script from GOAL.md § "Acceptance demo" — copy that command sequence verbatim into the script, add `print()` instrumentation like `scripts/e2e_smoke.py`, leave the worktree on failure. CI skips this test because: (a) claude needs auth credentials, (b) the crashed process is slow to spin up, (c) it costs real money. The `@pytest.mark.slow` marker ensures it's opt-in.
- **Acceptance criteria:**
  - Test exists and auto-skips in CI (via `integration` + `slow` markers).
  - Locally, running `pytest -m "integration and slow" tests/test_fix_e2e.py` against a machine with claude auth'd produces a green test and a real `.patch` file.
  - `scripts/e2e_fix_smoke.py` runs the GOAL.md demo command sequence verbatim and prints the expected output shape.
  - No new Python deps added for this task.
- **Size:** M — flagged, split if needed into (a) fake-build pytest integration test, (b) `scripts/e2e_fix_smoke.py` manual script.
- **Dependencies:** All of 2a.1 + 2a.3 tasks.

#### Task 2a.4.3 — README section, version bump, and constraint-enforcement CI step

- **Files:** `README.md` (extend), `src/debugbridge/__init__.py` (bump to `0.2.0`), `pyproject.toml` (bump version), `CHANGELOG.md` (new — minimal), `.github/workflows/ci.yml` (add grep-based constraint check).
- **Failing test to write first:** N/A for docs. Add `tests/test_version.py::test_version_matches_pyproject` that parses `pyproject.toml` with `tomllib` and asserts equality with `debugbridge.__version__`. For the CI step: add `tests/test_import_constraints.py::test_fix_does_not_import_debugsession` that uses `subprocess.run(["rg", "-q", "from debugbridge\\.session", "src/debugbridge/fix"])` and asserts `returncode != 0` (rg exits 1 when no match). This gives us the same check locally as in CI.
- **Action:**
  1. Add a "Fix-loop" section to README.md with: one-paragraph overview, the hand-off command, the autonomous command (GOAL.md demo block), the `claude` prerequisite + `debugbridge doctor`, where patches land, the 3-attempt cap, and the cost disclaimer.
  2. Update `__version__` and `pyproject.toml` to `0.2.0`.
  3. Create `CHANGELOG.md` with a `## 0.2.0 — Fix-loop MVP` section listing: `fix` command, `detach_process` MCP tool, `fix/` subpackage, `doctor` extensions.
  4. **Add a new step to `.github/workflows/ci.yml`** (immediately before `Lint (ruff)`):
     ```yaml
     - name: Enforce fix/ coupling rule (no direct DebugSession import)
       shell: bash
       run: |
         if rg -q "from debugbridge\.session" src/debugbridge/fix; then
           echo "ERROR: src/debugbridge/fix/ must not import from debugbridge.session — the fix agent talks to the server via MCP only."
           rg -n "from debugbridge\.session" src/debugbridge/fix
           exit 1
         fi
     ```
     This enforces architecture decision #1 (agent ↔ server coupling is strictly MCP) mechanically, not aspirationally.
- **Acceptance criteria:**
  - README section exists with the demo block.
  - Version test passes.
  - `pyproject.toml` version is `0.2.0`.
  - CHANGELOG.md is present.
  - `tests/test_import_constraints.py::test_fix_does_not_import_debugsession` passes (no matches in `src/debugbridge/fix/`).
  - CI workflow has the new grep step and it passes on the current commit.
  - If someone introduces `from debugbridge.session import DebugSession` anywhere under `src/debugbridge/fix/`, CI fails with the error message above.
- **Size:** S
- **Dependencies:** All prior 2a tasks (final polish).

---

## 5. Goal-backward verification

Every acceptance criterion in GOAL.md is produced by at least one task. Gaps flagged as **GAP**.

| # | Goal acceptance criterion (abbrev.) | Evidence-producing tasks |
|---|-------------------------------------|--------------------------|
| 1 | Hand-off mode end-to-end with real crash_app | 2a.1.1, 2a.1.2, 2a.1.3, 2a.1.4, 2a.1.5, 2a.2.1, 2a.2.2, 2a.4.1; manually verified via 2a.4.2 handoff-variant (to add as an optional scripted path) |
| 2 | Autonomous mode end-to-end with `--build-cmd` → `.patch` | 2a.3.1, 2a.3.2, 2a.3.3, 2a.3.4, 2a.3.5, 2a.3.6, 2a.3.9; 2a.4.1; 2a.4.2 |
| 3 | Agent ↔ server coupling is strictly MCP | 2a.1.1, 2a.1.2 (no DebugSession imports anywhere in `src/debugbridge/fix/**`); **enforcement**: task 2a.4.3 adds a `.github/workflows/ci.yml` grep step + `tests/test_import_constraints.py` that fail CI if anything under `src/debugbridge/fix/` imports `debugbridge.session` |
| 4 | Repo isolation — writes under `.debugbridge/`, gitignore auto-added | 2a.2.1, 2a.3.1, 2a.3.3 (all writers go under `.debugbridge/`); 2a.3.5 has a test proving no writes outside `.debugbridge/` |
| 5 | Build/test validation user-parametric | 2a.3.2, 2a.3.5, 2a.3.7 |
| 6 | Cost tracking + 3-attempt cap | 2a.3.4 (parses `total_cost_usd`), 2a.3.5/3.6 (enforces `max_attempts`), 2a.3.9 (aggregates + reports) |
| 7 | Tests prove it — integration test + unit tests + CI auto-skip | 2a.4.2 (integration); every phase-2a task has a unit test on its feature; 2a.4.2 honors the `integration` marker for CI auto-skip |

**GAP flag — hand-off e2e scripted verification:** GOAL.md criterion 1 says "tested with a real crashed crash_app.exe (null-deref mode) — the developer sees a Claude Code session with stack trace and source file hints already on screen." Currently 2a.4.2 only adds an autonomous-mode integration test. Hand-off verification today is "run the command and look at the screen" — hard to automate since it's interactive.

**Resolution:** Add a **hand-off verification** paragraph to `scripts/e2e_fix_smoke.py` (in task 2a.4.2) that runs `debugbridge fix --pid <wait_pid> --repo .` in hand-off mode but captures Claude's startup with `--output-format json` injected via monkey-patch — this converts the interactive session into a one-shot call that we can assert the output of. If that fails, at minimum: the smoke script prints "About to hand off — expect claude to start interactively. Press Ctrl+C after verifying." and is manually exercised. No task-level gap in the plan — documented as a human-driven check in the verification plan (§7).

## 6. Risk register (plan-specific)

Risks that are specific to *this plan*, not the generic ones already documented in PROJECT.md.

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|------------|--------|------------|
| R1 | `claude` CLI not on PATH on dev machine | High (first-run) | Blocks all of phase 2a | Task 2a.0.2 extends `debugbridge doctor` to detect and guide install. Dispatcher also checks `shutil.which("claude")` and fails fast with a clear error (task 2a.4.1). |
| R2 | First-run `--dangerously-skip-permissions` prompt hangs headless subprocess | Medium | Silent 30s hang, non-zero exit | Task 2a.0.2's doctor check surfaces the warning; user acknowledges via one-time interactive `claude --dangerously-skip-permissions --help`. Also document in README (task 2a.4.3). |
| R3 | MCP tool-result tokens exceed `--max-budget-usd` prematurely | Medium | Claude aborts mid-fix with `is_error=true`, subtype=budget | Tasks 2a.3.4/3.5 use a belt-and-suspenders approach: `--max-budget-usd 0.75` per attempt + `max_attempts=3` + `MAX_MCP_OUTPUT_TOKENS` env passed through. 2a.4.2's integration test emits real cost numbers for calibration. |
| R4 | Worktree left behind on unclean exit | Medium | Dir pollution in `.debugbridge/`, subsequent runs hit stale-worktree error | Task 2a.3.1's `create_worktree` auto-cleans same-hash stale worktrees. Task 2a.3.8's SIGINT handler explicitly preserves worktree (for inspection) but logs its path. README documents `git worktree remove --force` cleanup command. |
| R5 | User's repo has uncommitted changes at fix-start | High | Worktree branches off HEAD, not their WIP — confusion | Task 2a.3.1's `detect_dirty` logs a `[yellow]warning[/yellow]` but doesn't abort. Briefing generator (task 2a.1.4) notes the branch-off commit in metadata. |
| R6 | Agent attempts fixes in third-party / stdlib code instead of user code | Medium | Unfixable patches, wasted $ | Task 2a.1.3 filters stack frames to in-repo only for source snippets. Briefing (task 2a.1.4) Constraints section tells Claude "Only edit files listed in the Source context section." `--allowedTools` excludes broad Bash access (task 2a.3.4), so Claude can't shell out to modify system files. |
| R7 | `claude-agent-sdk` gets tempting mid-implementation | Low | 200+ lines of wrapper; wasted effort | Architecture decision #2 is pinned. All Claude Code interaction goes through the CLI subprocess. RESEARCH.md §Alternatives explicitly warns off the SDK on Windows + Python 3.12. |
| R8 | `kn f` output format drifts across pybag / WinDbg versions | Low | `_parse_callstack` returns empty frames, briefing degrades | Phase 1's `_fallback_backtrace` already handles this (returns frames sans file/line). Briefing still usable. Unit tests at `test_parsers.py` guard against the most likely drift. |
| R9 | MCP server port 8585 already bound by another process | Low | `ensure_server_running` sees a live port but the server isn't ours | Task 2a.1.1's ensure logic probes TCP only, not "is it OUR server." Mitigation: if `capture_crash` gets back MCP errors (not our 9 tools), fail fast with "port 8585 is bound by a non-DebugBridge process; pass `--port 8586` or shut the other process down." |
| R10 | Windows cmd-line length silently truncates briefing prompt | Low (guarded) | Claude sees empty prompt, produces fluff | Task 2a.3.4 always passes the briefing as `@file` reference, never as inline string. `_parse_claude_json` handles the "hey what would you like me to do?" generic response by treating it as non-useful (heuristic: if `num_turns < 2`, flag in failure report). |

## 7. Verification plan — proving Phase 2a is done

Phase 2a exits when all seven of GOAL.md's criteria have evidence. The test matrix below is the exit checklist.

### Automated tests (run via `uv run pytest`)

1. All unit tests pass (no integration markers): `uv run pytest -m "not integration"` — exit 0, includes every new `test_fix_*.py` test file.
2. Phase-1 tests still pass: same command confirms the 22 Phase-1 tests haven't regressed.
3. Integration tests (local Windows machine with Debugging Tools + claude): `uv run pytest -m "integration and not slow"` — exit 0, includes `test_detach_process_releases_target`, `test_capture_crash_end_to_end`, and dispatcher-orchestration tests using monkeypatched claude.
4. Slow/E2E tests (local + claude authenticated): `uv run pytest -m "integration and slow"` — at least `test_fix_auto_produces_patch` passes with a real `.patch` generated.
5. Lint + typecheck: `uv run ruff check`, `uv run pyright` — both clean on all new files.

### Manual exit demo (the GOAL.md acceptance demo, verbatim)

Run the exact sequence from GOAL.md § "Acceptance demo" on a fresh Windows dev machine:

```powershell
# Terminal A:
D:\Projects\BridgeIt\tests\fixtures\crash_app\build\Debug\crash_app.exe null
# (crashes)

# Terminal B:
D:\Projects\BridgeIt\tests\fixtures\crash_app\build\Debug\crash_app.exe wait
# (blocks; note PID)

# Terminal C:
debugbridge fix --pid <PID> --repo D:/Projects/BridgeIt `
    --auto `
    --build-cmd "cmake --build tests/fixtures/crash_app/build --config Debug"
```

Expected output shape (exact text per GOAL.md; tokens/cost will vary):

```
[debugbridge] attaching to pid <PID>…
[debugbridge] captured: EXCEPTION_ACCESS_VIOLATION @ crash_app!crash_null+0x2a
[debugbridge] working in worktree .debugbridge/wt-<hash>
[claude-code] analyzing crash…
[claude-code] proposed fix: …
[debugbridge] running build command…
[debugbridge] build passed
[debugbridge] patch written: .debugbridge/patches/crash-<hash>.patch
[debugbridge] tokens: <N>K in / <M>K out, est cost $<Z>
[debugbridge] apply with: git apply .debugbridge/patches/crash-<hash>.patch
```

**Hand-off verification (GOAL.md criterion 1):**

```powershell
# (crash_app wait running as above)
debugbridge fix --pid <PID> --repo D:/Projects/BridgeIt
# Expected: claude opens interactively in the current terminal with a first
# message already queued that references @.debugbridge/briefings/crash-<hash>.md
# and mentions "a crash." Manual visual check; type /exit to leave.
```

**Patch-apply verification (manual):**

```powershell
cd D:/Projects/BridgeIt
git checkout -b debugbridge-fix-test
git apply .debugbridge/patches/crash-<hash>.patch
git diff  # Human inspects the diff
# If acceptable: commit on a branch, test, PR. If not: abandon the branch.
```

### What counts as "shipped"

- All automated tests listed above are green.
- The manual autonomous demo produces a `.patch` file ≥ 1 line.
- The manual hand-off demo opens Claude Code with the briefing pre-loaded.
- The patch, when applied, resolves the null-deref (manually verified by re-running `crash_app null` — it should no longer crash at the same site).
- `debugbridge doctor` reports all components green on the demo machine.
- README has the `debugbridge fix` usage section.
- Git tag `v0.2.0-phase2a` pushed on `main`.

## 8. Explicit non-goals (copied from GOAL.md)

- Crash auto-detection — Phase 2.5
- PyPI publish — Phase 2c
- Non-Windows platforms — Phase 3
- Cloud relay — Phase 4
- "Smart" build-system auto-detection
- Multi-crash batch processing
- A daemon / watcher mode
- Claude Code UI customization

---

## Appendix A — Constraints cross-check

| Constraint | Where honored |
|------------|---------------|
| No breaking change to the 8 MCP tools | Task 2a.0.1 adds a new tool (`detach_process`), doesn't modify existing signatures. Verified in `scripts/e2e_smoke.py` — the `expected` set grows from 8 to 9, no existing names change. |
| Pybag imports stay lazy | Task 2a.0.1 only adds methods to `DebugSession`; `session.py`'s `_make_userdbg()` pattern is preserved. `cli.py`'s `fix` command imports `debugbridge.fix.dispatcher` lazily (task 2a.4.1) so `fix --help` doesn't load pybag or MCP. |
| No direct `from debugbridge.session import DebugSession` in fix-agent code | All `src/debugbridge/fix/**` code goes through MCP. **Enforced mechanically** by task 2a.4.3: a `.github/workflows/ci.yml` step runs `rg "from debugbridge\.session" src/debugbridge/fix` and fails CI on any match. A local test `tests/test_import_constraints.py::test_fix_does_not_import_debugsession` gives contributors the same signal before pushing. |
| New deps added to `pyproject.toml` explicitly and justified | **Zero new deps in Phase 2a.** All functionality uses stdlib (`subprocess`, `hashlib`, `json`, `pathlib`, `shutil`, `socket`, `signal`, `shlex`) + existing deps (`mcp[cli]`, `pydantic`, `typer`, `rich`). The `claude` CLI is a user prerequisite, not a pip dep. |
| All new production code under `src/debugbridge/fix/` | Verified — every new `.py` under `src/` is in `fix/`. Exceptions: `session.py` (add `detach` method — unavoidable), `tools.py` (add `detach_process` tool — unavoidable), `cli.py` (add `fix` command — unavoidable), `env.py` (add `check_claude_cli` — unavoidable). All four are small surgical additions to Phase 1 modules. |
| Tests under `tests/` with `test_*.py` + integration-mark pattern | New test files: `test_fix_models.py`, `test_fix_mcp_client.py`, `test_fix_briefing.py`, `test_fix_worktree.py`, `test_fix_claude_runner.py`, `test_fix_build_runner.py`, `test_fix_patch_writer.py`, `test_fix_dispatcher.py`, `test_fix_e2e.py`, `test_cli.py`, `test_version.py`. All follow the pattern. |

## Appendix B — Task dependency graph (summary)

Updated after plan-check C1 fix: `2a.1.2` now depends on `2a.3.1` (for `compute_crash_hash`). Worktree module lands before MCP capture.

```
2a.0.1 (detach_process tool)
2a.0.2 (doctor claude check)
2a.0.3 (fix/models.py — final AttemptRecord schema, no retroactive mutations)

2a.0.3 ──> 2a.1.1 (server auto-spawn)
2a.0.3 ──> 2a.1.3 (source snippets)
2a.0.3, 2a.1.3 ─> 2a.1.4 (briefing renderer)
(none)  ──> 2a.1.5 (mcp-config + system-append)
(none)  ──> 2a.2.1 (gitignore + git detection)

2a.2.1, 2a.0.3 ─> 2a.3.1 (worktree create/cleanup + compute_crash_hash)
2a.0.3, 2a.1.1, 2a.3.1 ─> 2a.1.2 (capture_crash; imports compute_crash_hash)

2a.1.2, 2a.1.4, 2a.1.5, 2a.2.1 ─> 2a.2.2 (handoff dispatch)

(none) ──> 2a.3.2 (build_runner)
2a.0.3 ──> 2a.3.3 (patch_writer)
2a.0.3, 2a.1.5 ─> 2a.3.4 (claude headless)
2a.1.2, 2a.1.4, 2a.1.5, 2a.2.1, 2a.3.1, 2a.3.2, 2a.3.3, 2a.3.4 ─> 2a.3.5 (auto loop)
2a.3.5 ──> 2a.3.6 (retry feedback)
2a.3.6 ──> 2a.3.7 (test_cmd — reuses pre-defined AttemptRecord fields)
2a.3.5 ──> 2a.3.8 (signal handlers)
2a.3.5, 2a.3.6, 2a.3.7 ─> 2a.3.9 (cost summary + Rich)
2a.3.6 ──> 2a.3.10 (stretch: session resume)

2a.2.2, 2a.3.9 ──> 2a.4.1 (CLI wiring)
all 2a.1 + all 2a.3 ──> 2a.4.2 (e2e integration)
all prior ──> 2a.4.3 (README + version bump + CI grep constraint)
```

Tasks ordered for execution:
`[2a.0.1, 2a.0.2, 2a.0.3]` → `[2a.1.1, 2a.1.3, 2a.1.5, 2a.2.1]` → `[2a.1.4, 2a.3.1]` → `[2a.1.2, 2a.3.2, 2a.3.3, 2a.3.4]` → `[2a.2.2]` → `[2a.3.5]` → `[2a.3.6]` → `[2a.3.7, 2a.3.8]` → `[2a.3.9]` → `[2a.3.10 (optional)]` → `[2a.4.1]` → `[2a.4.2]` → `[2a.4.3]`.

Critical path length: 13 serial tasks minimum (2a.3.10 is optional), +1 vs pre-fix because `2a.1.2` now follows `2a.3.1`. With 2–5 min per XS task, ~30 min per S task, ~45 min per M task, rough execution budget ~6–9 hours of `gsd-executor` wall clock, no re-planning loops.
