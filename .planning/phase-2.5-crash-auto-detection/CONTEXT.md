# Phase 2.5: Crash auto-detection - Context

**Gathered:** 2026-04-23
**Status:** Ready for planning
**Derived from:** ROADMAP.md (Phase 2.5 scope), PROJECT.md (pybag threading constraints), phase-2a-fix-loop-mvp/GOAL.md + PLAN.md + RESEARCH.md (established architecture)

<domain>
## Phase Boundary

Stackly watches an attached live Windows process and **automatically triggers the Phase 2a fix pipeline when a crash fires** — closing the manual-invocation gap where today a developer must notice a crash and run `stackly fix --pid N` by hand.

**In scope:**
- A new `stackly watch --pid N --repo PATH` CLI command (per-PID, one-shot by default).
- A new MCP tool on `stackly serve` that blocks until an exception event fires.
- On crash: hand off directly into the existing Phase 2a dispatcher (`run_handoff` or `run_autonomous`) — no re-implementation of capture/briefing/worktree logic.
- Clean signal handling and debugger detach matching 2a's patterns.

**Explicitly out of scope (deferred to later phases):**
- Multi-PID daemon mode (one-process-per-watcher in 2.5).
- Windows AeDebug JIT postmortem registry integration (admin-required, separate UX).
- Auto-spawn-and-watch (launching the target under the watcher, rather than attaching to an existing PID).
- ETW event subscription as an alternative detection channel.

</domain>

<decisions>
## Implementation Decisions

### Detection mechanism
- **Primary path: polling loop on `dbg.wait(timeout_ms=500)`** driven by a background worker thread. PROJECT.md §Key technical constraints pins pybag as polling-based (not push-callback) — this is the documented, working path.
- **Secondary/investigation: `pybag.dbgeng.callbacks.EventHandler`** for exception events. RESEARCH.md phase for 2.5 must empirically confirm whether EventHandler fires reliably on pybag 2.2.16 before committing. Default assumption: callbacks are NOT reliable; polling wins.
- **Status check after each tick:** inspect DbgEng execution status; if `STATUS_BREAK` and last event is an exception (`.lastevent`), trigger dispatch. If process exited (`STATUS_NO_DEBUGGEE`), exit the watch loop cleanly.
- **AeDebug JIT registry integration:** DEFERRED to a later phase — admin elevation, system-wide side effects, and distinct UX make it its own scope.

### Invocation model
- **`stackly watch --pid N --repo PATH`** — per-PID command, mirrors `stackly fix` exactly.
- **Shared flags with `fix`:** `--host`, `--port`, `--conn-str`, `--build-cmd`, `--test-cmd`, `--auto`, `--model`, `--max-attempts`. Rationale: on crash, `watch` calls straight into the 2a dispatcher — same flags, same semantics.
- **One-shot by default:** after the first crash fires and dispatch completes, `watch` exits. DbgEng/pybag state after an exception is usually unreliable for continued watching.
- **`--max-crashes N` flag** (default 1) for stay-resident mode. Watch detaches + re-attaches between crashes rather than trying to continue after an exception.
- **No daemon / multi-PID mode in 2.5** — deferred.

### Trigger action on crash
- **Dog-food 2a: re-use the existing dispatcher.** When the watch loop detects a crash, it calls `stackly.fix.dispatcher.run_handoff(...)` (default) or `run_autonomous(...)` (`--auto`) directly. No duplicated briefing/worktree/patch logic.
- **Same `.stackly/` layout as 2a.** Briefings, worktrees, patches land in the same subdirectories — `watch` and `fix` produce identical artifacts. A user can't tell from the output whether the fix was manually triggered or auto-triggered.
- **Hand-off vs autonomous selection** follows the `--auto` flag exactly as in 2a.

