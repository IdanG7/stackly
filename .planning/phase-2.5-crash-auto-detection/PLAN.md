# Phase 2.5 — Crash auto-detection — Executable Plan

**Plan date:** 2026-04-23
**Phase goal (one sentence):** A developer runs `stackly watch --pid N --repo PATH` against a live Windows process and Stackly blocks until that process throws an exception, then auto-invokes the Phase 2a fix dispatcher (`run_handoff` or `run_autonomous`) so the developer gets a briefing or validated patch without ever noticing the crash by hand.
**Source of truth:** `CONTEXT.md` (locked decisions), `RESEARCH.md` (empirical pybag/FastMCP findings — do NOT re-derive), `phase-2a-fix-loop-mvp/PLAN.md` (format + dispatcher contract), `phase-2a-fix-loop-mvp/GOAL.md` (dog-food-via-MCP constraint).

---

## 1. Context

Phase 1 shipped the MCP server + 8 crash-capture tools. Phase 2a added a 9th tool (`detach_process`) and the `stackly fix --pid N --repo PATH` CLI — the first *consumer* of the server, with two modes: hand-off (interactive Claude Code) and autonomous (headless `claude -p` → validated `.patch`).

Phase 2.5 closes the last manual link in the loop: **the developer shouldn't have to notice the crash.** Today someone has to watch their app, see it die, then run `stackly fix --pid N` by hand. This phase adds `stackly watch --pid N --repo PATH` — a command that attaches once, blocks server-side on a polling wait loop, and on exception directly invokes Phase 2a's `dispatcher.run_handoff(...)` / `run_autonomous(...)`.

Phase 2.5 is deliberately ~200 lines of orchestration sitting on three existing foundations: (a) Phase 1's `DebugSession` + MCP server, (b) Phase 2a's already-shipped `dispatcher.run_handoff` / `run_autonomous` entry points, and (c) pybag's standalone `dbg.wait()` primitive. **Zero new crash-capture code. Zero new fix-loop code.** One new MCP tool (the 10th), one new CLI subcommand, one new `wait_for_exception()` method on `DebugSession`.

Explicitly deferred (see §8): multi-PID daemon mode, Windows AeDebug JIT registry integration, auto-spawn-and-watch, ETW, tiered model routing, `claude --resume` across crashes.

## 2. Architecture decisions (pinned — from CONTEXT.md)

These are **LOCKED** per `CONTEXT.md`. Changing any of them requires updating CONTEXT.md first.

1. **Detection is polling, not callbacks.** `DebugSession.wait_for_exception()` loops on `dbg.wait(timeout=poll_s)` + `dbg.exec_status()` + `.lastevent` inspection. `pybag.dbgeng.callbacks.EventHandler.exception()` is explicitly **out of scope** for 2.5 (deadlock risk, non-reentrancy, threading complexity — see RESEARCH.md §2.4; re-evaluate in a follow-up phase only after empirical benchmarking).

2. **The polling loop runs SERVER-SIDE inside a new MCP tool.** Adding one new tool (`watch_for_crash`) keeps the agent↔server MCP-only coupling from Phase 2a's Architecture Decision #1. The `watch` CLI is an MCP client exactly like `fix`.

3. **`watch_for_crash` MUST be `async def`** and offload the blocking poll body via `await anyio.to_thread.run_sync(...)`. A sync tool would freeze the FastMCP asyncio event loop for the entire watch duration — the "share the attach" design (where the client calls other tools *after* watch returns) cannot work without thread-offload. This is non-negotiable (RESEARCH.md §3.2).

4. **`poll_s` parameter (seconds), not `poll_ms`.** CONTEXT.md's original name `poll_ms: int = 500` was based on an incorrect assumption about pybag. `pybag.pydbg.UserDbg.wait(timeout)` takes SECONDS; sub-second values are silently broken due to an integer-division in `_worker_wait` (`pybag/pydbg.py:256–276` — RESEARCH.md §2.1). The tool parameter is renamed to `poll_s: int = 1` with a 1-second floor documented in the docstring.

5. **MCP client must disable HTTP read-timeout.** Default httpx read timeout in `mcp/shared/_httpx_utils.py` is 300 s. Watches lasting > 5 min would fail client-side while the server is still happily blocking. The `watch` CLI passes a custom `httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))` to `streamable_http_client` AND sets `read_timeout_seconds=timedelta(days=30)` on the `call_tool("watch_for_crash")` call as belt-and-braces (RESEARCH.md §3.3).

6. **`WatchResult` is a Pydantic v2 discriminated union**, not an exception-raise-on-timeout. Timeouts and clean target-exits are expected outcomes, not errors — they get structured return shapes. Only "you called this wrong" (not attached, wrong PID) and "pybag died mid-wait" raise MCP errors. Three outcome shapes: `WatchException { exception: ExceptionInfo }`, `WatchTimedOut { elapsed_s }`, `WatchTargetExited { elapsed_s }` (RESEARCH.md §5.2).

7. **Dog-food Phase 2a.** On exception, `watch` calls `stackly.fix.dispatcher.run_handoff(repo, pid, host, port, conn_str)` (default) or `run_autonomous(repo, pid, host, port, build_cmd, test_cmd, model, max_attempts, conn_str)` (`--auto`) **directly**. No new capture code, no new briefing code, no new worktree code. Same `.stackly/` layout, same artifacts, same CLI flags.

8. **"Share the attach" is NOT optimized in 2.5.** After `watch_for_crash` returns, the 2a dispatcher's internal `capture_crash()` will re-attach to the same PID. `DebugSession.attach_local` calls `_close_locked()` first (session.py:134), so re-attaching is clean — idempotent, correct, costs ~3 extra MCP round-trips. Defer the optimization (RESEARCH.md §4.1).

9. **One-shot by default; `--max-crashes N` for stay-resident.** Default `--max-crashes 1`. Stay-resident mode detaches + re-attaches between crashes (cannot continue the target past an exception). Duplicate-crash dedup only in stay-resident mode: reuse `compute_crash_hash` from `fix/worktree.py`; skip dispatch if current hash equals previous within the same `watch` invocation.

10. **Signal handling mirrors 2a's pattern.** SIGINT + SIGBREAK on Windows. Handler terminates any in-flight Claude Code child (if dispatch is running), detaches pybag via MCP, exits 130. Once the dispatcher is invoked, its own 2a handlers install over the top (signal.signal replaces, doesn't stack — see RESEARCH.md §4.2). 1-second worst-case Ctrl-C latency is accepted (no `SetInterrupt` plumbing).

11. **No new Python dependencies.** `anyio`, `httpx`, `mcp`, `pybag`, `typer`, `pydantic`, `rich` are all already pinned via Phase 1/2a. `doctor` remains unchanged (no new external binaries).

### Critical-path invariant

RESEARCH.md identified three empirical gotchas (§6.1, §6.2, §6.4) that would each silently break the feature. They are derisked in Wave 0 as tests 2.5.0.1 / 2.5.0.2 / 2.5.0.3 **before** any tool-wiring or CLI code is written. 2.5.0.4 (stay-resident re-attach) is a lower-priority derisk; if it fails, we fall back to `--max-crashes 1` only and ship without stay-resident mode.

## 3. Component breakdown

All production code changes touch a small, well-bounded surface:

```
src/stackly/
├── models.py              # ADD: WatchException, WatchTimedOut, WatchTargetExited, WatchResult
├── session.py             # ADD: wait_for_exception(), _parse_lastevent_unlocked() factoring
├── tools.py               # ADD: async watch_for_crash tool (10th tool)
├── cli.py                 # ADD: `watch` Typer subcommand
└── watch/                 # NEW subpackage — parallels fix/
    ├── __init__.py        # re-exports for CLI wiring
    └── dispatcher.py      # run_watch() — MCP-client orchestration, dedup, signal handlers
```

**No changes to `fix/`**. `watch/` imports from `fix/` (dispatcher, worktree, mcp_client helpers); never the reverse.

Component responsibility:

1. **`models.py` extension**: Three new Pydantic models (`WatchException`, `WatchTimedOut`, `WatchTargetExited`) + a `WatchResult` discriminated union using `Annotated[... | ... | ..., Field(discriminator="outcome")]`. `WatchException` wraps the existing `ExceptionInfo`.

2. **`session.py` extension**: A new `wait_for_exception(pid, poll_s, timeout_s, stop_check) -> WatchResult` method that runs the polling loop under `self._lock`. Per RESEARCH.md §5.3: each tick = `dbg.wait(poll_s)` → `exec_status()` → if `"BREAK"`, check `.lastevent` via a factored-out `_parse_lastevent_unlocked(dbg)` helper; exception → `WatchException`; non-exception break → `dbg.cmd("g")` + continue polling; `"NO_DEBUGGEE"` → `WatchTargetExited`. Also factor the body of the existing `get_exception()` (session.py:288–318) into `_parse_lastevent_unlocked` so both public `get_exception()` and the new polling loop can reuse it without lock recursion.

