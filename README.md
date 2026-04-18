# DebugBridge

Remote crash capture MCP server for native Windows applications. Exposes live DbgEng debugger state (call stack, exception info, threads, locals) from local or remote processes to MCP-compatible AI clients like Claude Code, Cursor, and Claude Desktop.

> **Status:** Phase 2a (Fix-loop MVP) -- in active development. Not yet published to PyPI.

## Why

When a C/C++ application crashes on a remote test machine, no AI coding tool (Claude Code, Cursor, Copilot) can see the process. Developers copy-paste stack traces by hand, losing 30–60 minutes per crash. DebugBridge runs an MCP server on the dev machine that attaches to the remote process via Windows's built-in `dbgsrv.exe`, exposing debugger state as MCP tools. The AI can now read the crash directly.

## Install

```bash
uv pip install debugbridge   # not yet on PyPI; clone and `uv sync` for now
```

**Prerequisite:** [Windows Debugging Tools](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/) (part of the Windows SDK). Run `debugbridge doctor` to verify.

## Quick start

1. **On the test machine** — start the debug server:
   ```powershell
   dbgsrv.exe -t tcp:port=5555
   ```

2. **On the dev machine** — start DebugBridge:
   ```bash
   debugbridge serve --port 8585
   ```

3. **In your MCP client** — register DebugBridge:
   - Claude Desktop / Code → `%APPDATA%\Claude\claude_desktop_config.json`:
     ```json
     {"mcpServers": {"debugbridge": {"url": "http://localhost:8585/mcp"}}}
     ```
   - Cursor → `.cursor/mcp.json`:
     ```json
     {"mcpServers": {"debugbridge": {"url": "http://localhost:8585/mcp"}}}
     ```

4. Ask the AI to attach and diagnose:
   > "Attach to `myapp.exe` on `tcp:server=192.168.1.10,port=5555` and tell me why it crashed."

## Tools exposed

| Tool | Purpose |
|------|---------|
| `attach_process` | Attach to a local or remote process by PID or name |
| `get_exception` | Read the current exception / crash info |
| `get_callstack` | Full call stack with file paths and line numbers |
| `get_threads` | Enumerate all threads with their states |
| `get_locals` | Local variables for a given stack frame |
| `set_breakpoint` | Set a breakpoint at `file:line` or `module!symbol` |
| `step_next` | Step over one line |
| `continue_execution` | Resume the process |

## Fix-loop agent (Phase 2a)

Autonomous crash-fix pipeline. Captures crash state via MCP, generates a fix
with Claude Code, validates with your build command, and emits a `.patch` file.

### Hand-off mode (interactive)

```bash
debugbridge fix --pid <PID> --repo D:/myapp
```

Opens an interactive Claude Code session with the crash briefing preloaded.

### Autonomous mode

```bash
debugbridge fix --pid <PID> --repo D:/myapp \
    --auto \
    --build-cmd "cmake --build build --config Debug" \
    --test-cmd "ctest" \
    --max-attempts 3
```

Runs headless. Produces `.debugbridge/patches/crash-<hash>.patch` on success.

**Prerequisites:** `claude` CLI on PATH. Run `debugbridge doctor` to verify.

## Development

```bash
git clone <this repo>
cd BridgeIt
uv sync --all-extras
uv run pytest -m "not integration"  # unit tests only
```

Integration tests require Windows Debugging Tools installed and `PYBAG_INTEGRATION=1`:
```powershell
$env:PYBAG_INTEGRATION = "1"
uv run pytest
```

## License

MIT — see [LICENSE](LICENSE).
