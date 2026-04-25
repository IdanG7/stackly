# Stackly Roadmap

## Milestone 1 — Crash capture & autonomous repair for Windows C++

Deliver an MCP server and a paired fix agent that catch Windows native crashes and produce validated patches/PRs.

### Phase 1 — MCP crash capture ✅ COMPLETE (2026-04-16)

**Goal:** Any MCP-compatible AI client can attach to a running Windows process (local or remote) and read the debugger-level state.

**Delivered:** 8 MCP tools, HTTP + stdio transports, CLI (`serve`/`doctor`/`version`), 22 tests passing, commit `d514fb4` on `main`.

**Exit evidence:** `scripts/e2e_smoke.py` proves `list_tools` + `attach_process` + `get_threads` + `get_callstack` work end-to-end over MCP against a live C++ process.

### Phase 2a — Fix-loop MVP (current focus)

**Goal:** A developer can run `stackly fix --pid N --repo PATH` on a crashed process and get back either (a) an interactive Claude Code session preloaded with crash context, or (b) a validated `.patch` file ready to apply.

**Acceptance criteria (draft — to be refined in PLAN.md):**
- `stackly fix` CLI command exists with `--pid`, `--repo`, `--conn-str`, `--build-cmd`, `--test-cmd`, `--auto` flags
- **Hand-off mode (primary):** Captures crash into a briefing file, opens an interactive Claude Code session with the briefing loaded and Stackly MCP configured
- **Autonomous mode (secondary, `--auto`):** Launches Claude Code headless (`claude -p`), isolated in a git worktree under `.stackly/wt-<hash>/`, produces a `.patch` file in `.stackly/patches/`
- User-provided `--build-cmd` / `--test-cmd` run inside the worktree after Claude Code proposes a fix; if they fail, agent retries once (hard cap 3 total attempts) with error output fed back
- Agent speaks MCP to the `stackly serve` server (dog-fooded); never calls `DebugSession` directly
- Repo path defaults to cwd; explicit `--repo` overrides
- All writes live in `.stackly/` subdirectory of the repo — user's working tree untouched until they apply the patch

**Explicitly NOT in 2a:**
- Crash auto-detection (polling or event-driven) → Phase 2.5
- PyPI publish → Phase 2c
- Public open-source launch → Phase 2b
- Non-Windows runtimes → Phase 3

### Phase 2b — Public launch

**Goal:** Stackly is discoverable and installable by strangers on the internet.

**Scope:**
- README rewrite for public audience (currently developer-internal)
- Landing page at stackly.dev (static Vercel or similar)
- 60-second demo video (OBS, remote crash → Claude Code diagnoses → fix lands)
- Submit to MCP directories (Smithery, PulseMCP, LobeHub, Anthropic's registry)
- HN / Reddit / Twitter launch post
- Open-source announcement (MIT license is already in place)

### Phase 2c — PyPI + onboarding polish

**Goal:** `pip install stackly` works on a clean Windows machine and the first `stackly serve` either succeeds or prints clear install guidance.

**Scope:**
- TestPyPI publish dry-run
- Real PyPI publish
- Post-install hook or first-run check that validates Windows Debugging Tools presence, points to `stackly doctor` if missing
- Signed wheels (optional stretch)
- Docs site with Quickstart, MCP client configs, troubleshooting

### Phase 2.5 — Crash auto-detection ✅ COMPLETE (2026-04-24)

**Goal:** Stackly watches the attached process and triggers the fix agent automatically when a crash fires.

**Delivered:** `stackly watch --pid N --repo PATH` CLI + `watch_for_crash` MCP tool (10th), `WatchResult` discriminated union, `DebugSession.wait_for_exception` polling method with synthetic-code filter, `watch/dispatcher.py` orchestrator with signal handlers + stay-resident dedup + re-attach-failure exit. Version bumped to 0.2.5. 115 non-integration tests passing; Wave 0 derisks (4) + E2E test all green on Windows + pybag 2.2.16. Codex review P1 + P2 fixes applied (poll-seconds wired through; SIGINT detach moved to fresh thread to avoid nested asyncio.run).

**Exit evidence:** `tests/test_watch_e2e.py::test_watch_dispatches_run_handoff_against_live_server` proves `crash_app` → live `stackly serve` → `watch_for_crash` → `run_handoff` round-trip end-to-end (< 3 s).

**Known tech debt:** client-side dedup in stay-resident mode (`--max-crashes >1`) computes `compute_crash_hash` on an empty-callstack stub — dedups reliably only when both paths see the same empty-callstack shape. Stay-resident is a low-traffic path; full callstack-based dedup is deferred.

**Deferred out of scope (reaffirmed):** `pybag.dbgeng.callbacks.EventHandler` callback detection (deadlock risk — RESEARCH.md §2.4); Windows AeDebug JIT registry integration; multi-PID daemon mode.

### Phase 3 — Runtime adapter expansion

**Goal:** Beyond Windows C/C++ — Unity C#, Python, GDB/MI for Linux.

**Scope (each is its own phase):**
- 3a. Unity C# via Mono soft debugger wire protocol, `.unitypackage` distribution
- 3b. Python via debugpy/DAP — thin integration, DAP is native
- 3c. Linux C/C++ via GDB/MI — opens embedded/automotive/defense segments

### Phase 4 — Monetization + enterprise

**Goal:** Revenue, enterprise pipeline, 5 runtime adapters live.

**Scope:**
- Cloud relay (outbound-connecting remote agents, team-wide access)
- Paid tier ($15/seat/month, team session history, unlimited devices)
- Enterprise outreach (game studios, embedded shops)
- Batch API integration for non-urgent crashes (50% cost reduction)