3. **`tools.py` extension**: One new `@mcp.tool()` declared `async def watch_for_crash(pid, poll_s=1, timeout_s=None) -> WatchResult` that wraps `await anyio.to_thread.run_sync(partial(session.wait_for_exception, ...))`. **MANDATORY async** — see Architecture Decision #3.

4. **`watch/dispatcher.py`**: The new client-side orchestrator. One public entry point `run_watch(repo, pid, host, port, auto, build_cmd, test_cmd, model, max_attempts, conn_str, max_crashes, max_wait_minutes, quiet) -> int` that:
   - Calls `ensure_gitignore(repo)` and `ensure_server_running(host, port)` (both imported from `fix/`).
   - For each crash index up to `max_crashes`:
     - Opens an MCP session with `httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))` + `streamable_http_client` + `ClientSession`.
     - Calls `attach_process`, then `watch_for_crash` with `read_timeout_seconds=timedelta(days=30)`.
     - Match on `result.outcome`: `"exception"` → call `run_handoff` or `run_autonomous` (with dedup via `compute_crash_hash` in stay-resident mode); `"timed_out"` → log, exit 0; `"target_exited"` → log, exit 0.
   - Installs SIGINT/SIGBREAK handlers that terminate any in-flight claude child, call `detach_process` via MCP, exit 130.
   - Renders a Rich spinner during `watch_for_crash` (unless `--quiet`).

5. **`cli.py` extension**: A new `stackly watch` Typer subcommand that mirrors `fix`'s flag surface (`--pid`, `--repo`, `--host`, `--port`, `--conn-str`, `--build-cmd`, `--test-cmd`, `--auto`, `--model`, `--max-attempts`) plus three new-to-watch flags: `--max-crashes N` (default 1), `--max-wait-minutes N` (default: no limit), `--poll-seconds N` (default 1), `--quiet`. **Lazy-imports** `from stackly.watch.dispatcher import run_watch` inside the command function to preserve the Phase 1 invariant that `cli.py` doesn't pull pybag / MCP at module load.

6. **Tests**: `tests/test_watch_models.py` (discriminated-union round-trip), `tests/test_watch_derisk_timeout_units.py` + `_async_offload.py` + `_nonexception_break.py` (Wave 0 derisking), `tests/test_watch_session.py` (`wait_for_exception` behavior with mocked pybag), `tests/test_watch_dispatcher.py` (dedup, signal handlers, MCP-client plumbing — monkeypatched), `tests/test_watch_cli.py` (`--help` flags), `tests/test_watch_e2e.py` (@integration — live `crash_app` → watch → dispatch-invoked).

## 4. Atomic task list (the executable part)

Task-size legend: **XS** ≤ 10 min, **S** 10–30 min, **M** 30–60 min (flagged for splitting if reached).

Global notes on tests:
- Unit tests in `tests/test_watch_*.py` must NOT import pybag, MCP, or `claude`.
- Integration tests use `@pytest.mark.integration` and rely on `tests/conftest.py`'s existing auto-skip gate (Phase 1 pattern).
- Slow/live-crash-app tests additionally use `@pytest.mark.slow` (Phase 2a pattern).
- Derisk tests in Wave 0 use `@pytest.mark.integration` — they run against real pybag but not against a real claude subprocess.
- Commit style mirrors Phase 2a: `docs(2.5):` / `feat(2.5):` / `test(2.5):` / `fix(2.5):`. Each task produces at least one commit; tests-first per CLAUDE.md TDD discipline.

---

### Wave 0 — Derisking (MUST run before any downstream work)

Three empirical validations of RESEARCH.md's critical findings. Each is ≤ 30 min, each produces a single integration test file + minimal scaffolding. **If any of 2.5.0.1–2.5.0.3 fails, STOP and re-plan** — the finding that assumption was wrong must propagate back to the plan before wasting time on downstream plumbing. 2.5.0.4 is lower-priority (its failure degrades stay-resident mode to one-shot-only but doesn't block the phase).

#### Task 2.5.0.1 — Derisk: pybag `wait()` timeout-unit semantics

- **Files:** `tests/test_watch_derisk_timeout_units.py` (new).
- **Failing test to write first:** `tests/test_watch_derisk_timeout_units.py::test_wait_timeout_seconds_semantics_against_waiting_target` (`@pytest.mark.integration`) — attach to a `crash_app wait` fixture (import pybag directly — this is a one-off validation, not production code), call `dbg.wait(timeout=2)`, assert the call returns within 2.0–3.5 s; call `dbg.wait(timeout=1)`, assert it returns within 1.0–2.5 s. Skip on non-Windows or no-pybag environments via the existing fixture gate.
- **Action:** Empirically confirm RESEARCH.md §2.1's claim that `dbg.wait(timeout)` treats `timeout` as SECONDS, not milliseconds, and that `timeout=1` is the floor that works. No production code in this task — purely a validation test. If the test observations disagree with RESEARCH.md (e.g. behavior differs by pybag patch version), document the delta in a comment at the top of the test file and STOP work until the plan is updated. Also document observed elapsed times in a short comment block so future contributors see the calibration.
- **Acceptance criteria:**
  - Test passes locally on a Windows machine with Debugging Tools installed.
  - Auto-skips in CI (reuses `tests/conftest.py` gate).
  - Observed `dbg.wait(timeout=2)` elapsed ∈ [1.8, 3.5] seconds.
  - Observed `dbg.wait(timeout=1)` elapsed ∈ [0.8, 2.5] seconds.
  - Calibration numbers recorded as a comment in the test file.
- **Size:** S
- **Dependencies:** none.

#### Task 2.5.0.2 — Derisk: FastMCP async-tool + anyio.to_thread.run_sync plumbing

