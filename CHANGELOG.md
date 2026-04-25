# Changelog

## [0.2.5] — 2026-04-24 — Crash auto-detection (Phase 2.5)

### Added

- **`stackly watch --pid N --repo PATH` CLI command** — blocks on a live process until it crashes, then dispatches the fix agent (hand-off by default, `--auto` for headless patching).
- **`watch_for_crash` MCP tool (10th tool)** — async, `anyio.to_thread.run_sync` offload preserves FastMCP event-loop responsiveness during long watches.
- **`WatchResult` discriminated union** in `stackly.models`: `WatchException` | `WatchTimedOut` | `WatchTargetExited`.
- **`DebugSession.wait_for_exception`** polling method with `_SYNTHETIC_CODES` filter suppressing attach-break / single-step false positives.
- **Rich spinner** during `watch`; `--quiet` for scripting.
- **Stay-resident mode** via `--max-crashes N`; re-attach failure exits cleanly (empirical: pybag 2.2.16 cannot re-attach a terminated PID).
- **`tests/test_doctor_unchanged.py`** regression guard encoding the "no new external deps" invariant from CONTEXT.md.

### Fixed (Codex review)

- **`--poll-seconds` flag is now honored** end-to-end (`stackly watch` → `run_watch` → `_watch_once` → `watch_for_crash` MCP call). Previously hardcoded to 1.
- **SIGINT handler's best-effort MCP detach** now runs in a fresh daemon thread with its own event loop instead of calling `asyncio.run(...)` on the main thread (which was already inside an active `asyncio.run` and therefore raised `RuntimeError`, leaving the target attached unexpectedly).

## [0.2.1] — 2026-04-23 — Renamed to Stackly

**BREAKING CHANGES.** This release is the rename of the project from DebugBridge to Stackly. Zero behavioral changes, zero new features, zero regressions — but every name-surface is new, which breaks any config that referenced the old name.

### Breaking

- **Python package renamed:** `debugbridge` → `stackly`. Imports change from `from debugbridge...` to `from stackly...`.
- **CLI command renamed:** `debugbridge` → `stackly`. Update any scripts that invoke `debugbridge serve`, `debugbridge doctor`, `debugbridge fix`, or `debugbridge version` — all four are now `stackly ...`.
- **MCP server name changed:** `debugbridge` → `stackly`. Update MCP client configs (Claude Code, Claude Desktop, Cursor) — the JSON key `"mcpServers": {"debugbridge": {...}}` becomes `"mcpServers": {"stackly": {...}}`. Claude Code's tool prefix also changed: `mcp__debugbridge__*` → `mcp__stackly__*`.
- **Artifact directory renamed:** `.debugbridge/` → `.stackly/` for briefings, patches, failure reports, and per-crash worktrees. Existing user workspaces are not migrated; the old directory can be deleted safely.
- **GitHub repo renamed:** `IdanG7/bridgeit` → `IdanG7/stackly`. GitHub installs a permanent redirect, so existing `git clone` URLs keep working, but update your remote with `git remote set-url origin https://github.com/IdanG7/stackly.git`.
- **PyPI package renamed:** was never published under `debugbridge`; fresh `stackly` release.

### Internal

- All internal `.github/`, `CI`, `tests/`, `scripts/`, `CONTRIBUTING.md`, `README.md`, and live planning docs rewritten to reference Stackly. Phase 1 and Phase 2a archival planning docs preserved verbatim as historical record.

### No functional code changes in this release.

## 0.2.0 -- Fix-loop MVP (Phase 2a)

### Added
- `debugbridge fix` command -- hand-off (interactive) and autonomous (`--auto`) modes
- `detach_process` MCP tool -- releases the target process without stopping the server
- `debugbridge doctor` now checks for `claude` CLI and bypass-permission acknowledgement
- `fix/` subpackage: crash capture via MCP, briefing generator, git worktree isolation,
  Claude Code headless subprocess wrapper, build/test runner, patch writer, retry-feedback
  loop, signal handlers, cost tracking
- Architecture constraint enforcement: CI grep step + unit test block `DebugSession` imports in `fix/`

## 0.1.0 -- MCP Crash Capture (Phase 1)

### Added
- 8 MCP tools: attach_process, get_exception, get_callstack, get_threads, get_locals,
  set_breakpoint, step_next, continue_execution
- Streamable HTTP + stdio transport via FastMCP
- `debugbridge serve`, `debugbridge doctor`, `debugbridge version` CLI
- DebugSession pybag wrapper with lazy imports and thread-safe locking
- Test crash_app C++ fixture with null/stack/throw/wait modes
