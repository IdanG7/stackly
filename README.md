# Stackly

Remote crash capture for Claude Code, Cursor, and Claude Desktop. Expose live Windows debugger state as MCP tools, and run an autonomous AI fix-loop on remote crashes.

[![CI](https://github.com/IdanG7/stackly/actions/workflows/ci.yml/badge.svg)](https://github.com/IdanG7/stackly/actions/workflows/ci.yml)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)
![Platform: Windows 10/11](https://img.shields.io/badge/platform-Windows%2010%2F11-informational)

> **Alpha** — API stable, not yet on PyPI (clone to install). MIT-licensed. Windows 10/11 only for now; Linux/macOS/Unity are on the roadmap (Phase 3).

[![Watch the 60s demo](docs/demo-thumb.png)](https://youtu.be/XXXX)

## Why Stackly

When a C/C++ application crashes on a remote test machine, no AI coding tool (Claude Code, Cursor, Copilot) can see the process. Developers copy-paste stack traces by hand, losing 30–60 minutes per crash. Stackly runs an MCP server on the dev machine that attaches to the remote process via Windows's built-in `dbgsrv.exe`, exposing debugger state as MCP tools. The AI can now read the crash directly.

No other tool combines remote debugger capture, MCP exposure, and an autonomous repair agent in one flow.

## Architecture

```text
    TEST MACHINE                              DEV MACHINE
┌──────────────────┐                    ┌─────────────────────────────┐
│  Your C/C++ app  │                    │   stackly MCP server    │
│  (crashes)       │ ─── network ────── │   (Python + pybag + MCP)    │
│  dbgsrv.exe      │                    │                             │
│  (one command)   │                    │   Streamable HTTP on :8585  │
└──────────────────┘                    │        │                    │
                                        │        ▼                    │
                                        │   Claude Code / Cursor      │
                                        │                             │
                                        │   stackly fix agent     │
                                        │   (crash → patch → PR)      │
                                        └─────────────────────────────┘
```

## Prerequisites

- Windows 10/11 x64 (dev machine and test machine)
- Python >= 3.11
- [uv](https://docs.astral.sh/uv/) >= 0.5
- [Windows Debugging Tools](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/) (part of the Windows SDK) — required on the dev machine for pybag
- git >= 2.20
- [`claude` CLI](https://docs.claude.com/en/docs/claude-code/getting-started) on PATH (required for the fix-loop agent)

Run `uv run stackly doctor` after installation to verify everything is in place.

## Install

Install from source (PyPI publish is tracked in the roadmap for Phase 2c):

```bash
git clone https://github.com/IdanG7/stackly.git
cd stackly
uv sync
uv run stackly doctor   # verifies prerequisites
```

## Quick start

On a fresh Windows 10/11 box, after installing prerequisites:

1. **On the test machine** — start the debug server:
   ```powershell
   dbgsrv.exe -t tcp:port=5555
   ```

2. **On the dev machine** — verify your setup, then start Stackly:
   ```bash
   uv run stackly doctor
   uv run stackly serve --port 8585
   ```

3. **In your MCP client** — register Stackly (see configs below).

4. Ask the AI to attach and diagnose:
   > "Attach to `myapp.exe` on `tcp:server=192.168.1.10,port=5555` and tell me why it crashed."

## MCP client configuration

### Claude Code

Register the server with the Claude Code CLI:

```bash
claude mcp add stackly --transport http http://localhost:8585/mcp
```

If the CLI syntax on your version differs, you can use the equivalent JSON config shape:

```json
{"mcpServers": {"stackly": {"url": "http://localhost:8585/mcp"}}}
```

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{"mcpServers": {"stackly": {"url": "http://localhost:8585/mcp"}}}
```

### Cursor

Edit `.cursor/mcp.json` in your project (or the global Cursor config):

```json
{"mcpServers": {"stackly": {"url": "http://localhost:8585/mcp"}}}
```

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
| `detach_process` | Releases the target process without stopping the server |
| `watch_for_crash` | Block until the attached target throws a break-worthy exception |

## Watch mode

`stackly watch --pid <N> --repo <path>` attaches to a live Windows process and blocks until that process throws, then auto-dispatches the fix agent. The server-side polling loop runs at a 1-second floor (pybag granularity); Ctrl-C cleanly detaches and exits 130.

```bash
# Wait for a crash, then open Claude Code with the briefing preloaded
uv run stackly watch --pid 12345 --repo D:/myapp

# Wait for a crash, then produce a validated .patch headless
uv run stackly watch --pid 12345 --repo D:/myapp --auto \
    --build-cmd "cmake --build build" --test-cmd "ctest --test-dir build"

# Stay resident: handle up to 3 crashes, with a 30-min deadline
uv run stackly watch --pid 12345 --repo D:/myapp --max-crashes 3 --max-wait-minutes 30
```

## Fix-loop agent

Autonomous crash-fix pipeline. Captures crash state via MCP, generates a fix with Claude Code, validates with your build command, and emits a `.patch` file.

### Hand-off mode (interactive)

```bash
uv run stackly fix --pid <PID> --repo D:/myapp
```

Opens an interactive Claude Code session with the crash briefing preloaded.

### Autonomous mode

```bash
uv run stackly fix --pid <PID> --repo D:/myapp \
    --auto \
    --build-cmd "cmake --build build --config Debug" \
    --test-cmd "ctest" \
    --max-attempts 3
```

Runs headless. Produces `.stackly/patches/crash-<hash>.patch` on success.

> Hand-off mode is the default; `--auto` runs headless and should only be used after you've dogfooded the loop.

## How it compares

Existing tools cover pieces of the crash-fix workflow, but none cover all three:

- **CrashReporter / WER / Breakpad** — capture crash dumps, but there's no live debugger, no MCP surface, and no repair step.
- **Sentry / Rollbar / Bugsnag** — telemetry and aggregation after the fact, not a live debugger session you can step through.
- **Claude Code / Cursor on your dev box** — excellent at editing code, but can't see a process running on a remote test rig.

Stackly covers all three: remote debugger capture, MCP exposure to the AI client, and an autonomous repair agent that writes patches back.

## Troubleshooting

- **`stackly doctor` reports pybag missing.** Install the [Windows Debugging Tools](https://learn.microsoft.com/en-us/windows-hardware/drivers/debugger/) (part of the Windows SDK). pybag links against DbgEng.dll from that install.
- **Symbols aren't resolved in the call stack.** Set `_NT_SYMBOL_PATH` before starting the server, e.g. `srv*C:\Symbols*https://msdl.microsoft.com/download/symbols`.
- **Port 8585 is already in use.** Pass `--port N` to `stackly serve` and update the URL in your MCP client config.
- **`claude` command not found.** Install the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code/getting-started) and make sure it's on your PATH before running `stackly fix`.
- **Attach fails with access denied.** Run the Stackly process elevated (same or higher privilege level than the target process).

## Development

For development setup, testing, and PR process, see [CONTRIBUTING.md](./CONTRIBUTING.md).

## Links

- Landing page: https://stackly.dev
- [GitHub Issues](https://github.com/IdanG7/stackly/issues)
- [GitHub Discussions](https://github.com/IdanG7/stackly/discussions)
- [CHANGELOG](./CHANGELOG.md)
- [CONTRIBUTING](./CONTRIBUTING.md)
- [LICENSE](./LICENSE)

## License

MIT — see [LICENSE](LICENSE).