- **Files:** `tests/test_watch_derisk_async_offload.py` (new).
- **Failing test to write first:** `tests/test_watch_derisk_async_offload.py::test_async_tool_with_thread_offload_does_not_block_event_loop` (`@pytest.mark.integration`) — spin up a minimal `FastMCP()` instance with TWO tools: (a) an async tool `slow_blocker` that does `await anyio.to_thread.run_sync(lambda: time.sleep(2))`, (b) a sync trivial tool `ping` that returns `"pong"`. Use `mcp.server.lowlevel.server` + `mcp.shared.memory.create_connected_server_and_client_session` or `streamablehttp_client` against a real ephemeral server. Kick off `slow_blocker` as an asyncio task, then IMMEDIATELY call `ping` — assert `ping` returns within 0.5 s while `slow_blocker` is still running. This proves the event loop isn't frozen. If you can't easily do this in-process, run the server via subprocess on an ephemeral port and use two clients.
- **Action:** Empirically validate RESEARCH.md §3.2's claim that async tools offloaded via `anyio.to_thread.run_sync` leave the event loop responsive. This is a derisk of the central architectural premise — if this test fails, the whole approach of blocking-server-side-in-an-MCP-tool is infeasible and the phase needs to redesign watch as a client-side poll. Keep the test tightly scoped — it's not testing our code, it's testing the FastMCP contract we rely on.
- **Acceptance criteria:**
  - `slow_blocker` keeps running for ~2 s.
  - `ping` roundtrips in < 500 ms while `slow_blocker` is blocked.
  - Test passes on Windows + Linux (pybag-free; doesn't need Debugging Tools).
  - If test fails, a comment at the top records the failure mode and recommends aborting to redesign before proceeding.
- **Size:** M — flagged. If the in-process memory-transport path is awkward, fall back to subprocess + HTTP; that's slightly more plumbing but more faithful. Time cap: 60 min; if the test won't go green in that window, ship a documented stub that logs "derisk skipped — relying on RESEARCH.md §3.2 source reading" and proceed, but flag it in the final SUMMARY.
- **Dependencies:** none.

#### Task 2.5.0.3 — Derisk: non-exception-break handling in the polling loop

- **Files:** `tests/test_watch_derisk_nonexception_break.py` (new).
- **Failing test to write first:** `tests/test_watch_derisk_nonexception_break.py::test_wait_loop_survives_initial_break_without_returning_exception` (`@pytest.mark.integration`) — attach to `crash_app wait` (which hits `initial_break=True` at `session.py:140`, producing a BREAK event with no exception). Run an inline mini polling loop (10 iterations, 1-sec each): each iteration, call `dbg.wait(timeout=1)` → inspect `dbg.exec_status()` → if `"BREAK"` call `dbg.cmd(".lastevent", quiet=True)` and assert no exception signature match (use the existing `_LASTEVENT_RE` from `session.py`). Then `dbg.cmd("g", quiet=True)` to resume and loop. After ~5 ticks, assert the loop never "detected" an exception (since `crash_app wait` is running cleanly) and the target stays alive (PID still responding).
- **Action:** Empirically confirm RESEARCH.md §6.4's claim that the "resume non-exception break" pattern (`dbg.cmd("g")` after a non-exception BREAK) actually works — that we don't get flooded with module-load breaks or stuck in an infinite BREAK loop. This is the third blocking risk; if it fails, the polling loop needs additional event-filter tuning via `dbg._control.SetEngineOptions` before the main implementation can work. This test serves as a reference implementation for `wait_for_exception` in Task 2.5.1.2 — the production code should mirror its structure.
- **Acceptance criteria:**
  - Test passes on Windows with Debugging Tools installed.
  - No `WatchException` shape emitted (no exception in 5 ticks).
  - Target stays attached and alive.
  - If module-load breaks are observed to fire ≥ 10 times per second (flooding), the test documents it and recommends SetEngineOptions tuning before Task 2.5.1.2 ships.
- **Size:** S
- **Dependencies:** none.

#### Task 2.5.0.4 — Derisk (LOW priority): stay-resident re-attach after a crash

- **Files:** `tests/test_watch_derisk_reattach.py` (new).
- **Failing test to write first:** `tests/test_watch_derisk_reattach.py::test_reattach_after_exception_returns_failed_or_attached` (`@pytest.mark.integration`) — start `crash_app null` (which crashes immediately on access violation); `DebugSession.attach_local(pid)`; detect the crash via `get_exception`; call `session.detach()`; then call `session.attach_local(pid)` again. Assert that the second attach either (a) returns `AttachResult(status="failed", message=<something human-readable>)` OR (b) returns `status="attached"` and subsequent `get_exception` still reports the exception. Either outcome is valid — the test just documents which one actually happens on pybag 2.2.16 / Windows 11 so Task 2.5.2.1 knows how to code the stay-resident loop.
- **Action:** Empirically answer: "After `detach_process`, can we `attach_process` to the same PID?" RESEARCH.md §4.3 hypothesizes "probably not, because the target is dead" but explicitly marks this MEDIUM confidence. This test produces the authoritative answer. **Not blocking**: if the answer is "re-attach always fails cleanly," then Task 2.5.2.1 implements `--max-crashes > 1` as "try re-attach, on fail exit cleanly with a clear log message." If the answer is "re-attach works sometimes," Task 2.5.2.1 implements the detect-and-continue loop.
- **Acceptance criteria:**
  - Test passes on Windows with Debugging Tools installed.
  - Records the observed behavior in a comment block at the top of the test file: "On pybag 2.2.16 + Windows 11, re-attach to a crashed PID returns {attached|failed with message 'X'}."
  - Feeds this finding into Task 2.5.2.1's implementation approach.
- **Size:** S
- **Dependencies:** none.
- **Note:** If this derisk fails to produce a clear answer (e.g. flaky results), default Task 2.5.2.1 to "try re-attach; on failure log and exit cleanly, don't loop" — the safest shape.

---

### Wave 1 — Foundation (models, MCP tool, session method)

Wave 0 derisks must pass before starting Wave 1. Within Wave 1, 2.5.1.1 and 2.5.1.2 can run in parallel (different files, no shared state); 2.5.1.3 depends on both.

#### Task 2.5.1.1 — `WatchResult` discriminated union in `models.py`

- **Files:** `src/stackly/models.py` (extend), `tests/test_watch_models.py` (new).
- **Failing test to write first:** `tests/test_watch_models.py::test_watch_result_discriminated_union_round_trip` — build one instance each of `WatchException(exception=ExceptionInfo(...))`, `WatchTimedOut(elapsed_s=30.0)`, `WatchTargetExited(elapsed_s=10.0)`; `.model_dump()` then `pydantic.TypeAdapter(WatchResult).validate_python(dump)`, assert the outcome-string field correctly re-routes each to the right class. Also assert that a dict with an unknown `outcome` raises a `ValidationError`.
- **Action:** Add three Pydantic v2 models at the end of `src/stackly/models.py`:
  ```python
  from typing import Annotated, Literal

  class WatchException(BaseModel):
      outcome: Literal["exception"] = "exception"
      exception: ExceptionInfo

  class WatchTimedOut(BaseModel):
      outcome: Literal["timed_out"] = "timed_out"
      elapsed_s: float

  class WatchTargetExited(BaseModel):
      outcome: Literal["target_exited"] = "target_exited"
      elapsed_s: float

  WatchResult = Annotated[
      WatchException | WatchTimedOut | WatchTargetExited,
      Field(discriminator="outcome"),
  ]
  ```
  Per RESEARCH.md §5.2 — `Annotated[...] + Field(discriminator=...)` is the Pydantic v2 idiom; use `TypeAdapter(WatchResult)` for programmatic validation at call sites. Export `WatchResult` + the three concrete models from the module.
- **Acceptance criteria:**
  - Round-trip test passes for all three outcome shapes.
  - Unknown-outcome dict raises `ValidationError`.
  - `ruff check` and `pyright` clean on `models.py`.
  - No circular imports (`ExceptionInfo` already defined in the same file).
- **Size:** S
- **Dependencies:** none.

#### Task 2.5.1.2 — `DebugSession.wait_for_exception()` polling method + `_parse_lastevent_unlocked` factoring

- **Files:** `src/stackly/session.py` (extend — factor out `.lastevent` parsing; add `wait_for_exception`), `tests/test_watch_session.py` (new).
- **Failing test to write first:** `tests/test_watch_session.py::test_wait_for_exception_timeout_returns_watchtimedout` — **unit test using a fake `UserDbg` stub** (not live pybag). Stub responds to `wait(timeout)` by sleeping the timeout amount; `exec_status()` returns `"GO"`; `cmd(".lastevent")` unused. Create a DebugSession, inject the stub via `session._dbg = stub`, call `session.wait_for_exception(pid=stub.pid, poll_s=1, timeout_s=2)`, assert return is `WatchTimedOut` with `elapsed_s >= 2.0 - 0.1` and `elapsed_s <= 2.5`. Companion tests: `test_wait_for_exception_returns_watchtargetexited_on_no_debuggee` (stub sets exec_status to `"NO_DEBUGGEE"` after 1 tick → assert `WatchTargetExited`), `test_wait_for_exception_returns_watchexception_on_break_with_lastevent` (stub sets `"BREAK"` + `.lastevent` returns a real exception string → assert `WatchException` with `exception.code_name == "EXCEPTION_ACCESS_VIOLATION"`), `test_wait_for_exception_resumes_on_nonexception_break` (stub returns `"BREAK"` + empty `.lastevent` on tick 1, then `"NO_DEBUGGEE"` on tick 2 → assert `dbg.cmd("g", quiet=True)` was called once, then `WatchTargetExited`).
- **Action:** Two changes to `session.py`:
  1. **Factor out** the inner body of `get_exception()` (lines 290–318) into a private `_parse_lastevent_unlocked(self, dbg: UserDbg) -> ExceptionInfo | None` helper that takes a `dbg` parameter and does NOT acquire the lock. Re-point `get_exception()` to call the helper inside its existing `with self._lock:` block. Behavior must be byte-identical (all existing `test_session_integration.py` tests continue to pass — verify locally after this change).
  2. **Add** `wait_for_exception(self, pid: int, poll_s: int = 1, timeout_s: int | None = None, stop_check: Callable[[], bool] | None = None) -> WatchResult` following RESEARCH.md §5.3 sketch. Key details:
     - `poll_s = max(1, poll_s)` (pybag floor — see 2.5.0.1 + RESEARCH.md §2.1).
     - Acquire `self._lock` for the entire loop duration (Pattern A per RESEARCH.md §3.1). This is acceptable because a client in watch mode has no other concurrent calls to make.
     - Assert attached PID matches `pid` param via `dbg.pid`; raise `DebugSessionError(f"Session attached to pid={actual}, client asked for pid={pid}")` on mismatch.
     - `start = time.monotonic()`. Loop body: check `stop_check()` first (cancellation); check `timeout_s` deadline; call `dbg.wait(timeout=poll_s)`; inspect `dbg.exec_status()`.
       - `"NO_DEBUGGEE"` → return `WatchTargetExited(elapsed_s=time.monotonic()-start)`.
       - `"BREAK"` → call `self._parse_lastevent_unlocked(dbg)`; if non-None → `return WatchException(exception=exc)`; if None → `dbg.cmd("g", quiet=True)` + `continue`.
       - Else (`"GO"`, `"STEP_*"`) → `continue`.
     - On `timeout_s` expiry (before any BREAK) → return `WatchTimedOut(elapsed_s=time.monotonic()-start)`.
  3. Import `WatchException`, `WatchTimedOut`, `WatchTargetExited`, `WatchResult` from `stackly.models`.
- **Acceptance criteria:**
  - All four unit tests pass (timeout, target_exited, exception, non-exception-break-resume).
  - All existing `tests/test_session_integration.py` still pass (regression guard for the factoring).
  - New test `test_watch_session.py::test_wait_for_exception_rejects_mismatched_pid` passes: stub has `dbg.pid = 1234`, call `wait_for_exception(pid=9999)`, assert `DebugSessionError` raised with message matching `r"attached to pid=1234"`.
  - `ruff check` and `pyright` clean.
  - No pybag import inside the test file (the stub is a pure Python object).
- **Size:** M — flagged. If the factoring + 5 tests + 2 new methods crosses 50 min, split into (a) factor `_parse_lastevent_unlocked` + regression test, (b) implement `wait_for_exception` + all 4 unit tests.
- **Dependencies:** 2.5.1.1 (needs `WatchResult` models).

#### Task 2.5.1.3 — `watch_for_crash` async MCP tool

- **Files:** `src/stackly/tools.py` (extend), `scripts/e2e_smoke.py` (update `expected` tool set from 9 to 10), `tests/test_watch_tools.py` (new).
- **Failing test to write first:** `tests/test_watch_tools.py::test_watch_for_crash_is_async_and_offloads_blocking_work` — introspect the registered tool object: assert `asyncio.iscoroutinefunction(tool_fn)` is True (the MCP tool must be `async def`). Second test `test_watch_for_crash_calls_session_wait_for_exception_via_thread_offload`: monkeypatch `anyio.to_thread.run_sync` to record what it was called with; monkeypatch `session.wait_for_exception` to return a canned `WatchTargetExited(elapsed_s=5.0)`; invoke the tool handler with `pid=1234, poll_s=2, timeout_s=10`; assert `run_sync` was called exactly once with a partial/callable whose `.func is session.wait_for_exception` and kwargs `{'pid': 1234, 'poll_s': 2, 'timeout_s': 10}`.
- **Action:** Add to `tools.py` inside `register()`:
  ```python
  @mcp.tool()
  async def watch_for_crash(
      pid: int,
      poll_s: int = 1,
      timeout_s: int | None = None,
  ) -> WatchResult:
      """Block until a break-worthy exception fires on the attached process.

      Call after attach_process on the same MCP session. Returns a
      WatchResult discriminated union: WatchException on crash,
      WatchTimedOut on deadline expiry, WatchTargetExited on clean exit.

      Poll interval is clamped to pybag's 1-second minimum granularity;
      smaller values are silently raised to 1 second.
      """
      import anyio
      from functools import partial
      return await anyio.to_thread.run_sync(
          partial(
              session.wait_for_exception,
              pid=pid,
              poll_s=poll_s,
              timeout_s=timeout_s,
          )
      )
  ```
  Add `WatchResult` to the `stackly.models` imports at the top of `tools.py`. **The `async def` is mandatory** — RESEARCH.md §3.2; a sync tool would freeze the event loop. Update `scripts/e2e_smoke.py`'s `expected` tool set (currently 9 tools after Phase 2a.0.1's `detach_process`) to include `"watch_for_crash"` → 10 total.
