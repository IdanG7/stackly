# Changelog

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
