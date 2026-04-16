# DebugBridge — Project Brief

**One-line:** An open-source MCP server that captures live debug state from remote Windows processes, plus an autonomous AI agent that reads crash data, generates fixes, and opens PRs — so any MCP-capable AI client (Claude Code, Cursor, Claude Desktop) can see a remote crash directly instead of devs copy-pasting stack traces.

**Why this exists.** When a C/C++ app crashes on a remote test PC, no AI coding tool can see the remote process. Developers spend 30–60 minutes per crash walking over, reading the stack, pasting it into chat, writing a fix, and pushing. Multiply by 5–10 crashes per day across a team and it destroys flow state. DebugBridge kills that loop.

**Wedge:** Full debugger-level state (stack, locals, memory, threads, exception info) from a *remote* process, exposed to AI tools via MCP, with an autonomous repair agent that generates + validates fixes. No competing tool combines live remote debug capture, MCP exposure, local build validation, and PR creation.

---

## Architecture

```
    TEST MACHINE                              DEV MACHINE
┌──────────────────┐                    ┌─────────────────────────────┐
│  Your C/C++ app  │                    │   debugbridge MCP server    │
│  (crashes)       │ ─── network ────── │   (Python + pybag + MCP)    │
│  dbgsrv.exe      │                    │                             │
│  (one command)   │                    │   Streamable HTTP on :8585  │
└──────────────────┘                    │        │                    │
                                        │        ▼                    │
                                        │   Claude Code / Cursor      │
                                        │                             │
                                        │   debugbridge fix agent     │
                                        │   (crash → patch → PR)      │
                                        └─────────────────────────────┘
```

The agent runs on the dev machine (not the test machine) because QA rigs must stay lightweight — they only need `dbgsrv.exe`, which is built into Windows Debugging Tools.

---

## Target users

| Segment | Pain point |
|---------|-----------|
| Game studios (Unity/Unreal) | Crashes on test devices, QA rigs, consoles |
| Embedded / IoT shops | Debugging firmware on remote hardware |
| Desktop app developers | Crashes on customer/tester machines |
| Enterprise C++ teams | Build failures and runtime crashes in CI/staging |
| DevOps / SRE teams | Production crash triage |

---

## Current state (2026-04-16)

**Phase 1 MVP: ✅ SHIPPED** (pushed to https://github.com/IdanG7/bridgeit, commit d514fb4)

What works today:
- 8 MCP tools exposed via `FastMCP` on Streamable HTTP (attach_process, get_exception, get_callstack, get_threads, get_locals, set_breakpoint, step_next, continue_execution)
- `debugbridge` CLI with `serve` / `doctor` / `version`
- Pybag `UserDbg` wrapper in `DebugSession` with per-method threading.Lock
- Lazy pybag import so CLI runs on machines without Windows Debugging Tools installed
- Environment detection + canonical-path PATH injection
- Crash-triage data via WinDbg command parsing (`.lastevent`, `.exr -1`, `kn f`, `dv /t /v`) since pybag's wrappers for `GetLineByOffset` and `GetLastEventInformation` raise E_NOTIMPL
- Test crash_app fixture (C++17, CMake + MSVC, null/stack/throw/wait modes)
- 22 tests passing (18 unit + 4 integration, ~35s)
- End-to-end MCP smoke test (`scripts/e2e_smoke.py`) proves the full HTTP pipeline
- CI workflow (windows-latest runner)

**What Phase 1 deliberately did NOT ship:**
- No autonomous repair agent
- No crash auto-detection (pybag is polling/blocking, not callback-driven)
- No PyPI publish (wheel builds clean, but not released)
- No remote dbgsrv integration test (code path exists, not exercised in CI)
- No non-Windows adapters (Unity/Python/GDB are Phase 3)

---

## Tech stack

| Component | Choice |
|-----------|--------|
| Debug engine | `pybag==2.2.16` (Python wrapper for DbgEng COM) |
| MCP server | `mcp[cli]>=1.27,<1.28` (FastMCP, Streamable HTTP transport) |
| CLI | `typer` + `rich` |
| Data contracts | `pydantic>=2` |
| Tests | `pytest` (integration tests auto-skip without Debugging Tools) |
| Lint/format | `ruff` |
| Types | `pyright` (basic mode, pybag has no stubs) |
| Build/package | `uv` with uv_build backend |
| Python | 3.11+ |
| Target OS (MVP) | Windows 10/11 x64 |

---

## Key technical constraints

- **pybag loads `dbgeng.dll` at import time** → any module touching pybag must be lazy-imported so `doctor`/`version` work without Debugging Tools installed
- **DbgEng COM is single-threaded** → all session methods serialized behind `threading.Lock`
- **Pybag is polling-based** (`dbg.wait()` blocks on a worker thread) — NOT push-callback; event-callback work in Phase 2.5 will require careful threading
- **`GetLineByOffset`, `GetLastEventInformation` not implemented in pybag** → use WinDbg command parsing (stable, documented output)
- **Windows Debugging Tools are a 2GB SDK install** — not a pip dependency; `debugbridge doctor` detects + guides

---

## Cost philosophy (for the agent, Phase 2+)

Target: **< $0.50 per fix attempt** (realistic, not the spec's optimistic $0.08–0.15).

- Tiered routing: Haiku classifies crash type, Sonnet generates fix, Opus only on escalation
- Prompt caching for system prompt + tool definitions (90% discount on repeat input)
- Minimal context: only files referenced in the stack, not the whole repo
- Hard 3-attempt cap — escalate to human on failure
- Local build validation is free (no API cost for retry triggers)

---

## Success metrics

| Metric | Phase 1 | Phase 2 target | Phase 4 target |
|--------|---------|----------------|----------------|
| GitHub stars | 0 | 100 | 2,000 |
| PyPI downloads/month | 0 | 50 | 5,000 |
| Crash-fix success rate | n/a | 50% | 60–70% |
| Mean time crash → PR | n/a | < 5 min | < 2 min |
| Paid subscribers | 0 | 0 | 50 |