- **Acceptance criteria:**
  - `inspect.iscoroutinefunction` assertion passes on the registered tool.
  - `run_sync`-monkeypatch test passes.
  - `scripts/e2e_smoke.py` runs green locally and reports 10 tools.
  - No change to existing 9 tool signatures (architectural invariant from Phase 1 + 2a).
  - Docstring documents the 1-second floor.
  - `ruff check` + `pyright` clean.
- **Size:** S
- **Dependencies:** 2.5.1.1, 2.5.1.2.

---

### Wave 2 — CLI + dispatcher integration

Wave 1 must complete before Wave 2. Within Wave 2, 2.5.2.1 (dispatcher) lands first; 2.5.2.2 (CLI) wires it in; 2.5.2.3 adds signal handlers and dedup as small amendments.

#### Task 2.5.2.1 — `watch/dispatcher.py` run_watch() one-shot path + MCP client plumbing

- **Files:** `src/stackly/watch/__init__.py` (new — empty re-export stub), `src/stackly/watch/dispatcher.py` (new), `tests/test_watch_dispatcher.py` (new).
- **Failing test to write first:** `tests/test_watch_dispatcher.py::test_run_watch_one_shot_invokes_run_handoff_on_exception` — monkeypatch (a) `ensure_gitignore` no-op, (b) `ensure_server_running` returns a fake Popen, (c) the internal `_watch_once` coroutine (or its key steps) to return a canned `WatchException(exception=ExceptionInfo(code=0xC0000005, code_name="EXCEPTION_ACCESS_VIOLATION", address=0, description="", is_first_chance=False, faulting_thread_tid=1))`, (d) `run_handoff` to return a canned `FixResult(ok=True, mode="handoff", crash_hash="abc12345", ...)` and record its call args. Call `run_watch(repo=tmp_repo, pid=1234, host="127.0.0.1", port=8585, auto=False, max_crashes=1, ...)`; assert `run_handoff` was called exactly once with `(repo=tmp_repo, pid=1234, host="127.0.0.1", port=8585, conn_str=None)`; assert the return value is 0.
- **Action:** Create `src/stackly/watch/__init__.py` as an empty stub (no eager imports; matches `fix/__init__.py` pattern). Create `src/stackly/watch/dispatcher.py` with:
  1. `async def _watch_once(pid: int, mcp_url: str, poll_s: int, timeout_s: int | None, conn_str: str | None) -> WatchResult` — opens `httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)`, passes it to `streamable_http_client(mcp_url, http_client=http_client)` (non-deprecated variant per RESEARCH.md §3.3), wraps in `ClientSession(r, w)`, calls `initialize()`, calls `attach_process` with `{"pid": pid, "conn_str": conn_str}`, calls `call_tool("watch_for_crash", {"pid": pid, "poll_s": poll_s, "timeout_s": timeout_s}, read_timeout_seconds=timedelta(days=30))`, parses `result.structuredContent` via `pydantic.TypeAdapter(WatchResult).validate_python(...)`, returns.
  2. `run_watch(repo, pid, host, port, auto, build_cmd, test_cmd, model, max_attempts, conn_str, max_crashes, max_wait_minutes, quiet) -> int` — synchronous entry. Calls `ensure_gitignore(repo)` + `ensure_server_running(host, port)` (both imported from `stackly.fix.worktree` and `stackly.fix.mcp_client` respectively). Loops `max_crashes` times calling `asyncio.run(_watch_once(...))`. Translates `timeout_s = max_wait_minutes * 60 if max_wait_minutes else None`. On each iteration, match on `result.outcome`:
     - `"exception"` → import `run_handoff` / `run_autonomous` from `stackly.fix.dispatcher` and call with the appropriate kwargs; break out of the one-shot loop if `max_crashes == 1`.
     - `"timed_out"` → log `"watch timed out after {elapsed_s:.1f}s"`; return 0.
     - `"target_exited"` → log `"target process exited cleanly"`; return 0.
     - Finally: if we spawned the server, `shutdown_server(server_proc)`.
  3. Lazy-import heavy deps (`mcp`, `httpx`, `asyncio`) inside the function body so `cli.py`'s top-level `from stackly.watch.dispatcher import run_watch` doesn't eager-load. `from stackly.fix.dispatcher import run_handoff, run_autonomous` is OK at module top (already pybag-free per Phase 2a).
- **Acceptance criteria:**
  - One-shot + `run_handoff` test passes.
  - Second unit test `test_run_watch_one_shot_invokes_run_autonomous_on_auto_flag` passes — same setup, `auto=True` → `run_autonomous` called.
  - Third unit test `test_run_watch_returns_0_on_target_exited` passes — `_watch_once` returns `WatchTargetExited` → no dispatch → exit 0.
  - Fourth unit test `test_run_watch_returns_0_on_timed_out` passes.
  - Fifth unit test `test_run_watch_uses_unbounded_read_timeout` asserts the `httpx.AsyncClient` construction uses `httpx.Timeout(..., read=None)` — inspect by monkeypatching `httpx.AsyncClient` and recording constructor kwargs. (RESEARCH.md §3.3.)
  - `from stackly.session import DebugSession` is NOT present in `src/stackly/watch/` — same architecture rule as Phase 2a.
  - `ruff check` + `pyright` clean.
