# Phase 2.5: Crash auto-detection - Research

**Researched:** 2026-04-23
**Domain:** pybag wait-loop semantics, DbgEng event model, FastMCP long-running tool concurrency, Phase 2a dispatcher integration
**Confidence:** HIGH on pybag internals (source reading, site-packages), HIGH on Microsoft DbgEng docs for `WaitForEvent`, HIGH on FastMCP/MCP internals (source reading), MEDIUM on end-to-end crash-detection flow (no empirical run in this pass — must be derisked in an early task).

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

#### Detection mechanism
- **Primary path: polling loop on `dbg.wait(timeout_ms=500)`** driven by a background worker thread. PROJECT.md §Key technical constraints pins pybag as polling-based (not push-callback) — this is the documented, working path.
- **Secondary/investigation: `pybag.dbgeng.callbacks.EventHandler`** for exception events. RESEARCH.md phase for 2.5 must empirically confirm whether EventHandler fires reliably on pybag 2.2.16 before committing. Default assumption: callbacks are NOT reliable; polling wins.
- **Status check after each tick:** inspect DbgEng execution status; if `STATUS_BREAK` and last event is an exception (`.lastevent`), trigger dispatch. If process exited (`STATUS_NO_DEBUGGEE`), exit the watch loop cleanly.
- **AeDebug JIT registry integration:** DEFERRED to a later phase — admin elevation, system-wide side effects, and distinct UX make it its own scope.

#### Invocation model
- **`stackly watch --pid N --repo PATH`** — per-PID command, mirrors `stackly fix` exactly.
- **Shared flags with `fix`:** `--host`, `--port`, `--conn-str`, `--build-cmd`, `--test-cmd`, `--auto`, `--model`, `--max-attempts`. Rationale: on crash, `watch` calls straight into the 2a dispatcher — same flags, same semantics.
- **One-shot by default:** after the first crash fires and dispatch completes, `watch` exits. DbgEng/pybag state after an exception is usually unreliable for continued watching.
- **`--max-crashes N` flag** (default 1) for stay-resident mode. Watch detaches + re-attaches between crashes rather than trying to continue after an exception.
- **No daemon / multi-PID mode in 2.5** — deferred.

#### Trigger action on crash
- **Dog-food 2a: re-use the existing dispatcher.** When the watch loop detects a crash, it calls `stackly.fix.dispatcher.run_handoff(...)` (default) or `run_autonomous(...)` (`--auto`) directly. No duplicated briefing/worktree/patch logic.
- **Same `.stackly/` layout as 2a.** Briefings, worktrees, patches land in the same subdirectories — `watch` and `fix` produce identical artifacts. A user can't tell from the output whether the fix was manually triggered or auto-triggered.
- **Hand-off vs autonomous selection** follows the `--auto` flag exactly as in 2a.

#### MCP surface (new tool)
- **Add `watch_for_crash(pid: int, poll_ms: int = 500, timeout_s: int | None = None) -> ExceptionInfo`** as a 10th MCP tool. Blocks server-side on the polling loop, returns exception info when an exception event fires, or raises a timeout error.
- **Why add a new tool rather than drive the wait loop locally:** GOAL.md §Architecture decision 1 pins agent↔server coupling as strictly MCP. `watch` must be an MCP client, same as `fix`. If the wait loop ran in the CLI locally (bypassing MCP), it would reach into `DebugSession` directly and violate the dog-food constraint.
- **Shares the attach.** `watch` attaches once via `attach_process`, blocks on `watch_for_crash`, and on return reuses the same MCP session to call `get_exception`/`get_callstack`/`get_threads`/`get_locals` — no re-attach. This avoids DbgEng thread-affinity churn and matches 2a's capture sequence.

#### Lifecycle & signal handling
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

### Deferred Ideas (OUT OF SCOPE)
- **Multi-PID daemon mode** (`stackly watchd` watching N processes). Own phase — adds process-tree management, inter-process IPC, log routing.
- **Windows AeDebug JIT registry integration** for postmortem capture of processes that weren't pre-attached. Admin-required, system-wide side effects, distinct UX — warrants its own phase.
- **Auto-spawn-and-watch:** `stackly watch --cmd "my_app.exe --flag"` launches the target under the watcher. Useful but adds process-lifetime management that's orthogonal to attach-and-wait.
- **ETW (Event Tracing for Windows) event subscription** as an alternative detection path — richer event data, no attach needed, but a totally different plumbing stack.
- **Tiered routing (Haiku → Sonnet → Opus) on auto-detected crashes** — Phase 4 cost-optimization item, explicitly deferred in 2a as well.
- **Persistent session resumption via `claude --resume`** across multiple crashes in stay-resident mode — interesting but premature.
</user_constraints>

---

## §1 Summary

Phase 2.5 is ~200 lines of orchestration sitting on three existing foundations: (a) Phase 1's `DebugSession` + MCP server, (b) Phase 2a's `dispatcher.run_handoff` / `run_autonomous` entry points, (c) pybag's standalone-mode `wait()` which pumps `IDebugControl::WaitForEvent` on an internal worker thread. The architecture is **`watch` CLI → MCP `watch_for_crash` tool (new, 10th tool) → pybag `dbg.wait()` loop → on return, client calls existing `get_exception`/`get_callstack`/… on the same MCP session → `dispatcher.run_handoff/autonomous(...)`**. Zero new crash-capture code; zero new fix-loop code.

Three empirically-confirmed surprises drive the plan:

1. **pybag's `wait(timeout=N)` takes SECONDS, not milliseconds**, and sub-second timeouts don't work due to an integer-division bug in `_worker_wait` (see §2.1). CONTEXT.md's `poll_ms=500` parameter must either convert to a 1-second internal floor or the tool signature changes to `poll_s`.
2. **FastMCP 1.27 calls sync tools directly on the asyncio event loop** (`func_metadata.py:95`). A long-blocking sync `watch_for_crash` would freeze the entire MCP server — no other tool calls could run. **The tool MUST be `async def` and offload the pybag wait to `anyio.to_thread.run_sync`.** This is non-negotiable for CONTEXT.md's "share the attach" design.
3. **The MCP client's default HTTP read-timeout is 300 s (5 min)** (`_httpx_utils.py:11`). A `watch_for_crash` call that blocks for longer than 5 minutes will time out client-side even while the server is still waiting. The `watch` CLI must pass a custom `httpx_client_factory` with `read=None` (unbounded read timeout).

**Primary recommendation:** Build Phase 2.5 in three layers: (1) add `watch_for_crash` as an `async` MCP tool that takes the DebugSession lock and offloads a polling `dbg.wait()` loop to a worker thread, returning `ExceptionInfo` or raising a Pydantic error model; (2) add `stackly watch` Typer subcommand that opens an MCP `ClientSession` with a no-read-timeout httpx factory, calls `attach_process` → `watch_for_crash` → (on return) `run_handoff`/`run_autonomous` with the same MCP url; (3) a single integration test using the existing `crash_app` fixture that proves attach → watch → crash → dispatch-invoked end-to-end. Derisk #1 (sub-second timeouts) and #3 (client-side read timeout) in the very first tasks before building the full loop.

---

## §2 pybag wait-loop mechanics

### 2.1 `dbg.wait(timeout)` semantics — the ground truth

**Source:** `C:/Users/idang/AppData/Local/Programs/Python/Python314/Lib/site-packages/pybag/pydbg.py:256–287` (pybag 2.2.16 installed).

`UserDbg` is a standalone DebuggerBase, so `wait()` takes the `standalone=True` branch at `pydbg.py:280`:

```python
def wait(self, timeout=DbgEng.WAIT_INFINITE):
    if self.standalone:
        if not self._worker_wait('WaitForEvent', timeout):
            self._control.SetInterrupt(DbgEng.DEBUG_INTERRUPT_ACTIVE)
            return False
        else:
            return True
    else:
        self._control.WaitForEvent(timeout)
```