### MCP surface (new tool)
- **Add `watch_for_crash(pid: int, poll_ms: int = 500, timeout_s: int | None = None) -> ExceptionInfo`** as a 10th MCP tool. Blocks server-side on the polling loop, returns exception info when an exception event fires, or raises a timeout error.
- **Why add a new tool rather than drive the wait loop locally:** GOAL.md §Architecture decision 1 pins agent↔server coupling as strictly MCP. `watch` must be an MCP client, same as `fix`. If the wait loop ran in the CLI locally (bypassing MCP), it would reach into `DebugSession` directly and violate the dog-food constraint.
- **Shares the attach.** `watch` attaches once via `attach_process`, blocks on `watch_for_crash`, and on return reuses the same MCP session to call `get_exception`/`get_callstack`/`get_threads`/`get_locals` — no re-attach. This avoids DbgEng thread-affinity churn and matches 2a's capture sequence.

### Lifecycle & signal handling
- **Process exits cleanly:** watcher logs "process exited normally (exit code N)", detaches via `detach_process` (added in 2a.0.1), exits 0.
- **Debugger detach or pybag error during wait:** log, attempt clean detach, exit non-zero with diagnostic.
- **Ctrl-C during wait:** re-use 2a's signal-handler pattern (SIGINT + SIGBREAK on Windows). Terminate any in-flight Claude Code child (if crash already fired and dispatch is running), detach pybag, exit 130.
- **Ctrl-C during in-flight dispatch:** delegated to 2a's existing handlers — no new machinery.
- **Duplicate-crash dedup (stay-resident mode only):** skip dispatch if the current `crash_hash` matches the previous one within the same `watch` invocation; log "already seen this crash, skipping dispatch". No dedup in one-shot mode.
- **No hard timeout on the wait loop** by default. `--max-wait-minutes N` available but off by default.

### Claude's Discretion
- Exact poll interval (500 ms is a reasonable starting point; empirical tuning during implementation).
- Whether `pybag.dbgeng.callbacks.EventHandler` earns primary status or stays as an opportunistic speedup after research validates it.
- Terminal output format while waiting — Rich status spinner vs quiet. Recommendation: Rich spinner with current wait-tick count; `--quiet` flag to silence.
- Log verbosity per wait tick (presumably silent at INFO; debug logs at DEBUG level).
- How to structure the new MCP tool's errors (timeout, non-exception break, detach during wait) — Pydantic error model consistent with existing tools.
- Whether `watch` can be collapsed into `fix --watch` as a mode flag vs a separate subcommand. Recommendation: separate `watch` subcommand — clearer intent, cleaner `--help`.

</decisions>

<specifics>
## Specific Ideas

- **Architectural wedge preserved:** `watch` → `watch_for_crash` MCP tool → 2a dispatcher. No new "fix" implementation, no bypass of MCP, no second capture path. 2.5 is ~200 lines of orchestration plus one MCP tool.
- **Threading model to respect:** PROJECT.md §Key technical constraints — DbgEng COM is single-threaded; `DebugSession` serializes via `threading.Lock`. The new `watch_for_crash` tool must take the lock for the duration of `dbg.wait()`. While held, other tool calls queue (which is correct — the target is stopped or running under the watch, either way nothing else should talk to it).
- **Reuses `detach_process`** (added in 2a.0.1) for clean release — the server-side gap that 2a already closed.
- **Mirrors 2a's `fix` flag surface** so that `watch` feels like "fix, but waiting for the crash" rather than a separate tool with its own idioms.

</specifics>

<deferred>
## Deferred Ideas

- **Multi-PID daemon mode** (`stackly watchd` watching N processes). Own phase — adds process-tree management, inter-process IPC, log routing.
- **Windows AeDebug JIT registry integration** for postmortem capture of processes that weren't pre-attached. Admin-required, system-wide side effects, distinct UX — warrants its own phase.
- **Auto-spawn-and-watch:** `stackly watch --cmd "my_app.exe --flag"` launches the target under the watcher. Useful but adds process-lifetime management that's orthogonal to attach-and-wait.
- **ETW (Event Tracing for Windows) event subscription** as an alternative detection path — richer event data, no attach needed, but a totally different plumbing stack.
- **Tiered routing (Haiku → Sonnet → Opus) on auto-detected crashes** — Phase 4 cost-optimization item, explicitly deferred in 2a as well.
- **Persistent session resumption via `claude --resume`** across multiple crashes in stay-resident mode — interesting but premature.

</deferred>

---

*Phase: 2.5-crash-auto-detection*
*Context gathered: 2026-04-23*