- **Size:** M — flagged. If `_watch_once` + `run_watch` + 5 unit tests crosses 55 min, split into (a) `_watch_once` async + timeout test, (b) `run_watch` sync + the dispatch-routing tests.
- **Dependencies:** 2.5.1.1 (uses WatchResult), 2.5.1.3 (the tool must exist on the server side to be callable).

#### Task 2.5.2.2 — `stackly watch` Typer subcommand

- **Files:** `src/stackly/cli.py` (extend), `tests/test_watch_cli.py` (new).
- **Failing test to write first:** `tests/test_watch_cli.py::test_watch_help_shows_all_flags` — use `typer.testing.CliRunner().invoke(app, ["watch", "--help"])`; assert `exit_code == 0`; assert stdout contains each of `--pid`, `--repo`, `--conn-str`, `--build-cmd`, `--test-cmd`, `--auto`, `--host`, `--port`, `--model`, `--max-attempts`, `--max-crashes`, `--max-wait-minutes`, `--poll-seconds`, `--quiet`.
- **Action:** Add a `watch` command to `cli.py` mirroring the existing `fix` command shape. Shared flags (`--pid`, `--repo`, `--host`, `--port`, `--conn-str`, `--build-cmd`, `--test-cmd`, `--auto`, `--model`, `--max-attempts`) copy their Typer definitions verbatim from `fix`. New flags:
  - `--max-crashes: int = typer.Option(1, help="Max crashes to handle before exiting (stay-resident mode >1; default one-shot).")`
  - `--max-wait-minutes: int | None = typer.Option(None, help="Hard deadline for each watch (minutes). None = wait forever.")`
  - `--poll-seconds: int = typer.Option(1, help="Poll interval in seconds. Clamped to 1s minimum by pybag.")`
  - `--quiet: bool = typer.Option(False, help="Suppress the Rich spinner while waiting.")`
  Validates: `repo.exists()` + `is_git_repo(repo)` (imported from `stackly.fix.worktree`) + if `--auto`, `shutil.which("claude")` is on PATH (same checks as `fix`). Lazy-imports `from stackly.watch.dispatcher import run_watch` inside the function body. First line of command help (for `watch --help` summary line): `"Watch a process for crashes and auto-dispatch the fix agent."`
- **Acceptance criteria:**
  - Help test passes with all 14 flags listed.
  - Second unit test `test_watch_rejects_nonexistent_repo`: `CliRunner().invoke(app, ["watch", "--pid", "0", "--repo", "C:/nonexistent"])`, assert exit_code is 1 and stderr contains "not a git repository".
  - Third unit test `test_watch_auto_without_claude_fails`: monkeypatch `shutil.which("claude")` → `None`; `CliRunner().invoke(app, ["watch", "--pid", "0", "--repo", str(tmp_git_repo), "--auto"])`; assert exit_code 1, stderr mentions "claude CLI not found".
  - `cli.py` still imports without pulling pybag / MCP at module load (verify: `python -c "import stackly.cli"` completes in < 200ms — existing Phase 2a invariant).
- **Size:** S
- **Dependencies:** 2.5.2.1.

#### Task 2.5.2.3 — Signal handlers + dedup + stay-resident loop (>1 max-crashes)