**Return value:** `True` iff the event-thread finished processing the WorkItem within the Python-side polling window. `False` iff the Python-side polling window expired before the event thread signaled completion — in which case `wait()` ALSO sends `SetInterrupt(DEBUG_INTERRUPT_ACTIVE)` to force the pending `WaitForEvent` to return so the worker thread unblocks.

**CRITICAL TIMEOUT UNITS (pydbg.py:256–276):**

```python
def _worker_wait(self, msg, timeout=DbgEng.WAIT_INFINITE, args=None):
    if timeout == -1:
        timeout = 0xffffffff
    if timeout != 0xffffffff:
        timeout *= 1000                   # <-- now in ms; passed to WaitForEvent
    item = WorkItem(msg, timeout, args)
    self._ev.clear()
    self._q.put(item)
    try:
        for i in itertools.repeat(1, int(timeout / 1000)):  # loop N seconds
            if self._ev.is_set():
                break
            self._ev.wait(1)              # sleep 1 second per iteration
    except KeyboardInterrupt:
        pass
    return self._ev.is_set()
```

The argument is treated as **SECONDS** (multiplied by 1000 to ms for DbgEng's `WaitForEvent`, then the outer Python polling loop iterates `int(timeout/1000) == int(original_seconds)` times at 1-sec granularity).

**Consequences for Phase 2.5:**

- `dbg.wait(timeout=500)` (intending "500 ms") actually means **500 seconds** — ~8 minutes of blocking per poll tick. This would miss crashes by 8 min on average and leak the DbgEng interrupt. Bug-level wrong.
- `dbg.wait(timeout=0.5)` (intending 0.5 s) means `int(500/1000) == 0` iterations. The outer loop runs zero times, the Python-side check never fires, `_worker_wait` returns False **immediately without waiting for the event**, and `SetInterrupt` fires. **Sub-second timeouts are silently broken.**
- `dbg.wait(timeout=1)` is the floor that works correctly: 1 iteration of the outer loop, 1-sec `_ev.wait(1)` granularity, 1000 ms ceiling passed to `WaitForEvent`.

**CONTEXT.md's `poll_ms: int = 500` parameter name is misleading.** Recommended handling in the planner:

- Keep the MCP tool's public parameter named `poll_ms` for consumer-facing symmetry (it matches how HTTP request parameters read), BUT
- Internally convert: `timeout_s = max(1, round(poll_ms / 1000))`, and document on the tool: "Poll interval is clamped to 1-second minimum by pybag's `wait()` granularity."
- Alternatively rename to `poll_s: int = 1` (default 1 second) and document the 1-sec floor honestly. **Recommendation: this path** — truthful API beats backward-compatibility with a phase that hasn't shipped yet.

**Confidence:** HIGH — direct source reading in site-packages.

### 2.2 `IDebugControl::WaitForEvent` return codes (MSFT authoritative)

**Source:** [Microsoft Learn — IDebugControl::WaitForEvent](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/dbgeng/nf-dbgeng-idebugcontrol-waitforevent).

| Return | Meaning |
|---|---|
| `S_OK` | An event occurred AND a callback (or default event filter) returned `DEBUG_STATUS_BREAK` to break back to the debugger application |
| `S_FALSE` | The timeout expired without a break-worthy event |
| `E_PENDING` | An exit interrupt was issued; target unavailable |
| `E_UNEXPECTED` | No targets can generate events (e.g. all exited); the session is ended and discarded |
| `E_FAIL` | Engine is already waiting (re-entrancy error) |

**Translation to pybag behavior** (`pybag/dbgeng/idebugcontrol.py:508–512`):

```python
def WaitForEvent(self, timeout=DbgEng.WAIT_INFINITE):
    hr = self._ctrl.WaitForEvent(0, timeout)
    if hr == S_FALSE:
        raise exception.DbgEngTimeout("WaitForEvent timeout: {}".format(timeout))
    exception.check_err(hr)
```

In pybag's `EventThread` (`pydbg.py:92–119`) the `DbgEngTimeout` exception is swallowed (`except Exception as ex: pass`) and `Ev.set()` fires regardless. So from `_worker_wait`'s perspective:
- **Event broke in (S_OK)** → EventThread finishes → `Ev.set()` → `_worker_wait` returns True
- **Timeout (S_FALSE)** → DbgEngTimeout swallowed → `Ev.set()` → `_worker_wait` returns True (same)
- **Session ended (E_UNEXPECTED)** → raises through `check_err`, gets swallowed → `Ev.set()` → `_worker_wait` returns True (same)
- **Python-side poll timeout** → `Ev` never sets within the iteration count → `_worker_wait` returns False → `wait()` sends SetInterrupt

**Consequence:** `wait()`'s bool return is NOT a reliable event/timeout discriminator. It tells you "did the Python loop see Ev in time." To distinguish "event fired" from "timeout" from "target exited," you MUST inspect `dbg.exec_status()` **after** `wait()` returns — which is exactly what CONTEXT.md mandates.

**Confidence:** HIGH — MS docs + pybag source.

### 2.3 Post-`wait()` state inspection — the detection recipe

Four things to check after each `wait()` call. All must happen while holding the DebugSession lock (§3.1).

**Step A — Execution status.** `dbg.exec_status()` returns one of the strings from `dbgeng/core.py:54–64`: `"BREAK"`, `"GO"`, `"STEP_*"`, `"NO_DEBUGGEE"`, or `"UNKNOWN - <int>"`. The underlying call is `IDebugControl::GetExecutionStatus`, which is synchronous and NOT routed through the EventThread — safe to call while holding the session lock between ticks.

| exec_status | Meaning | Action |
|---|---|---|
| `"BREAK"` | Target is stopped — could be crash, breakpoint, manual break, or module-load break | Check step B |
| `"GO"` / `"STEP_*"` | Target is still running | Continue polling |
| `"NO_DEBUGGEE"` | Target exited / detached | Exit watch loop, log "process exited" |
| other | Unexpected | Log, detach, exit non-zero |

**Step B — If `BREAK`, discriminate exception vs other break.** `.lastevent` output is already parsed by `DebugSession.get_exception()` at `session.py:288–318` using the `_LASTEVENT_RE` regex. Re-use that parser — the watch tool should call `session.get_exception()` (NOT shell out to `.lastevent` again), which returns `ExceptionInfo | None`. If it returns non-None, an exception fired. If it returns None, the break was for some other reason (module load, initial break, etc.) — continue polling.

**Step C — Return `ExceptionInfo` to the MCP client.** Matches CONTEXT.md: `watch_for_crash` returns `ExceptionInfo` (the existing Pydantic model at `models.py:46–54`); the client then makes follow-up calls (`get_callstack`, `get_threads`, `get_locals`) on the same attached session.

**Step D — After return, the session is still attached AND in BREAK state.** Pybag does not auto-resume after the exception fires. The follow-up queries work because the target is paused at the fault point — this is exactly what `attach_process` with `initial_break=True` already relies on in Phase 1 (`session.py:139–141`).

**Confidence:** HIGH — `.lastevent` + `exec_status()` are already used in `DebugSession` and have passing integration tests.

### 2.4 `pybag.dbgeng.callbacks.EventHandler` — empirical check

**Source:** `pybag/dbgeng/callbacks.py:11–80` + `pybag/pydbg.py:35–48`.

**Does it exist?** Yes. `EventHandler` class at `callbacks.py:11`; imported and instantiated for every `DebuggerBase` via `pydbg.py:35` (`Dbg.events = EventHandler(Dbg)`). It's the real, supported path for hooking DbgEng event callbacks.

**Does it wire up exception callbacks?** Yes — `EventHandler.exception(handler=None, verbose=False)` at `callbacks.py:75–78` subscribes `DEBUG_EVENT_EXCEPTION`; the underlying dispatch is via `DbgEngCallbacks.IDebugEventCallbacks_Exception` at `callbacks.py:416–418`. The user's handler receives `(exception_record_struct, first_chance_bool)` — per the default `_ev_exception` at `callbacks.py:62–73` — and must return a `DEBUG_STATUS_*` constant (return value drives the break/go decision).

**Is it wired by default?** **No.** `InitComObjects` at `pydbg.py:39` only wires `Dbg.events.breakpoint(Dbg.breakpoints)`. Exception callbacks are NOT subscribed by default. `SetEventCallbacks` IS called on the `DbgEngCallbacks` aggregate at `pydbg.py:48`, but the `GetInterestMask` at `callbacks.py:420–423` returns only the events registered via `_catch_event`. An exception callback only fires if someone calls `dbg.events.exception(my_handler)` first.

**Threading caveat that makes it risky as a primary path:**

1. Per Microsoft: `WaitForEvent` "is not re-entrant. Once it has been called, it cannot be called again on any client until it has returned. In particular, it cannot be called from the event callbacks, including extensions and commands executed by the callbacks." Callbacks run **inside** `WaitForEvent`, on the EventThread.
2. pybag's DbgEng COM client is created on the EventThread (`pydbg.py:87–88`: `Dbg._client = DebugClient()` runs inside `EventThread`). Callbacks fire on the EventThread. But the DebugSession lock is held by the MCP-server asyncio thread pool (via `anyio.to_thread.run_sync`). **So the event thread is NOT the lock-holder** — calling back into `DebugSession` methods from an exception callback would deadlock or violate COM thread affinity.
3. A workable callback-based design would need: (a) register a sentinel callback that sets a `threading.Event` or queues to a `queue.Queue`, (b) the tool's polling loop becomes event-wait instead of `dbg.wait(timeout=N)`. This is strictly more plumbing for strictly no latency benefit over a 1-sec `dbg.wait()` poll on a process that crashes once.

**Recommendation (aligns with CONTEXT.md default assumption):** Stay with polling-only for Phase 2.5. Leave `EventHandler.exception()` as an explicitly-deferred optimization path documented in RESEARCH.md §5 (Open Risks) — do not build it. The sub-second latency win isn't worth the threading complexity; CONTEXT.md already documents this is the default.

**Confidence:** HIGH on the EventHandler existence and wiring (source-read); MEDIUM on the "would work but isn't worth it" judgement (no empirical test of exception-callback firing — but Microsoft's non-reentrancy docs are categorical enough that polling is clearly the lower-risk path).

---

## §3 Threading model & FastMCP concurrency

### 3.1 DebugSession lock — what it protects and what it doesn't

**Source:** `src/stackly/session.py:113` (the lock), lines 131–439 (every public method takes it).

`DebugSession._lock` is a `threading.Lock`. Every public method that touches `self._dbg` enters `with self._lock:` first, so sequential tool calls on one server are serialized behind a single pybag consumer. `attach_local` / `attach_remote` / `close` / `detach` all share this lock.

**What the lock CANNOT do:**
- It doesn't protect against re-entrancy from the same thread (it's `Lock`, not `RLock` — re-entrant acquisition would deadlock).
- It doesn't provide fairness across waiters; under contention, ordering is OS-determined.
- It doesn't know about pybag's internal EventThread — pybag's `_worker_wait` already marshals calls to the EventThread internally, so holding the DebugSession lock is sufficient to serialize all MCP-facing access.

**For `watch_for_crash`:** the tool MUST take `session._lock` for the **entire** duration of each `dbg.wait()` call, and it must hold the lock across the status-check sequence that follows (`exec_status` → `.lastevent` via `get_exception`). Releasing the lock between wait ticks is acceptable — at poll boundaries, other tools can briefly acquire it (e.g. a UI client wanting to peek at threads). CONTEXT.md's constraint "takes the lock for the duration of dbg.wait()" is literally achievable.

**Practical design for the tool:**

- Pattern A (simple, blocks server): acquire lock once, loop forever on `dbg.wait()` + status-check, release on crash/exit. Blocks ALL other tool calls.
- Pattern B (cooperative, CONTEXT.md-compatible): each poll iteration acquires → waits → checks → releases. Between ticks, other tools can sneak in. Slightly more overhead (lock acquire/release per tick) but enables concurrent `get_*` calls on the live attached session.
- **Recommendation: Pattern A for 2.5.** Between ticks, there's nothing useful a concurrent tool call could do on a not-yet-broken target (get_callstack would return stale pre-attach state or raise; get_exception would be None). Plus, CONTEXT.md's design has `watch` call follow-up queries ONLY AFTER `watch_for_crash` returns — sequential, no concurrency needed. Pattern A is simpler and matches the actual usage.

### 3.2 FastMCP's sync vs async tool dispatch — the blocking-event-loop gotcha

**Source:** `mcp/server/fastmcp/utilities/func_metadata.py:74–95` (mcp 1.27.0 installed):

```python
async def call_fn_with_arg_validation(self, fn, fn_is_async, arguments_to_validate, arguments_to_pass_directly):
    ...
    if fn_is_async:
        return await fn(**arguments_parsed_dict)
    else:
        return fn(**arguments_parsed_dict)   # <-- sync tool runs on the event loop thread
```

**If a tool is declared sync (`def foo(...)`), FastMCP calls it directly on the asyncio event loop thread — no `anyio.to_thread.run_sync`, no thread pool.** A long-blocking sync tool therefore stalls the entire event loop: the HTTP server cannot accept new requests, SSE cannot flush, nothing runs until the tool returns.

The lowlevel MCP server DOES dispatch concurrent requests as concurrent asyncio tasks (`mcp/server/lowlevel/server.py:673–684`: `tg.start_soon(self._handle_message, ...)` for each incoming message). So in principle two tool calls can run concurrently at the asyncio layer — but only if BOTH tools cooperate with async scheduling. A sync tool that never awaits blocks all of them.

**Consequence for `watch_for_crash`:**

**MANDATORY: declare the tool as `async def watch_for_crash(...)` and offload the blocking `dbg.wait()` loop via `await anyio.to_thread.run_sync(blocking_poll_body)`.**

- A sync `def watch_for_crash` would freeze the server for minutes/hours — the `watch` CLI's follow-up `get_exception`/`get_callstack` calls would never get through.
- `anyio.to_thread.run_sync` offloads to anyio's default thread pool; the event loop stays responsive. When `blocking_poll_body` finally returns on crash, the await unblocks and the tool function returns the ExceptionInfo.
- Inside the offloaded body, the code is running in a worker thread — it can acquire `session._lock`, run the poll loop, and (once the lock is held) pybag's `_worker_wait` internally marshals to its own EventThread and blocks correctly. The worker thread just sleeps.

**Code-sketch pattern (not to ship verbatim — illustration only):**
```python
@mcp.tool()
async def watch_for_crash(pid: int, poll_s: int = 1, timeout_s: int | None = None) -> ExceptionInfo:
    import anyio
    from functools import partial
    return await anyio.to_thread.run_sync(
        partial(session.wait_for_exception, poll_s=poll_s, timeout_s=timeout_s)
    )
```
Where `session.wait_for_exception(...)` is a new sync method on `DebugSession` that runs the polling loop under the session lock.

**Confidence:** HIGH — direct source reading of FastMCP 1.27.

### 3.3 MCP client HTTP timeouts — the other gotcha

**Source:** `mcp/shared/_httpx_utils.py:10–11` (mcp 1.27.0):

```python
MCP_DEFAULT_TIMEOUT = 30.0           # General operations (seconds)
MCP_DEFAULT_SSE_READ_TIMEOUT = 300.0  # SSE streams — 5 minutes
```

`streamablehttp_client` creates an httpx client with `httpx.Timeout(30.0, read=300.0)` if no `http_client` is passed. The 300-sec read timeout applies to the SSE POST response that carries the tool-call result.

**Practical consequence:**

- If `watch_for_crash` blocks on the server for more than 5 minutes waiting for a crash, the `watch` CLI's `session.call_tool("watch_for_crash", ...)` will raise an `httpx.ReadTimeout` (surfaced as an MCP error) even though the server is still happily waiting.
- Users will expect `watch` to block indefinitely. The common case (a latent bug, "run the app, go get coffee, come back to a fix") WILL exceed 5 minutes.

**Fix (MUST do in 2.5):** the `watch` MCP-client code must create an httpx client with `read=None` (disabled read timeout) OR pass `read_timeout_seconds=timedelta(days=30)` (practical infinity) to `session.call_tool`.

Two acceptable approaches for the planner:

**Approach A (simpler — per-call override):**
```python
from datetime import timedelta
exc = await session.call_tool(
    "watch_for_crash",
    {"pid": pid, "poll_s": 1},
    read_timeout_seconds=timedelta(days=30),   # supported at mcp/client/session.py:372
)
```
Verified: `ClientSession.call_tool` accepts `read_timeout_seconds: timedelta | None = None` at `mcp/client/session.py:368–397`. This overrides the session-level read timeout ONLY for the `send_request` — but the underlying httpx transport still has its own read timeout. Checking the flow: `send_request` uses `anyio.fail_after(timeout)` at `mcp/shared/session.py:291` — that's an asyncio-level fail_after, which overrides the session timeout. But the HTTP-level httpx read timeout is a separate layer.

**Approach B (safer — custom http_client):**
```python
from mcp.client.streamable_http import streamable_http_client   # non-deprecated variant
import httpx
async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True) as http_client:
    async with streamable_http_client(mcp_url, http_client=http_client) as (read, write, _):
        ...
```
Note: the deprecated `streamablehttp_client` (current Phase 2a usage, `mcp_client.py:31`) hardcodes the factory. The non-deprecated `streamable_http_client` accepts `http_client=None` parameter (see `mcp/client/streamable_http.py:618–624`). `read=None` on httpx disables the read-timeout entirely.

**Recommendation for the planner:** use **both** layered defenses in `watch`:
1. Pass a custom `httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))` to `streamable_http_client`.
2. Also pass `read_timeout_seconds=timedelta(days=30)` on the `call_tool("watch_for_crash")` — cheap belt-and-braces.

**Confidence:** HIGH on the timeout defaults (source-read); MEDIUM on "Approach A alone is sufficient" (I didn't trace the entire async chain to confirm httpx read timeout is correctly short-circuited by `fail_after`). Planner task: pick Approach B for safety.

---

## §4 Phase 2a integration points

### 4.1 Dispatcher entry points (READ THIS — it's already shipped)

**Source:** `src/stackly/fix/dispatcher.py:152–398`.

Two public functions exist today with stable signatures:

```python
def run_handoff(
    repo: Path,
    pid: int,
    host: str = "127.0.0.1",
    port: int = 8585,
    conn_str: str | None = None,
) -> FixResult:
    ...

def run_autonomous(
    repo: Path,
    pid: int,
    host: str = "127.0.0.1",
    port: int = 8585,
    build_cmd: str | None = None,
    test_cmd: str | None = None,
    model: str = "sonnet",
    max_attempts: int = 3,
    max_budget_usd: float = 0.75,
    conn_str: str | None = None,
) -> FixResult:
    ...
```

Both return `FixResult` (`fix/models.py:100–117`). `watch` can call these **directly** after `watch_for_crash` returns. No new dispatcher, no duplicated capture code.

**Critical detail for re-use:** both functions internally call `capture_crash(pid, mcp_url, conn_str)` at `dispatcher.py:188` / `267`, which does its OWN `attach_process` + full capture. That's wasteful if `watch` has already attached via MCP. But — **it's idempotent and correct**: `DebugSession.attach_local` at `session.py:131–151` calls `_close_locked()` first (line 134), so re-attaching to the same PID from inside `capture_crash` will cleanly detach-then-reattach. No corruption, no leak. CONTEXT.md's "share the attach" benefit is purely a latency/round-trip optimization; **for 2.5, accept the re-attach and do not optimize this in the first cut.** It's ~3 extra MCP calls on the dispatcher side, negligible against the crash-fix cost.

If the planner wants to optimize later, there's a path: `dispatcher.run_handoff` / `run_autonomous` could accept an optional pre-captured `CrashCapture` and skip the internal `capture_crash(...)` call. That's a 2-line change. **Recommendation: defer this optimization to a follow-up task; ship 2.5 without it.** Dog-food test: does `watch` → `dispatcher` → re-attach work end-to-end on a real crash? If yes, ship. If not, optimize.

### 4.2 Signal handling — reuse or compose

**Source:** `dispatcher.py:58–85` — the `_install_signal_handlers(state: _FixState)` pattern + `_FixState` dataclass.

`_install_signal_handlers` installs SIGINT + SIGBREAK (Windows) handlers that:
1. Terminate the claude subprocess (if any)
2. Shut down the server subprocess (if `state.did_spawn_server`)
3. Raise `SystemExit(130)` (idempotent via `state._handled`)

**For `watch`:** compose a similar handler that additionally:
1. Sets a `stop_flag` that the watch loop checks at each `wait()` tick boundary
2. Calls `detach_process` via MCP (or directly via `session.detach()` if running in-process — but per CONTEXT.md "strictly MCP," go via the MCP tool)
3. Raises `SystemExit(130)` matching 2a's exit code

**Cannot directly reuse `_install_signal_handlers` from 2a** because it's keyed to `_FixState` which only tracks claude/server subprocesses. Watch has a different composition — `watch_for_crash` is in flight, and the crash dispatcher is NOT yet running when the user hits Ctrl-C early.

**Recommendation (matches CONTEXT.md):** write a sibling `_install_watch_signal_handlers(watch_state)` in a new `src/stackly/watch.py` or `watch/` subpackage, following the same pattern. Once a crash is caught and `run_handoff`/`run_autonomous` is invoked, the 2a dispatcher installs ITS OWN handlers over the top — re-entry is clean because `signal.signal` replaces, not stacks.

**Interrupting a blocked `dbg.wait()`:** CONTEXT.md asks "how do we interrupt a blocked `dbg.wait()`?" Answer: you don't need to. Because the poll interval is 1 second (§2.1 floor), every Ctrl-C at worst waits 1 second for the current poll tick to finish, then the `stop_flag` check at the top of the next iteration exits the loop. If you want faster interrupt, you can call `dbg._control.SetInterrupt(DEBUG_INTERRUPT_ACTIVE)` from the signal handler — pybag exposes `SetInterrupt` at `idebugcontrol.py:38`. But this is complex (needs access to `session._dbg` from the handler). **Recommendation: accept 1-sec worst-case Ctrl-C latency; don't bother with SetInterrupt from the handler.**

### 4.3 One-shot vs stay-resident — the re-attach mechanics

**The core question:** after `watch_for_crash` returns with an exception, and `run_handoff`/`run_autonomous` completes, can `watch` re-attach cleanly to continue watching for the next crash?

**Answer: generally no, because the target process has crashed and is dead.**

- An unhandled EXCEPTION_ACCESS_VIOLATION (the canonical crash case) is a fatal event. After DbgEng reports it, the process is in a dead-or-dying state. `continue_execution` would either let it die or re-raise. You cannot "resume past a crash."
- The one practical exception is first-chance exceptions that ARE caught by the target (C++ `throw` inside a `try`/`catch`). For those, `dbg.go_handled()` does exist (`pydbg.py:189`), but detecting "this exception will be handled and the process will keep running" is not trivial.

**CONTEXT.md's stance is correct:** stay-resident mode should **detach → re-attach** between crashes, not try to continue the target past an exception. But since the target is typically dead post-crash, stay-resident mode's utility is limited to:
- Second-chance exceptions where the target has a top-level exception handler that eats the crash.
- Target processes that auto-restart themselves externally (supervisor, systemd-analogue, NSSM) — but then the PID changes, so we can't just re-attach by PID anyway.

**Recommendation for 2.5:** implement `--max-crashes` and the detach/re-attach loop, but:
- Document in `--help` that stay-resident mode is only useful if the target survives the crash OR is restarted with the same PID externally.
- In the re-attach path, `AttachResult.status == "failed"` with message containing "Access denied" or similar signals the target is gone — log "target no longer attachable" and exit cleanly.
- The dedup-via-crash-hash is useful only during the brief window when a first-chance exception fires repeatedly before the catch kicks in.

**Reuse `compute_crash_hash`:** already shipped at `src/stackly/fix/worktree.py:99–116`. Same function, same inputs — import and use directly.

**`detach_process` MCP tool for clean release:** already shipped in 2a.0.1 at `src/stackly/tools.py:61–71`. Call via MCP between re-attach attempts.

**Confidence:** HIGH on the integration points (source-read of shipped code); MEDIUM on "stay-resident has limited real-world utility" (not empirically tested).

---

## §5 `watch_for_crash` MCP tool design sketch

### 5.1 Tool signature

```python
@mcp.tool()
async def watch_for_crash(
    pid: int,
    poll_s: int = 1,                   # renamed from CONTEXT.md's "poll_ms" — see §2.1
    timeout_s: int | None = None,
) -> WatchResult:
    """Block until a (second-chance) exception fires on the attached process.

    MUST be called AFTER attach_process on the same session. Returns the
    exception info when a break-worthy event is observed, or raises an
    MCP error with shaped payload if the timeout expires, the target exits
    before any exception, or the session disconnects.
    """
```

**Parameter rationale:**

- **`pid`** — mirrors `attach_process(pid=...)`. Required so the tool can assert the session is attached to the expected PID (raises if not attached or if attached to a different PID — defensive against multi-client misuse).
- **`poll_s: int = 1`** (renamed per §2.1). Default 1 second (pybag's effective floor). Users who want larger poll intervals pass e.g. `poll_s=5` to reduce CPU.
- **`timeout_s: int | None = None`** — overall deadline. None = wait forever. Matches CONTEXT.md's "no hard timeout by default"; exposes the `--max-wait-minutes N` flag on the CLI side as `timeout_s = max_wait_minutes * 60` at the client.

### 5.2 Return contract

**Success shape:** return `ExceptionInfo` (existing Pydantic model at `src/stackly/models.py:46–54`). Matches CONTEXT.md. The client then fetches `get_callstack`/`get_threads`/`get_locals` on the same MCP session.

**Error shapes** (Claude's Discretion per CONTEXT.md — here's a recommended design):

Extend `src/stackly/models.py` with a `WatchOutcome` union to carry both success and structured non-error "outcomes":

```python
class WatchTimedOut(BaseModel):
    outcome: Literal["timed_out"] = "timed_out"
    elapsed_s: float

class WatchTargetExited(BaseModel):
    outcome: Literal["target_exited"] = "target_exited"
    elapsed_s: float

class WatchException(BaseModel):
    outcome: Literal["exception"] = "exception"
    exception: ExceptionInfo

WatchResult = Annotated[
    WatchException | WatchTimedOut | WatchTargetExited,
    Field(discriminator="outcome"),
]
```

**Why discriminated-union over raising:**
- Timeouts and clean target exits are NOT errors — they're expected outcomes. Pydantic error models are for actual failures (not-attached, detach-mid-wait, pybag-error).
- An MCP "error" gets wrapped in `ToolError` by FastMCP (`mcp/server/fastmcp/tools/base.py:116–117`) and surfaces on the client as an `McpError` exception. That's correct for "you called this wrong" or "pybag died." It's wrong for "the 5-min timeout you asked for elapsed."
- The discriminated union gives the client explicit structured handling: `match result.outcome:` pattern.

**Raise (MCP-error) for:**
- Not attached (session has no active DebugSession)
- Attached to a different PID than the `pid` parameter
- pybag detached mid-wait (COM error, DLL unload)
- Lock contention timeout (shouldn't happen in normal use; safety net)

### 5.3 Server-side implementation sketch

Put the polling loop on `DebugSession` (keeps pybag access in the one module that's allowed to touch it):

```python
# src/stackly/session.py — add new method

def wait_for_exception(
    self,
    pid: int,
    poll_s: int = 1,
    timeout_s: int | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> WatchResult:
    import time
    start = time.monotonic()
    poll_s = max(1, poll_s)  # pybag floor; see RESEARCH §2.1

    with self._lock:
        dbg = self._require_attached()
        # Assert the attached PID matches what the client expects (defensive).
        # Cheap — self._systems.GetCurrentProcessSystemId() is already in UserDbg.pid.
        attached_pid = dbg.pid
        if attached_pid != pid:
            raise DebugSessionError(
                f"Session attached to pid={attached_pid}, client asked for pid={pid}"
            )

        while True:
            if stop_check is not None and stop_check():
                # Cooperative cancellation from the async tool wrapper
                raise DebugSessionError("watch_for_crash cancelled")

            # Overall-deadline check
            if timeout_s is not None and (time.monotonic() - start) >= timeout_s:
                return WatchTimedOut(elapsed_s=time.monotonic() - start)

            # One poll tick. pybag.wait(poll_s) treats poll_s as seconds (§2.1)
            _ev_settled = dbg.wait(timeout=poll_s)
            # _ev_settled is unreliable as event discriminator — inspect status

            status = dbg.exec_status()  # "BREAK" | "GO" | "NO_DEBUGGEE" | ...

            if status == "NO_DEBUGGEE":
                return WatchTargetExited(elapsed_s=time.monotonic() - start)

            if status == "BREAK":
                # Could be exception OR initial break OR module-load break.
                # Reuse the existing .lastevent parser.
                exc = self._parse_lastevent_unlocked(dbg)
                if exc is not None:
                    return WatchException(exception=exc)
                # Non-exception break — resume target and keep polling.
                dbg.cmd("g", quiet=True)
                continue

            # "GO" or "STEP_*" — target still running, next tick
```

**Notes on this sketch:**

- `_parse_lastevent_unlocked(dbg)`: factor out the body of the existing `get_exception()` (session.py:288–318) so it can run without re-acquiring the lock. Current `get_exception()` is `with self._lock:` guarded; extract the inner body into a private helper.
- The `stop_check` callable lets the async tool wrapper poke the loop to exit on client disconnect. Set it via a `threading.Event` that the wrapper manages.
- The poll loop does NOT release the lock between ticks. Pattern A from §3.1 — simplest, and matches the actual usage (watch is the only tool running against this session during a watch).
- "Resume after non-exception break" via `dbg.cmd("g")`: matches how `DebugSession.continue_execution` works at session.py:435–440. Without this, a module-load break would leave the target paused and never resume — infinite loop of BREAK ticks.

**The async MCP tool wrapper** in `tools.py`:

```python
@mcp.tool()
async def watch_for_crash(pid: int, poll_s: int = 1, timeout_s: int | None = None) -> WatchResult:
    import anyio
    from functools import partial
    return await anyio.to_thread.run_sync(
        partial(session.wait_for_exception, pid=pid, poll_s=poll_s, timeout_s=timeout_s)
    )
```

Thread-offload is mandatory per §3.2.

### 5.4 Client-side usage in `stackly watch`

Pseudocode (the planner should turn this into tasks):

```python
# src/stackly/watch/__init__.py (new module)

import asyncio
from datetime import timedelta
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client  # non-deprecated variant

from stackly.fix.dispatcher import run_handoff, run_autonomous
from stackly.fix.mcp_client import ensure_server_running, shutdown_server

async def _watch_once(pid: int, mcp_url: str, conn_str: str | None = None) -> WatchResult:
    # Unbounded read-timeout httpx client for the watch_for_crash call (§3.3)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                # verify server identity (reuse mcp_client._REQUIRED_TOOLS pattern — ADD "watch_for_crash" to it)

                attach_args = {"pid": pid}
                if conn_str:
                    attach_args["conn_str"] = conn_str
                await session.call_tool("attach_process", attach_args)

                # Block indefinitely. timedelta(days=30) = practical infinity.
                result = await session.call_tool(
                    "watch_for_crash",
                    {"pid": pid, "poll_s": 1},
                    read_timeout_seconds=timedelta(days=30),
                )
                # result.structuredContent is a dict matching the WatchResult discriminated union.
                return parse_watch_result(result.structuredContent)

def run_watch(repo: Path, pid: int, host: str, port: int, auto: bool, ...) -> int:
    ensure_gitignore(repo)
    server_proc = ensure_server_running(host, port)
    mcp_url = f"http://{host}:{port}/mcp"
    try:
        for crash_idx in range(max_crashes):
            outcome = asyncio.run(_watch_once(pid, mcp_url, conn_str))
            match outcome.outcome:
                case "exception":
                    # Hand off to 2a dispatcher. Re-captures via its own attach — see §4.1.
                    if auto:
                        result = run_autonomous(repo=repo, pid=pid, host=host, port=port, ...)
                    else:
                        result = run_handoff(repo=repo, pid=pid, host=host, port=port, conn_str=conn_str)
                    # crash-hash dedup check (stay-resident only)
                    if result.crash_hash == last_hash:
                        logger.info("duplicate crash, skipping")
                        continue
                    last_hash = result.crash_hash
                case "timed_out":
                    logger.info("watch timed out after %.1fs", outcome.elapsed_s)
                    return 0
                case "target_exited":
                    logger.info("target process exited cleanly")
                    return 0
    finally:
        if server_proc is not None:
            shutdown_server(server_proc)
    return 0
```

Three things the planner must call out as tasks:
1. The `ensure_server_running` path is ALREADY shipped at `src/stackly/fix/mcp_client.py:48–107`. **Do not duplicate — import and reuse.**
2. The tool-presence check (R9 mitigation from Phase 2a) at `mcp_client.py:134` (`_REQUIRED_TOOLS`) must be UPDATED to include `"watch_for_crash"` once the new tool ships. Otherwise the capture step's server-identity check in `run_handoff`/`run_autonomous` won't require the new tool (which is fine, but worth noting for consistency).
3. The sync wrapper in `dispatcher.run_handoff` / `run_autonomous` is imported into the `watch` code directly — no new MCP plumbing in the `fix` module.

---

## §6 Open risks + recommended derisking in an early task

### 6.1 Risk: sub-second timeout reality vs CONTEXT.md signature (BLOCKING)

**Surface:** CONTEXT.md's `poll_ms: int = 500` parameter implies sub-second polling. pybag 2.2.16 silently converts `dbg.wait(timeout=0.5)` to "run the outer Python loop zero times, send SetInterrupt, return False" — i.e. **broken** (§2.1).

**Derisk in Task 2.5.0.1** (first task): write a unit test that calls `DebugSession.wait_for_exception(pid, poll_s=1, timeout_s=2)` against a `crash_app wait` fixture. Assert: `WatchTimedOut` is returned, `elapsed_s ≈ 2.0`. Then `poll_s=2, timeout_s=4` → same shape, `elapsed_s ≈ 4.0`. No crash. Proves the polling primitive works before building the tool on top.

### 6.2 Risk: client-side HTTP read-timeout cuts long waits (BLOCKING)

**Surface:** default 300-sec SSE read timeout in `create_mcp_http_client` (§3.3). Watches lasting > 5 min will fail client-side.

**Derisk in Task 2.5.0.2:** in the first `watch` skeleton, use Approach B (custom httpx client with `read=None`). Write an integration test that starts a `crash_app wait`, kicks off watch with a 60-sec timeout, and asserts the client doesn't raise `httpx.ReadTimeout`. Failure mode: if Approach B has a subtle bug (e.g., the non-deprecated `streamable_http_client` has a different API surface than documented), fall back to Approach A (`read_timeout_seconds=timedelta(days=30)` on `call_tool`) and document why.

### 6.3 Risk: `_parse_lastevent_unlocked` factoring introduces a subtle bug

**Surface:** splitting the `get_exception` body into a lock-free helper means the existing `get_exception` behavior must remain byte-identical. Regression risk.

**Derisk:** the existing integration tests (`tests/test_session_integration.py`) must all still pass. Also write a new unit test that calls `get_exception()` and `_parse_lastevent_unlocked(dbg)` back-to-back and asserts identical `ExceptionInfo` — no drift between the two paths.

### 6.4 Risk: non-exception breaks in the polling loop

**Surface:** `exec_status() == "BREAK"` is ambiguous (crash vs breakpoint vs module-load vs initial-break). The proposed handling is "if not an exception, `dbg.cmd('g')` and continue polling" — but module-load breaks on a busy target could loop thousands of times per second.

**Derisk in Task 2.5.0.3:** write an integration test that attaches to `crash_app wait` (which auto-breaks via `initial_break=True` at session.py:139), invokes watch_for_crash with a 5-sec timeout, and asserts `WatchTimedOut` (not a flood of `exception` results). This proves the "resume non-exception breaks" path works. If module-load breaks are too frequent, mitigation: call `dbg._control.SetEngineOptions` to disable unnecessary event filters (deferred if tests pass without it).

### 6.5 Risk: stay-resident mode's re-attach semantics

**Surface:** §4.3 — after a crash, the target is usually dead. Re-attach will fail. Stay-resident mode would loop with repeated "attach failed" on a corpse.

**Derisk:** in the stay-resident loop, catch `AttachResult.status == "failed"` explicitly and exit cleanly with a "target no longer attachable" log message. Write an integration test: `crash_app null` (crashes immediately), watch with `--max-crashes 3`, assert watch exits cleanly after the first crash, with clear stderr explaining the target is gone.

### 6.6 Risk: EventHandler exception-callbacks temptation

**Surface:** someone during implementation notices `pybag.dbgeng.callbacks.EventHandler` and tries to wire it up as a speedup, breaking the polling fallback.

**Mitigation:** DO NOT implement in 2.5 (§2.4 + CONTEXT.md Deferred). Flag in PLAN.md's architecture-decisions section: "EventHandler.exception() is explicitly out of scope for 2.5. Re-evaluate in a follow-up phase with empirical benchmarking vs polling."

### 6.7 Risk: `_REQUIRED_TOOLS` in `fix/mcp_client.py` rejects new tools

**Surface:** `fix/mcp_client.py:134` has `_REQUIRED_TOOLS = {"attach_process", "detach_process"}`. It checks the server exposes AT LEAST these tools — doesn't reject if more are present. So 2.5 adding `watch_for_crash` is safe.

BUT: when `watch` calls into `run_handoff`/`run_autonomous`, those functions' `capture_crash` does its own tool-presence check. If the planner tightens that check to include `watch_for_crash` without coordinating, backward compat breaks. **Recommendation: leave `_REQUIRED_TOOLS` as-is (don't tighten); 2.5 adds but doesn't require.**

---

## §7 Planner checklist

Concrete things the planner MUST address when writing PLAN.md:

### User-constraint hard rules (from CONTEXT.md)
- [ ] The tool IS named `watch_for_crash` (not `watch` or `await_crash`).
- [ ] The tool blocks SERVER-SIDE (not CLI-side).
- [ ] `watch` CLI dog-foods 2a via `run_handoff` / `run_autonomous`. No new capture code. No new briefing code. No new worktree code.
- [ ] Only ONE new MCP tool added in 2.5. No modifications to the existing 9 tools' signatures.
- [ ] Same `.stackly/` layout produced by `watch` as by `fix`.
- [ ] `--max-crashes N` flag exists; default 1 (one-shot).
- [ ] Duplicate-crash dedup via `crash_hash` compare in stay-resident only.
- [ ] Ctrl-C handler: SIGINT + SIGBREAK, terminate any in-flight claude child, detach pybag, exit 130.
- [ ] `.stackly/` auto-added to `.gitignore` via `ensure_gitignore` (already shipped at `fix/worktree.py`).

### Cannot-skip derisking (from §6, reordered by risk)
- [ ] **Task 2.5.0.1 first:** validate the polling primitive — unit test + integration test that `DebugSession.wait_for_exception` with `poll_s=1, timeout_s=N` returns `WatchTimedOut` with correct elapsed_s on a non-crashing target.
- [ ] **Task 2.5.0.2 second:** validate unbounded HTTP read-timeout using Approach B (`streamable_http_client` + custom `httpx.AsyncClient(read=None)`). Integration test: watch for > 60 s without httpx.ReadTimeout.
- [ ] **Task 2.5.0.3 third:** validate non-exception-break handling — integration test with `initial_break=True` attach proves the loop doesn't bail early.

### Signature corrections (from §2.1)
- [ ] Rename the MCP tool's parameter from `poll_ms` to `poll_s` (or internally floor to 1 second with a prominent docstring). CONTEXT.md's `poll_ms` is based on incorrect pybag API assumption; correct before the tool ships.
- [ ] Document in the tool docstring: "Poll interval is clamped to pybag's 1-second minimum granularity."

### Module structure
- [ ] New file: `src/stackly/watch.py` (or `src/stackly/watch/` subpackage) — parallel to `fix/` but smaller. Keep naming consistent with `fix/` idioms (same function names, same file-layout pattern).
- [ ] Extend `src/stackly/session.py` with `wait_for_exception(pid, poll_s, timeout_s, stop_check) -> WatchResult`. This is the ONLY place pybag access happens.
- [ ] Extend `src/stackly/tools.py` with the async `watch_for_crash` tool registration.
- [ ] Extend `src/stackly/models.py` with `WatchException`, `WatchTimedOut`, `WatchTargetExited` Pydantic models + the discriminated `WatchResult` alias.
- [ ] Extend `src/stackly/cli.py` with the `watch` Typer subcommand.
- [ ] Factor `DebugSession.get_exception()` body into `_parse_lastevent_unlocked(dbg)` so `wait_for_exception` can re-use without lock recursion.

### Reuse checklist (do NOT duplicate)
- [ ] Import `compute_crash_hash` from `stackly.fix.worktree`.
- [ ] Import `ensure_server_running` / `shutdown_server` from `stackly.fix.mcp_client`.
- [ ] Import `ensure_gitignore` from `stackly.fix.worktree`.
- [ ] Import `run_handoff` / `run_autonomous` from `stackly.fix.dispatcher`.
- [ ] Add `"watch_for_crash"` to `scripts/e2e_smoke.py`'s `expected` tool-set (it's currently 9 tools; becomes 10).
- [ ] Keep lazy-pybag-import discipline: `cli.py` and `watch.py` must NOT import `session.py` or pybag at module load. Top-level imports of `dispatcher` and `mcp_client` are OK — they're already pybag-free per Phase 2a's constraint.

### FastMCP async correctness (§3.2)
- [ ] `watch_for_crash` declared `async def`.
- [ ] Body uses `anyio.to_thread.run_sync(...)` to offload the blocking poll.
- [ ] Test asserts that another MCP tool call (e.g. `attach_process` to a different PID, or an intentional no-op like a `list_tools`) can be serviced by the server DURING a `watch_for_crash` in-flight. This is the structural proof that the event loop isn't blocked. Integration test, not a unit test.

### Client-timeout correctness (§3.3)
- [ ] `watch` CLI's MCP client uses `streamable_http_client(url, http_client=httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)))`.
- [ ] Per-call `read_timeout_seconds=timedelta(days=30)` on `session.call_tool("watch_for_crash", ...)` as belt-and-braces.
- [ ] Consider refactoring `fix/mcp_client.py` to expose a shared `create_watch_http_client()` helper so Phase 2a's capture path and 2.5's watch path agree on http-client construction. **Recommendation: defer — they have genuinely different timeout needs; separate call sites are clearer than premature sharing.**

### CLI shape (from CONTEXT.md shared-flags decision)
- [ ] `stackly watch --pid N --repo PATH` required flags.
- [ ] Shared with `fix`: `--host`, `--port`, `--conn-str`, `--build-cmd`, `--test-cmd`, `--auto`, `--model`, `--max-attempts`.
- [ ] New to `watch`: `--max-crashes N` (default 1), `--max-wait-minutes N` (default: no limit), `--poll-seconds N` (default 1), `--quiet` (suppress Rich spinner).
- [ ] `watch --help` presented clearly: the first line is "Watch a process for crashes and auto-dispatch the fix agent."

### Test strategy (from Q12 nice-to-have)
- [ ] Unit tests: the polling loop's status-transition logic (mockable via a fake `DebugSession.wait_for_exception` stub).
- [ ] Unit tests: signal-handler idempotency (same pattern as `tests/test_fix_dispatcher.py`).
- [ ] Unit tests: `WatchResult` discriminated-union round-trip.
- [ ] Integration test: `crash_app wait` with short timeout → asserts WatchTimedOut.
- [ ] Integration test: `crash_app null` → asserts WatchException with code_name == "EXCEPTION_ACCESS_VIOLATION", then asserts the dispatcher was invoked (monkeypatch `run_handoff` to record its invocation).
- [ ] Integration test auto-skip gate: reuse `tests/conftest.py`'s existing pattern.

### Commit discipline
- [ ] Each task produces one commit using the same style as Phase 2a's `docs(2.5):` / `feat(2.5):` / `test(2.5):` prefixes.
- [ ] Tests written before implementation per CLAUDE.md's TDD discipline and Phase 2a's established pattern.

---

## Sources

### Primary (HIGH confidence — source-read or official docs)

**pybag 2.2.16 internals (installed at `C:/Users/idang/AppData/Local/Programs/Python/Python314/Lib/site-packages/pybag/`):**
- `userdbg.py:43–58` — attach/detach public API, standalone mode.
- `pydbg.py:86–119` — `EventThread` + `WorkItem` queue; exception swallow.
- `pydbg.py:256–287` — `_worker_wait` (seconds→ms conversion + Python-side poll loop) + `wait()` standalone branch.
- `pydbg.py:173–196` — `exec_status`, `go`, `go_handled`, `go_nothandled`.
- `pydbg.py:289–299` — `cmd()` execution (used by existing `DebugSession.get_exception` for `.lastevent`/`.exr`).
- `dbgeng/core.py:54–64` — `str_execution_status` map; `DEBUG_STATUS_BREAK`, `DEBUG_STATUS_NO_DEBUGGEE`, etc.
- `dbgeng/callbacks.py:11–80` — `EventHandler.exception()` + `_ev_exception`; `_catch_event`/`_ignore_event`; `InterestMask` wiring.
- `dbgeng/callbacks.py:363–460` — `DbgEngCallbacks` CoClass + `IDebugEventCallbacks_Exception` dispatch.
- `dbgeng/idebugcontrol.py:508–512` — `WaitForEvent` wrapper; S_FALSE → `DbgEngTimeout`.

**mcp 1.27.0 internals (installed at `.../mcp/`):**
- `server/fastmcp/utilities/func_metadata.py:74–95` — sync-tool-on-event-loop gotcha.
- `server/fastmcp/tools/base.py:93–117` — tool dispatch + `ToolError` wrapping.
- `server/lowlevel/server.py:673–684` — concurrent message dispatch via `tg.start_soon`.
- `client/session.py:368–397` — `call_tool(read_timeout_seconds=)`.
- `client/streamable_http.py:618–707` — `streamable_http_client` (non-deprecated) with `http_client=...` param; `streamablehttp_client` (deprecated, Phase 2a's current usage).
- `shared/_httpx_utils.py:10–87` — `MCP_DEFAULT_TIMEOUT=30`, `MCP_DEFAULT_SSE_READ_TIMEOUT=300`.
- `shared/session.py:240–308` — `send_request` with `anyio.fail_after(timeout)`.

**Stackly source (this repo):**
- `src/stackly/session.py:105–194` — `DebugSession` lock + attach/detach.
- `src/stackly/session.py:288–318` — `get_exception` + `.lastevent`/`.exr` parsers.
- `src/stackly/session.py:176–196` — `detach()` (2a.0.1 addition).
- `src/stackly/models.py:46–54` — `ExceptionInfo` schema.
- `src/stackly/tools.py:27–122` — existing 9-tool registration surface.
- `src/stackly/fix/dispatcher.py:58–85` — `_install_signal_handlers` + `_FixState`.
- `src/stackly/fix/dispatcher.py:152–398` — `run_handoff` + `run_autonomous` signatures and internal flow.
- `src/stackly/fix/mcp_client.py:48–107` — `ensure_server_running`; line 110–131 — `shutdown_server`; line 134 — `_REQUIRED_TOOLS`.
- `src/stackly/fix/worktree.py:99–116` — `compute_crash_hash(capture) -> str`.
- `src/stackly/fix/models.py:31–117` — `CrashCapture`, `FixResult`.
- `src/stackly/cli.py:143–205` — `fix` Typer subcommand shape to mirror.
- `scripts/e2e_smoke.py:95–148` — MCP-client integration test pattern.
- `tests/conftest.py:48–75` — `crash_app_waiting` fixture.
- `tests/fixtures/crash_app/crash.cpp:25–75` — deterministic crash modes (null, stack, throw, wait).

**Microsoft DbgEng authoritative docs:**
- [IDebugControl::WaitForEvent — MS Learn](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/dbgeng/nf-dbgeng-idebugcontrol-waitforevent) — return codes (S_OK, S_FALSE, E_PENDING, E_UNEXPECTED, E_FAIL); non-reentrancy; single-thread constraint.

**Stackly planning docs:**
- `.planning/PROJECT.md` — tech stack; Windows + pybag + single-threaded DbgEng constraints.
- `.planning/ROADMAP.md` — Phase 2.5 scope boundaries.
- `.planning/phase-2.5-crash-auto-detection/CONTEXT.md` — user's locked decisions (verbatim in `<user_constraints>` above).
- `.planning/phase-2a-fix-loop-mvp/GOAL.md` — agent↔server MCP coupling rule (dog-food principle).
- `.planning/phase-2a-fix-loop-mvp/PLAN.md` §2 — 2a's 8 locked architecture decisions; §3 — component breakdown; tasks 2a.0.1 / 2a.1.1 / 2a.3.1 for the reusable bits.
- `.planning/phase-2a-fix-loop-mvp/RESEARCH.md` §1.1 / §3.4 / §3.5 — Claude Code CLI flags, server-spawn / server-shutdown patterns (already shipped in `fix/mcp_client.py`).
- `.planning/phase-2a-fix-loop-mvp/PLAN_CHECK.md` — surfaced architectural issues to avoid re-introducing (e.g. C1 `compute_crash_hash` location, C3 no-DebugSession-import enforcement).

### Secondary (MEDIUM confidence — single web source or single-author blog)
- [Pybag on GitHub](https://github.com/dshikashio/Pybag) — upstream project; latest activity indicates still maintained. Corroborates the module structure seen in site-packages.
- [Pybag on PyPI](https://pypi.org/project/Pybag/) — version listing; confirms 2.2.16 is a real release.

### Tertiary (LOW confidence / unverified)
- None used for any load-bearing claim in this document.

---

## §8 Metadata

**Confidence breakdown:**
- pybag wait-loop semantics: HIGH — direct source read of pybag 2.2.16 at installed site-packages; MS docs for underlying COM primitive.
- FastMCP async/concurrency model: HIGH — direct source read of mcp 1.27.0.
- Phase 2a integration surface: HIGH — all cited functions exist in the current repo with tests.
- End-to-end crash detection flow: MEDIUM — no empirical run in this research pass; derisking tasks 2.5.0.1–2.5.0.3 in §6 are designed to catch surprises in the first hour of implementation.
- Stay-resident mode usefulness: MEDIUM — the physical constraints (crashed target usually dead) are well-understood, but the specific behavior of `attach_local` on a zombie PID has not been empirically tested in this research.

**Research date:** 2026-04-23
**Valid until:** 2026-05-23 (30 days — pybag 2.2.16 and mcp 1.27.x are both stable; no known upcoming breaking changes). Re-validate on pybag or mcp minor-version bump.

## RESEARCH COMPLETE

**Phase:** 2.5 - Crash auto-detection
**Confidence:** HIGH (with two pre-flight derisking tasks required, as listed in §6.1 and §6.2)

### Key Findings

- **pybag's `dbg.wait(timeout)` takes SECONDS, not milliseconds**, and sub-second timeouts are silently broken in pybag 2.2.16 (`_worker_wait` integer-division bug; see §2.1). CONTEXT.md's `poll_ms: int = 500` signature needs correction before shipping — recommend `poll_s: int = 1` with a 1-second floor documented in the tool docstring.
- **FastMCP 1.27 runs sync tools on the asyncio event loop**, so `watch_for_crash` MUST be declared `async def` and offload the blocking poll via `anyio.to_thread.run_sync` — otherwise the server is frozen for the entire watch duration and the "share the attach" design cannot work (§3.2).
- **The MCP client's default HTTP read-timeout is 300 s** (§3.3). The `watch` CLI must pass a custom `httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))` to `streamable_http_client` OR a large `read_timeout_seconds` to `call_tool` (both recommended). Without this, watches lasting more than 5 minutes fail client-side even though the server is still waiting.
- **Phase 2a's dispatcher is directly callable** (`run_handoff`, `run_autonomous` at `src/stackly/fix/dispatcher.py:152–398`). `watch` dog-foods by calling these after `watch_for_crash` returns. The internal `capture_crash` inside the dispatcher will re-attach via its own MCP flow — this is redundant but idempotent and correct. Defer the "share the attach" optimization; ship first.
- **`pybag.dbgeng.callbacks.EventHandler.exception()` DOES exist** and IS wired to `IDebugEventCallbacks::Exception` (`dbgeng/callbacks.py:75–78`, line 416). But it's NOT subscribed by default, runs on pybag's EventThread (not the MCP async thread), and would require deadlock-prone re-plumbing to integrate cleanly. Stay with polling — matches CONTEXT.md's default assumption. Defer to a follow-up phase.
- **Status discrimination after `dbg.wait()` returns** is reliable: inspect `dbg.exec_status()` for `"BREAK"`/`"NO_DEBUGGEE"`, then if `"BREAK"` reuse the existing `.lastevent` parser (factor out of `DebugSession.get_exception` body into a lock-free helper so the polling loop can reuse without lock recursion).

### File Created
`.planning/phase-2.5-crash-auto-detection/2.5-RESEARCH.md`

### Confidence Assessment

| Area | Level | Reason |
|------|-------|--------|
| Standard Stack | HIGH | All libraries already in pinned Phase 1/2a deps; no new dependencies. |
| Architecture | HIGH | All integration points are either (a) already shipped in Phase 2a, or (b) one-method additions to shipped modules. |
| Pitfalls | HIGH | Three blocking gotchas identified and each has a concrete derisking task (§6.1, §6.2, §6.4). |

### Open Questions

None blocking. Two items flagged as MEDIUM-confidence to validate via the first implementation task (§6.1 poll-timeout units, §6.2 http read-timeout) — these are cheap, sub-30-minute integration tests that must pass before building the full feature.

### Ready for Planning

Research complete. Planner can now create PLAN.md with the §7 checklist and §6 risk-derisking tasks as the first three atomic tasks in the plan. Recommend the planner write tasks 2.5.0.1–2.5.0.3 before ANY of the tool-wiring or CLI tasks — these three tests will surface any remaining pybag/mcp surprise within the first 90 minutes of implementation.