- **Files:** `src/stackly/watch/dispatcher.py` (extend), `tests/test_watch_dispatcher.py` (extend).
- **Failing test to write first:** `tests/test_watch_dispatcher.py::test_run_watch_stay_resident_dedups_duplicate_crashes` — monkeypatch `_watch_once` to return a `WatchException` with the same `ExceptionInfo` every iteration; monkeypatch `run_handoff` to return a `FixResult` where `crash_hash == "same_hash"`; call `run_watch(..., max_crashes=3, auto=False, ...)`; assert `run_handoff` was invoked ONCE (not three times) — dedup kicked in after the first. Assert the log contains "already seen this crash, skipping dispatch" for subsequent iterations. Companion test: `test_run_watch_sigint_handler_detaches_and_exits_130` — install handler, trigger `signal.SIGINT`, assert the detach-via-MCP call was invoked, assert `SystemExit` with code 130 was raised.
- **Action:** Two amendments to `watch/dispatcher.py`:
  1. **Dedup (stay-resident only):** track `last_crash_hash: str | None = None` across iterations of the `max_crashes` loop. After a successful `run_handoff` / `run_autonomous`, read `result.crash_hash` and compare to `last_crash_hash`. If equal AND `max_crashes > 1`, log `"already seen this crash, skipping dispatch"` and `continue` to the next iteration (re-attach, re-watch). If different, update `last_crash_hash = result.crash_hash`. **Only active when `max_crashes > 1`** — one-shot mode never dedups (would be a no-op anyway since we exit after one).
  2. **Signal handlers:** add a `_WatchState` dataclass (similar to `fix/dispatcher.py`'s `_FixState`): `claude_proc: subprocess.Popen | None = None`, `server_proc: subprocess.Popen | None = None`, `did_spawn_server: bool = False`, `_handled: bool = False`. Add `_install_watch_signal_handlers(state, mcp_url)` following `fix/dispatcher.py:58–85`'s pattern. Handler: (a) if `state._handled`: return; set `state._handled = True`; (b) terminate claude_proc if running; (c) attempt a quick detach via MCP (best-effort: open a short-lived client with a 5-sec timeout, call `detach_process`; on failure swallow the exception so we still exit); (d) if `state.did_spawn_server` call `shutdown_server(state.server_proc)`; (e) `raise SystemExit(130)`. Install SIGINT on all platforms, SIGBREAK on Windows via `hasattr(signal, "SIGBREAK")`. Call `_install_watch_signal_handlers(state, mcp_url)` at the top of `run_watch` after state is constructed.
  3. **Re-attach on stay-resident iteration N+1:** the 2a dispatcher's internal `capture_crash` already re-attaches, which detaches-and-re-attaches (§4.1). So for iterations 2..N of the stay-resident loop, `_watch_once` just re-enters `attach_process` → `watch_for_crash` cleanly. Per Task 2.5.0.4's derisking finding, handle the "re-attach failed" case: if `attach_process` returns `AttachResult(status="failed", ...)`, log "target no longer attachable: {message}" and break out of the stay-resident loop with exit 0.
- **Acceptance criteria:**
  - Dedup test passes: `run_handoff` called exactly once despite 3 iterations.
  - SIGINT handler test passes: detach-via-MCP invoked, SystemExit(130) raised.
  - Third unit test `test_run_watch_stay_resident_exits_on_reattach_failure`: iteration 2's `_watch_once` raises `DebugSessionError("attach failed: ...")` → `run_watch` logs + returns 0 without further loop iterations.
  - Handler is idempotent (calling twice doesn't double-detach).
  - `compute_crash_hash` import is reused from `stackly.fix.worktree`, not re-implemented.
- **Size:** M — flagged. If the three amendments + three tests + state dataclass crosses 55 min, split into (a) dedup + loop, (b) signal handlers.
- **Dependencies:** 2.5.2.1.

---

### Wave 3 — Integration, polish, docs

Wave 2 must complete before Wave 3. Within Wave 3, all three tasks can run in parallel (no file conflicts).

#### Task 2.5.3.1 — End-to-end integration test against `crash_app`

- **Files:** `tests/test_watch_e2e.py` (new), optional: extend `tests/conftest.py` with a crashing-fixture helper if needed.
- **Failing test to write first:** `tests/test_watch_e2e.py::test_watch_dispatches_handoff_on_real_crash` (`@pytest.mark.integration` + `@pytest.mark.slow`). Steps: (a) start `crash_app wait` (the long-running fixture — PID available, not yet crashed); (b) spawn a `stackly serve` subprocess on an ephemeral port (use the same pattern as `scripts/e2e_smoke.py:52–62`); (c) monkeypatch `stackly.fix.dispatcher.run_handoff` to return a canned `FixResult(ok=True, ...)` and record invocation args (avoids needing real claude); (d) in a background thread, call `run_watch(repo=tmp_git_repo, pid=crash_app_pid, host=..., port=..., auto=False, max_crashes=1, max_wait_minutes=None, ...)`; (e) after 2 sec delay, crash the target by sending it a specific input (or launching a second `crash_app null` is not equivalent — the `wait` fixture needs to transition). **Simpler alternative:** run `crash_app null` directly — it crashes on startup. Attach, run watch — watch_for_crash returns `WatchException` quickly, dispatch fires, monkeypatched `run_handoff` records the call. Assert (f) `run_handoff` was invoked with `(repo=tmp_git_repo, pid=crash_app_pid, host=..., port=..., conn_str=None)`; (g) the `watch` thread returned 0.
- **Action:** Write the integration test to pragmatically prove the end-to-end flow. Because real claude auth adds cost + flake, monkeypatch only the dispatcher's `run_handoff`/`run_autonomous` entry — keep everything else real (actual `stackly serve`, actual pybag, actual `watch_for_crash` MCP tool, actual `WatchResult` parsing). This mirrors Phase 2a's `test_fix_e2e.py` pragmatism. Use `@pytest.mark.integration` for the auto-skip gate + `@pytest.mark.slow` for opt-in only. Reuse `crash_app_waiting` fixture from `tests/conftest.py` and supplement with a `crash_app_crashed` helper if needed.
- **Acceptance criteria:**
  - Test exists and auto-skips in CI (`integration` + `slow` markers).
  - Locally: `pytest -m "integration and slow" tests/test_watch_e2e.py` on a Windows + Debugging-Tools machine: passes.
  - Recorded invocation proves `run_handoff` was called with the right kwargs.
  - Test completes in < 30 s.
  - If `crash_app null` crashes too fast to reliably attach first, the test uses `crash_app wait` and triggers a crash via signal or stdin — document the chosen approach in the test file header comment.
- **Size:** M — flagged. If live-fixture orchestration crosses 50 min, split into (a) a simpler mock-pybag test that proves `run_watch → run_handoff` wire-up, (b) a manual `scripts/e2e_watch_smoke.py` that runs the GOAL-md-style demo. Ship (a) in CI; (b) can land later.
- **Dependencies:** 2.5.2.1, 2.5.2.2, 2.5.2.3.

#### Task 2.5.3.2 — Rich spinner + stdout polish + `scripts/e2e_smoke.py` update

- **Files:** `src/stackly/watch/dispatcher.py` (extend with Rich progress wrapper), `scripts/e2e_smoke.py` (update tool count — already done in 2.5.1.3, just verify here), optional: `scripts/e2e_watch_smoke.py` (new — analog to `scripts/e2e_fix_smoke.py`).
- **Failing test to write first:** `tests/test_watch_dispatcher.py::test_run_watch_quiet_flag_suppresses_spinner` — monkeypatch `rich.progress.Progress` constructor to record whether it was invoked; call `run_watch(..., quiet=True)` → assert Progress was NOT constructed. Then call `run_watch(..., quiet=False)` → assert Progress WAS constructed.
- **Action:** Wrap the `_watch_once` call inside `run_watch` with a `rich.progress.Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"))` context manager when `quiet=False`. Task description: `"waiting for crash on pid {pid} (tick {N})"`; update on each poll tick internally (since we can't instrument the server-side loop from the client, use elapsed time: update every 2 seconds via a small asyncio timer alongside the `call_tool` future). When `quiet=True`, skip the Progress entirely — just log one-liner `"[stackly] watching pid {pid}..."` at INFO level. Verify `scripts/e2e_smoke.py`'s `expected` tool set includes `"watch_for_crash"` (from Task 2.5.1.3). Optional: add `scripts/e2e_watch_smoke.py` that runs `stackly watch --pid <PID> --repo <REPO>` against a crashing fixture and prints a GOAL-md-style output block.
- **Acceptance criteria:**
  - Quiet-flag test passes.
  - `scripts/e2e_smoke.py` runs green locally with `expected = {10 tools}`.
  - Watch output when `quiet=False` shows a running spinner; when `quiet=True` shows only the one-liner.
  - No new dependencies.
- **Size:** S
- **Dependencies:** 2.5.2.3.

#### Task 2.5.3.3 — README section, version bump, CHANGELOG, doctor-no-change assertion

- **Files:** `README.md` (extend), `src/stackly/__init__.py` (bump to `0.2.5` or `0.3.0`), `pyproject.toml` (bump version), `CHANGELOG.md` (extend), `tests/test_version.py` (existing test — must still pass), `tests/test_doctor_unchanged.py` (new — assertion that doctor behavior is unchanged).
- **Failing test to write first:** `tests/test_doctor_unchanged.py::test_doctor_does_not_add_new_environment_checks` — use `typer.testing.CliRunner().invoke(app, ["doctor"])`; capture stdout; assert the printed Rich-table rows exactly match the set `{"dbgeng", "cdb", "claude CLI", "claude bypass ack'd"}` (Phase 1 + 2a checks, no new 2.5 items). Rationale: CONTEXT.md states no new external deps; `doctor` must remain unchanged.
- **Action:**
  1. Add a "Watch mode" section to `README.md` directly after the existing "Fix-loop" section. One paragraph overview of `stackly watch`, the one-shot command, the `--auto` autonomous auto-dispatch command, the stay-resident `--max-crashes` example, the `--max-wait-minutes` example, and a note about the 1-second poll floor.
  2. Update `__version__` in `src/stackly/__init__.py` and `pyproject.toml` to the next version (recommend `0.2.5` to match the phase number; or `0.3.0` if preferring minor-bumps-per-phase — verify Phase 2a used `0.2.0` and pick the next available number).
  3. Add a `## 0.2.5 — Crash auto-detection` (or `0.3.0`) section to `CHANGELOG.md` listing: `watch` command, `watch_for_crash` MCP tool, `WatchResult` discriminated union, `DebugSession.wait_for_exception` method.
  4. Verify `tests/test_version.py` still passes after the bump.
  5. Add `tests/test_doctor_unchanged.py` with the assertion above — this encodes the "no new external deps" constraint from CONTEXT.md as a regression test.
- **Acceptance criteria:**
  - README section exists with three example commands.
  - Version bump reflected in both `__init__.py` and `pyproject.toml` (equal).
  - `tests/test_version.py` passes.
  - `tests/test_doctor_unchanged.py` passes.
  - CHANGELOG.md has a new section listing the four deliverables.
- **Size:** S
- **Dependencies:** All prior 2.5 tasks (final polish).

---

## 5. Goal-backward verification

Every CONTEXT.md locked decision is produced by at least one task.

| # | Locked decision (from CONTEXT.md) | Evidence-producing task(s) |
|---|----------------------------------|---------------------------|
| 1 | Polling loop on `dbg.wait(timeout)` is the primary path; EventHandler is deferred | 2.5.0.1 (empirical derisk), 2.5.1.2 (`wait_for_exception`), 2.5.0.3 (non-exception-break handling); EventHandler explicitly absent from all tasks |
| 2 | `stackly watch --pid N --repo PATH` subcommand exists with full flag surface | 2.5.2.2 (CLI wiring), 2.5.2.2 help-text test enumerates every flag |
| 3 | `watch_for_crash` MCP tool added (10th tool), async, wraps `anyio.to_thread.run_sync` | 2.5.1.3 (tool), 2.5.0.2 (FastMCP async derisk), 2.5.1.3 acceptance: `inspect.iscoroutinefunction` |
| 4 | On crash: calls `run_handoff` / `run_autonomous` directly; no duplicated capture/briefing | 2.5.2.1 (dispatcher wiring), 2.5.2.1 test asserts import is from `stackly.fix.dispatcher` |
| 5 | Same `.stackly/` layout; `watch` + `fix` artifacts indistinguishable | 2.5.2.1: calls `ensure_gitignore` from `fix.worktree`; all artifacts flow through the 2a dispatcher which writes under `.stackly/` |
| 6 | `watch_for_crash` takes DebugSession lock for duration of `dbg.wait()` | 2.5.1.2: `wait_for_exception` acquires `self._lock` before the loop; documented in the method docstring |
| 7 | One-shot default; `--max-crashes N` for stay-resident; dedup via crash_hash | 2.5.2.2 (flag), 2.5.2.3 (dedup + stay-resident loop); test `test_run_watch_stay_resident_dedups_duplicate_crashes` |
| 8 | Signal handler: SIGINT + SIGBREAK, terminates Claude, detaches pybag, exits 130 | 2.5.2.3: `_install_watch_signal_handlers`; test `test_run_watch_sigint_handler_detaches_and_exits_130` |
| 9 | MCP client uses unbounded HTTP read timeout (via custom httpx.AsyncClient) | 2.5.2.1: `httpx.Timeout(30.0, read=None)` in `_watch_once`; test `test_run_watch_uses_unbounded_read_timeout` verifies constructor kwargs |
| 10 | `WatchResult` discriminated union for timeout / target-exited / exception outcomes | 2.5.1.1 (models); test `test_watch_result_discriminated_union_round_trip` |
| 11 | `poll_s` (seconds), not `poll_ms`, with 1-sec floor | 2.5.0.1 (derisk confirms units), 2.5.1.2 (`poll_s = max(1, poll_s)` in `wait_for_exception`), 2.5.1.3 (tool docstring) |
| 12 | `doctor` behavior unchanged (no new deps) | 2.5.3.3: `tests/test_doctor_unchanged.py` asserts no new rows |
| 13 | `scripts/e2e_smoke.py` sees 10 tools | 2.5.1.3 updates `expected` set; 2.5.3.2 verifies |

**must_haves (for gsd-verifier):**
```yaml
must_haves:
  truths:
    - "A developer can run `stackly watch --pid N --repo PATH` and it blocks until the target crashes."
    - "When the target throws an exception, the 2a fix dispatcher is invoked with the same pid + repo, producing a briefing (hand-off) or .patch file (--auto)."
    - "If the target exits cleanly before any exception, `watch` returns 0 and logs 'target process exited cleanly'."
    - "If the user passes --max-wait-minutes N and no crash fires within N minutes, `watch` returns 0 and logs 'watch timed out after Ns'."
    - "Ctrl-C during wait terminates any in-flight Claude Code child, detaches pybag, and exits 130."
    - "In stay-resident mode (--max-crashes > 1), a duplicate crash_hash within one watch invocation is not re-dispatched."
    - "The MCP server exposes exactly 10 tools, including `watch_for_crash`, and `scripts/e2e_smoke.py` validates this."
    - "`stackly doctor` reports the same set of environment checks as Phase 2a (no new external deps)."
  artifacts:
    - path: "src/stackly/models.py"
      provides: "WatchResult discriminated union (WatchException | WatchTimedOut | WatchTargetExited)"
      contains: "class WatchException"
    - path: "src/stackly/session.py"
      provides: "wait_for_exception method + _parse_lastevent_unlocked helper"
      contains: "def wait_for_exception"
    - path: "src/stackly/tools.py"
      provides: "async watch_for_crash MCP tool registered on FastMCP"
      contains: "async def watch_for_crash"
    - path: "src/stackly/watch/dispatcher.py"
      provides: "run_watch orchestrator + _watch_once MCP-client coroutine + signal handlers"
      contains: "def run_watch"
    - path: "src/stackly/cli.py"
      provides: "`watch` Typer subcommand with 14 flags"
      contains: "def watch"
    - path: "tests/test_watch_e2e.py"
      provides: "@pytest.mark.integration test proving live crash_app → watch → dispatch"
      contains: "def test_watch_dispatches_handoff_on_real_crash"
  key_links:
    - from: "src/stackly/tools.py"
      to: "src/stackly/session.py"
      via: "anyio.to_thread.run_sync(session.wait_for_exception, ...)"
      pattern: "anyio\\.to_thread\\.run_sync"
    - from: "src/stackly/watch/dispatcher.py"
      to: "src/stackly/fix/dispatcher.py"
      via: "from stackly.fix.dispatcher import run_handoff, run_autonomous"
      pattern: "from stackly\\.fix\\.dispatcher import"
    - from: "src/stackly/watch/dispatcher.py"
      to: "src/stackly/fix/worktree.py"
      via: "from stackly.fix.worktree import compute_crash_hash, ensure_gitignore"
      pattern: "compute_crash_hash"
    - from: "src/stackly/cli.py"
      to: "src/stackly/watch/dispatcher.py"
      via: "lazy import inside `watch` command body"
      pattern: "from stackly\\.watch\\.dispatcher import run_watch"
    - from: "src/stackly/watch/dispatcher.py"
      to: "httpx.AsyncClient"
      via: "httpx.Timeout(30.0, read=None) passed to streamable_http_client"
      pattern: "httpx\\.Timeout\\([^)]*read=None"
```

## 6. Risk register (plan-specific)

Risks specific to Phase 2.5 — generic risks from PROJECT.md and 2a's plan are inherited.

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|------------|--------|------------|
| R1 | pybag `dbg.wait(timeout)` actually takes milliseconds on some version, invalidating the 1-sec floor | Low (source-read HIGH confidence) | Poll loop misses crashes by minutes | Task 2.5.0.1 empirically validates on the actual install before any downstream work; calibration numbers recorded in the test file. |
| R2 | FastMCP async-tool + anyio.to_thread.run_sync doesn't actually unblock the event loop | Low (source-read HIGH confidence) | Server freezes for entire watch duration; "share the attach" design broken | Task 2.5.0.2 empirically validates with a minimal server+client test. If it fails, phase aborts before tool-wiring and redesigns as client-driven polling. |
| R3 | Module-load breaks (`BREAK` + no exception) flood the polling loop, burning CPU | Medium | Watch consumes 100% CPU waiting on a target that's starting up | Task 2.5.0.3 validates the non-exception-break + `dbg.cmd("g")` pattern; if flooding observed, Task 2.5.1.2 adds `SetEngineOptions` tuning. |
| R4 | Re-attach after a crash is unreliable; stay-resident mode's utility is limited | Medium | `--max-crashes > 1` fails silently on certain crash types | Task 2.5.0.4 empirically documents the behavior; Task 2.5.2.3 handles both outcomes (detect `AttachResult.status=="failed"` → exit cleanly). `--help` text for `--max-crashes` documents the limitation. |
| R5 | httpx / mcp API drifts: `streamable_http_client` parameter name changes between mcp 1.27.x patches | Low | `_watch_once` raises a TypeError on construct | Pinned `mcp[cli]>=1.27,<1.28` in pyproject.toml. Task 2.5.2.1 test monkeypatches the constructor signature — drift is detected locally before ship. |
| R6 | `compute_crash_hash` gives different hashes for the same crash across `watch`-triggered and `fix`-triggered captures (dedup breaks) | Low | Stay-resident mode re-dispatches when it shouldn't | Both paths use the same `compute_crash_hash` via `fix.worktree` — dedup is deterministic by construction. Unit test `test_run_watch_stay_resident_dedups_duplicate_crashes` verifies equality. |
| R7 | SIGBREAK on Windows doesn't propagate to `anyio.to_thread.run_sync`-offloaded thread; Ctrl-C takes > 1 sec | Medium | Slow Ctrl-C response (up to poll_s seconds, typically 1 s) | Documented as an accepted limitation (RESEARCH.md §4.2). `_install_watch_signal_handlers` handles the main-thread signal; the offloaded thread cooperatively checks `stop_check` between ticks. |
| R8 | Factoring `_parse_lastevent_unlocked` introduces a regression in `get_exception()` | Low | Phase 1 integration tests fail | Task 2.5.1.2 acceptance criteria explicitly includes "all existing `tests/test_session_integration.py` still pass." Run locally before committing. |
| R9 | `--max-wait-minutes` deadline fires between poll ticks but server thinks it's still waiting | Low | Client receives WatchTimedOut but server's thread keeps the lock for an extra second | The deadline check is inside the offloaded thread, between ticks; server releases the lock on the natural loop exit. Worst-case: 1 tick of latency (acceptable). |
| R10 | Contributors are tempted to add `EventHandler.exception()` because "it's in pybag anyway" | Medium | Threading deadlock, phase scope creep | Architecture Decision #1 explicitly pins polling; §2 of this plan and RESEARCH.md §2.4 both call out EventHandler as out of scope. Task 2.5.1.3 has no EventHandler code — code review catches any drift. |

## 7. Verification plan — proving Phase 2.5 is done

Phase 2.5 exits when all CONTEXT.md locked decisions have evidence (§5 matrix) and the test matrix below is green.

### Automated tests (run via `uv run pytest`)

1. All unit tests pass (no integration markers): `uv run pytest -m "not integration"` — exit 0. New test files: `test_watch_models.py`, `test_watch_session.py`, `test_watch_tools.py`, `test_watch_dispatcher.py`, `test_watch_cli.py`, `test_doctor_unchanged.py`.
2. Phase 1 + Phase 2a tests still pass: same command confirms no regression. Especially critical: `test_session_integration.py::test_get_exception_*` (proves `_parse_lastevent_unlocked` factoring is byte-identical).
3. Derisk tests (Wave 0) pass: `uv run pytest -m integration tests/test_watch_derisk_*.py` — exit 0 on Windows + Debugging Tools.
4. Integration tests: `uv run pytest -m "integration and not slow"` — exit 0. Includes dispatcher-orchestration tests using monkeypatched claude.
5. Slow/E2E tests (local + Windows + claude authenticated): `uv run pytest -m "integration and slow"` — at least `test_watch_dispatches_handoff_on_real_crash` passes.
6. Lint + typecheck: `uv run ruff check`, `uv run pyright` — both clean on all new files.
7. Architecture-invariant test: `tests/test_import_constraints.py` (Phase 2a's CI grep) still passes — `src/stackly/watch/` must not import `from stackly.session` (same rule as `fix/`). Consider extending the CI grep to cover `src/stackly/watch/` in Task 2.5.3.3 if not automatically covered.

### Manual exit demo

Run the following on a fresh Windows dev machine with Debugging Tools + claude installed:

```powershell
# Terminal A — start a watch (no crash yet):
D:\Projects\BridgeIt\tests\fixtures\crash_app\build\Debug\crash_app.exe wait
# (blocks; note PID)

# Terminal B — start watch (one-shot, hand-off mode):
stackly watch --pid <PID> --repo D:/Projects/BridgeIt

# Terminal A — trigger the crash by sending input (or launch crash_app null in place of wait):
# the process crashes, terminal A exits with EXCEPTION_ACCESS_VIOLATION

# Terminal B — expect:
# [stackly] attaching to pid <PID>...
# [stackly] watching pid <PID> (1-sec poll)
#   ⠋ waiting for crash on pid <PID> (tick 3)
# [stackly] crash detected: EXCEPTION_ACCESS_VIOLATION
# [stackly] dispatching fix (hand-off mode)...
# [claude-code] opens interactively with briefing pre-loaded
```

Autonomous-mode demo (using the same `crash_app null` crash):

```powershell
stackly watch --pid <PID> --repo D:/Projects/BridgeIt `
    --auto `
    --build-cmd "cmake --build tests/fixtures/crash_app/build --config Debug"
# Expected: same watch flow, then autonomous loop per Phase 2a,
# terminates with a `.stackly/patches/crash-<hash>.patch` written.
```

Timeout-exit demo:

```powershell
stackly watch --pid <PID> --repo D:/Projects/BridgeIt --max-wait-minutes 1
# (no crash fires within 60 s)
# Expected: "[stackly] watch timed out after 60.0s" + exit 0.
```

### What counts as "shipped"

- All automated tests listed above are green.
- The manual one-shot hand-off demo shows Claude Code opening with the briefing after a crash.
- The manual autonomous demo produces a `.patch` file ≥ 1 line after a crash.
- The timeout demo exits cleanly after `--max-wait-minutes` elapses.
- `stackly doctor` output is identical to Phase 2a's (no new checks).
- `scripts/e2e_smoke.py` lists 10 tools including `watch_for_crash`.
- README has a "Watch mode" section.
- Git tag `v0.2.5-phase2.5` (or `v0.3.0-phase2.5`) pushed on `main`.
- `tests/test_doctor_unchanged.py` regression-guards the "no new deps" constraint.

## 8. Explicit non-goals (from CONTEXT.md Deferred Ideas)

These are **deliberately** not in Phase 2.5. Do not add tasks for them.

- **Multi-PID daemon mode** (`stackly watchd` watching N processes). Own phase — adds process-tree management, inter-process IPC, log routing.
- **Windows AeDebug JIT registry integration** for postmortem capture of processes that weren't pre-attached. Admin-required, system-wide side effects, distinct UX — warrants its own phase.
- **Auto-spawn-and-watch** (`stackly watch --cmd "my_app.exe --flag"` launches the target under the watcher). Useful but adds process-lifetime management orthogonal to attach-and-wait.
- **ETW (Event Tracing for Windows) subscription** as an alternative detection path. Richer event data, no attach needed, but a totally different plumbing stack.
- **Tiered Haiku→Sonnet→Opus routing** on auto-detected crashes. Phase 4 cost-optimization item; deferred in Phase 2a as well.
- **`claude --resume` persistent session resumption across multiple crashes** in stay-resident mode. Interesting but premature.
- **`pybag.dbgeng.callbacks.EventHandler.exception()` callback-based detection.** Explicitly deferred per CONTEXT.md + RESEARCH.md §2.4 (threading + non-reentrancy risk not worth the latency savings for a 1-sec poll).

---

## Appendix A — Constraints cross-check

| Constraint | Where honored |
|-----------|---------------|
| No breaking change to the 9 existing MCP tools | Task 2.5.1.3 adds `watch_for_crash` as a new tool; doesn't modify existing signatures. `scripts/e2e_smoke.py`'s `expected` set grows 9 → 10 with no existing names changed. |
| Pybag imports stay lazy | Task 2.5.1.2 only adds a method to `DebugSession` (pybag import is already lazy via `_make_userdbg`). Task 2.5.2.2's `cli.py` `watch` command lazy-imports `from stackly.watch.dispatcher import run_watch` inside the function, preserving the Phase 1 invariant. |
| No direct `from stackly.session import DebugSession` in `src/stackly/watch/` | Task 2.5.2.1 explicitly avoids the import. Phase 2a's CI grep step (`test_fix_does_not_import_debugsession`) should be extended in Task 2.5.3.3 to cover `src/stackly/watch/` as well — or a companion test added. |
| No new Python deps in `pyproject.toml` | **Zero new deps in Phase 2.5.** All functionality uses existing deps: `anyio` (comes with `mcp`), `httpx` (comes with `mcp`), `mcp[cli]`, `pydantic`, `typer`, `rich`, stdlib (`subprocess`, `hashlib`, `signal`, `asyncio`, `time`, `functools`). |
| All new production code under `src/stackly/watch/` | Verified — only `src/stackly/watch/__init__.py` and `src/stackly/watch/dispatcher.py` are new files. Exceptions (small surgical additions to Phase 1/2a modules): `session.py` (add `wait_for_exception` + factor `_parse_lastevent_unlocked`), `tools.py` (add `watch_for_crash` tool), `models.py` (add `WatchResult`), `cli.py` (add `watch` subcommand). |
| Tests under `tests/` with `test_*.py` + integration-mark pattern | New test files: `test_watch_models.py`, `test_watch_session.py`, `test_watch_tools.py`, `test_watch_dispatcher.py`, `test_watch_cli.py`, `test_watch_derisk_timeout_units.py`, `test_watch_derisk_async_offload.py`, `test_watch_derisk_nonexception_break.py`, `test_watch_derisk_reattach.py`, `test_watch_e2e.py`, `test_doctor_unchanged.py`. All follow the pattern. |
| `doctor` behavior unchanged | Task 2.5.3.3's `tests/test_doctor_unchanged.py` encodes this as a regression test. |

## Appendix B — Task dependency graph (summary)

Legend: `A -> B` means A must complete before B starts.

```
Wave 0 (derisking — MUST run first):
  2.5.0.1 (poll timeout units)           [no deps]
  2.5.0.2 (FastMCP async offload)        [no deps]
  2.5.0.3 (non-exception-break handling) [no deps]
  2.5.0.4 (re-attach after crash)        [no deps — lower priority, feeds 2.5.2.3]

Wave 1 (foundation — parallel where possible):
  2.5.0.1, 2.5.0.3           -> 2.5.1.2 (wait_for_exception + lastevent factoring)
  (none)                     -> 2.5.1.1 (WatchResult models)
  2.5.1.1, 2.5.1.2, 2.5.0.2  -> 2.5.1.3 (async watch_for_crash MCP tool)

Wave 2 (CLI + dispatcher):
  2.5.1.1, 2.5.1.3                -> 2.5.2.1 (watch/dispatcher.py — one-shot + MCP client)
  2.5.2.1                         -> 2.5.2.2 (cli.py watch subcommand)
  2.5.2.1, 2.5.0.4                -> 2.5.2.3 (signal handlers + dedup + stay-resident)

Wave 3 (integration + polish — all parallel):
  2.5.2.1, 2.5.2.2, 2.5.2.3 -> 2.5.3.1 (e2e integration test)
  2.5.2.3                   -> 2.5.3.2 (Rich spinner + e2e_smoke.py polish)
  all prior                 -> 2.5.3.3 (README + version + doctor regression test)
```

Critical path length (minimum serial): 7 tasks = `2.5.0.1 → 2.5.1.2 → 2.5.1.3 → 2.5.2.1 → 2.5.2.2 → 2.5.2.3 → 2.5.3.3`. Wall-clock budget: ~30 min per S task, ~45 min per M task → ~4–5 hours of `gsd-executor` time on the critical path, with Wave 0 (4 S tasks) + Wave 3 (2 S tasks + 1 M task) parallelizable alongside for a realistic total of ~5–6 hours end-to-end.

Tasks ordered for execution (one possible serialization that respects all dependencies):

`[2.5.0.1, 2.5.0.2, 2.5.0.3, 2.5.0.4]` → `[2.5.1.1]` → `[2.5.1.2]` → `[2.5.1.3]` → `[2.5.2.1]` → `[2.5.2.2, 2.5.2.3]` → `[2.5.3.1, 2.5.3.2, 2.5.3.3]`.
