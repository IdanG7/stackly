# Phase 1 — Tech Stack & Architecture Map

**Analysis Date:** 2026-04-15
**Scope:** Everything shipped in commit `d514fb4` on `main`. This is the foundation Phase 2a (`debugbridge fix`) will build on top of.

---

## 1. Package layout

```
D:\Projects\BridgeIt\
├── pyproject.toml                         # uv_build backend, mcp[cli] 1.27.x, Pybag 2.2.16
├── uv.lock                                # pinned
├── README.md, LICENSE (MIT)
├── dist\                                  # pre-built wheel + sdist (0.1.0)
│   ├── debugbridge-0.1.0-py3-none-any.whl
│   └── debugbridge-0.1.0.tar.gz
├── src\debugbridge\
│   ├── __init__.py        (3 lines)      # __version__ = "0.1.0"
│   ├── __main__.py        (6 lines)      # python -m debugbridge → cli.app
│   ├── cli.py             (116 lines)    # Typer CLI: serve / doctor / version
│   ├── env.py             (88 lines)     # Debugging Tools detection + PATH injection
│   ├── models.py          (81 lines)     # 7 Pydantic wire-contract types
│   ├── session.py         (445 lines)    # The core: pybag UserDbg wrapper + parsers
│   ├── server.py          (46 lines)     # FastMCP wiring (build_app, run)
│   └── tools.py           (110 lines)    # 8 MCP tool adapters
├── tests\
│   ├── conftest.py        (81 lines)     # auto-skip logic + crash_app_waiting fixture
│   ├── test_env.py        (52 lines)
│   ├── test_models.py     (61 lines)
│   ├── test_parsers.py    (96 lines)     # regex parsers, no pybag
│   ├── test_session_integration.py (116 lines)  # @pytest.mark.integration
│   └── fixtures\crash_app\
│       ├── crash.cpp      (76 lines)     # null/stack/throw/wait modes
│       ├── CMakeLists.txt (20 lines)     # MSVC /Zi /Od /MTd, /DEBUG /INCREMENTAL:NO
│       ├── build.ps1      (20 lines)     # VS 2022 x64 generator → build/Debug/
│       └── build\Debug\crash_app.exe     # already built
├── scripts\
│   ├── e2e_smoke.py       (178 lines)    # Full MCP client drive-test
│   └── install-debugging-tools.ps1       # downloads Windows SDK, admin required
└── .github\workflows\ci.yml              # windows-latest; skips integration tests
```

Totals: ~900 lines of production code, ~400 lines of tests, ~180 lines e2e script.

---

## 2. Module-by-module reference

### `src/debugbridge/__init__.py`

Three lines. Exports `__version__ = "0.1.0"`. No imports. Safe to touch from anywhere (does NOT pull pybag transitively — that lives in `session.py` and is lazily imported there).

### `src/debugbridge/__main__.py`

Six lines. Enables `python -m debugbridge` by importing `cli.app` and calling it. Not critical for Phase 2a.

### `src/debugbridge/cli.py`

Typer CLI with three commands, wired through a single `typer.Typer(name="debugbridge", no_args_is_help=True)` instance at `cli.py:19-24`. Exposed as a console script via `pyproject.toml:38` → `debugbridge = "debugbridge.cli:app"`.

- **`serve`** (`cli.py:29-67`): Starts the MCP server. Takes `--transport {http,stdio}` (default `http`), `--host` (default `127.0.0.1`), `--port` (default `8585`), `--skip-env-check` (bypasses `check_debugging_tools()`). At `cli.py:48-56` runs `check_debugging_tools()` and aborts with install guidance if tools are missing. At `cli.py:59` the server module (`from debugbridge.server import run`) is imported **lazily**, so `serve` is the only command that ever touches pybag. Prints `DebugBridge serving on http://{host}:{port}/mcp` before calling `run(transport, host, port)`.

- **`doctor`** (`cli.py:70-95`): Runs `check_debugging_tools()` and renders a `rich.Table` with each component's status (`found`/`missing`) and resolved path. Exits 0 when all are present, 1 otherwise. Does NOT import pybag.

- **`version`** (`cli.py:98-101`): Prints `debugbridge 0.1.0`. No imports beyond the module.

There is also a `main()` wrapper at `cli.py:104-112` that converts untrapped exceptions to `sys.exit(1)`.

### `src/debugbridge/env.py`

Zero-dependency-on-pybag environment detector. Safe on any platform (though the canonical path is Windows-only).

- **Canonical path** (`env.py:17`): `C:\Program Files (x86)\Windows Kits\10\Debuggers\x64`. pybag hard-codes this same path when loading `dbgeng.dll`.
- **Required components** (`env.py:21-22`): exes `dbgsrv.exe`, `cdb.exe`; DLLs `dbgeng.dll`, `symsrv.dll`, `dbghelp.dll`.
- **`EnvCheckResult`** dataclass (`env.py:34-41`): `ok: bool`, `found: dict[str, str]`, `missing: list[str]`, `guidance: str | None`.
- **`check_debugging_tools()`** (`env.py:55-73`): for each required component, calls `_find_on_path_or_canonical()` which does `shutil.which(name)` first, falling back to `CANONICAL_DEBUGGERS_X64 / name` existence check. Returns an `EnvCheckResult`.
- **`ensure_dbgeng_on_path()`** (`env.py:76-88`): idempotent PATH prepender. If the canonical debuggers dir exists and is not already in `PATH`, prepends it so `ctypes.windll.LoadLibrary` (used by pybag) will find `dbgeng.dll`. Called at the top of `DebugSession._make_userdbg()` and at the top of `conftest.py`'s collection hook.

### `src/debugbridge/models.py`

Seven Pydantic v2 models that form the wire contract returned to MCP clients. Tools in `tools.py` must return one of these; no ad-hoc dicts.

1. **`AttachResult`** (`models.py:15-22`): `pid: int`, `process_name: str | None`, `is_remote: bool = False`, `status: Literal["attached","failed"]`, `message: str | None`.
2. **`CallFrame`** (`models.py:25-33`): `index: int` (0 = innermost), `function: str | None`, `module: str | None`, `file: str | None`, `line: int | None`, `instruction_pointer: int`.
3. **`ThreadInfo`** (`models.py:36-42`): `id: int` (DbgEng internal index), `tid: int` (Windows TID), `state: str` ("running"/"stopped"/"exited"/"unknown"), `is_current: bool = False`, `frame_count: int | None`.
4. **`ExceptionInfo`** (`models.py:45-54`): `code: int`, `code_name: str`, `address: int`, `description: str = ""`, `is_first_chance: bool = True`, `faulting_thread_tid: int | None`.
5. **`Local`** (`models.py:57-64`): `name: str`, `type: str`, `value: str` (stringified, may be truncated), `address: int | None`, `truncated: bool = False`.
6. **`Breakpoint`** (`models.py:67-74`): `id: int`, `location: str` (e.g. `"module!symbol"` or `"file.cpp:42"`), `enabled: bool = True`, `hit_count: int = 0`, `address: int | None`.
7. **`StepResult`** (`models.py:77-81`): `status: Literal["stopped","crashed","exited"]`, `current_frame: CallFrame | None`.

Phase 2a will likely need to extend these (e.g. add a `Briefing` model or bundle exception+stack+locals). Nothing in the current models is marked internal/private — all fields are part of the MCP wire format.

### `src/debugbridge/session.py`

The heart of DebugBridge. 445 lines, one class (`DebugSession`), one helper exception, and three module-level regex parsers. Read this file before touching anything in Phase 2a that interacts with the debugger.

**Key design invariants (from the module docstring at `session.py:1-18`):**
- This is the **only** module allowed to import pybag.
- pybag imports are **lazy** (inside methods, not at module top) because `pybag/__init__.py` tries to `LoadLibrary("dbgeng.dll")` at import time and raises `FileNotFoundError` if the DLL is absent.
- Some crash-triage data is extracted by running WinDbg commands via `dbg.cmd(...)` and parsing the text output, because pybag's wrappers for `GetLineByOffset` and `GetLastEventInformation` raise `E_NOTIMPL_Error`.

**Regex parsers (module-level):**
- `_FRAME_LINE_RE` (`session.py:74-83`): matches a stack-trace line from `kn f`: `"00 00000053`abc12340 00007ff6`12341234 myapp!crash_null+0x2a [c:\src\crash.cpp @ 42]"`. Captures `idx`, `child` (childEBP), `ret` (return addr), `sym` (`module!symbol+disp`), optional `file`/`line`.
- `_LASTEVENT_RE` (`session.py:89-94`): matches `".lastevent"` output: `"Last event: 1234.5678: Access violation - code c0000005 (first chance)"`. Captures `pid`, `tid`, `desc`, `code`, `chance`.
- `_EXR_ADDR_RE` (`session.py:102`): matches `"ExceptionAddress: 00007ff6..."` from `.exr -1` output.
- `_EXCEPTION_CODE_NAMES` dict (`session.py:47-59`): 11 well-known NTSTATUS codes (AV, stack overflow, breakpoint, single-step, div-by-zero, int-overflow, illegal-instruction, noncontinuable, invalid-disposition, invalid-handle, C++ `0xE06D7363`). `_decode_exception_code()` at `session.py:62-64` looks them up, falling back to `0x%08X`.

**`DebugSession` class (`session.py:105-445`):**

- **State** (`session.py:112-116`): `self._lock: threading.Lock`, `self._dbg: UserDbg | None`, `self._is_remote: bool`, `self._process_name: str | None`. One session per server process; re-attaching calls `_close_locked()` first.

- **`_make_userdbg()`** (`session.py:120-129`): calls `ensure_dbgeng_on_path()`, then `from pybag.userdbg import UserDbg` and returns `UserDbg()`. Must be called inside the lock.

- **`attach_local(pid)`** (`session.py:131-151`): closes existing `UserDbg`, constructs a new one, calls `self._dbg.attach(pid, initial_break=True)`, stores process name. `initial_break=True` pauses the target so subsequent queries can inspect it; for an already-crashed process the crash event takes precedence over the initial break.

- **`attach_remote(conn_str, pid)`** (`session.py:153-170`): same flow as `attach_local` but first calls `self._dbg.connect(conn_str)` (e.g. `"tcp:server=192.168.1.10,port=5555"`). **This code path exists but is not exercised in CI** (per `PROJECT.md:66`).

- **`close()` / `_close_locked()`** (`session.py:172-184`): detach, Release COM, set `_dbg = None`. Both wrapped in `contextlib.suppress(Exception)` because teardown on a dead process can throw.

- **`_lookup_process_name(pid)`** (`session.py:186-196`): iterates `dbg.proc_list()` to find the name for a pid. Best-effort; returns `None` on failure.

- **`get_callstack(max_frames=64)`** (`session.py:200-215`): calls `dbg.cmd(".lines -e", quiet=True)` to turn on source-line annotation, then `dbg.cmd("kn f", quiet=True)` to get the stack. Parses with `_FRAME_LINE_RE`. Falls back to `_fallback_backtrace()` (which uses `dbg.backtrace_list()` + `dbg.get_name_by_offset()`) when the text parser finds zero frames — but the fallback loses file/line info.

- **`_parse_callstack()`** (`session.py:217-247`): walks the `kn f` output line-by-line, matches `_FRAME_LINE_RE`, splits `module!symbol+disp`, converts hex addresses. Note: uses `ret` (return address) as the `instruction_pointer` field — the parser comment at `session.py:227-229` acknowledges that "ChildEBP isn't the instruction pointer" but it's the closest reliably-available column, and a truer IP "would need stepping per-frame — out of scope for MVP."

- **`_split_sym()`** (`session.py:249-255`): splits `"module!symbol+0x2a"` into `("module", "symbol+0x2a")`. Returns `(None, sym or None)` when there's no `!`.

- **`_fallback_backtrace()`** (`session.py:257-274`): iterates `dbg.backtrace_list()`, resolves `f.InstructionOffset` via `dbg.get_name_by_offset()`. No file/line info. Used when text-parse fails.

- **`get_exception()`** (`session.py:276-306`): runs `.lastevent`, parses code/tid/desc/chance. Then runs `.exr -1` and parses the faulting address. Returns `None` if `.lastevent` didn't match (e.g. no exception has fired). Warning in-code at `session.py:287-289`: "only meaningful if the last event was actually an exception — for a manual break the ExceptionRecord may be stale."

- **`get_threads()`** (`session.py:308-327`): calls `dbg.get_thread()` for current index, then reaches into `dbg._systems.GetThreadIdsByIndex()` (private pybag attribute) because `thread_list()` doesn't return the DbgEng thread INDEX. Builds `ThreadInfo` rows with `state` set only for the current thread (via `dbg.exec_status().lower()`), others are `"unknown"`. `frame_count` is always `None` — would require per-thread switch, deemed too expensive.

- **`get_locals(frame_index=0)`** (`session.py:329-344`): runs `.frame {n}` to switch scope, captures `dv /t /v` (dump vars with type + value), restores `.frame 0`. Parses with `_parse_locals()`. **Acknowledged limitation**: DbgEng renders STL containers as raw memory (`std::string`/`std::vector` appear as opaque binary); primitives / pointers / POD structs come through correctly. Phase 2 is mentioned as the tracking target in the docstring.

- **`_parse_locals()`** (`session.py:346-383`): parses `dv /t /v` output lines shaped like `00000053`abc12348  int i = 0n42` or `00000053`abc12350  char * name = 0x... "hello"`. Address is the first whitespace-separated token; split-on-first-`=` gives left (type+name) and right (value); `rsplit(None, 1)` pulls name off the right of the left side. Lines without `=` are skipped. Values over 256 chars are truncated to 256 + `"…"` with `truncated=True`.

- **`set_breakpoint(location)`** (`session.py:387-401`): calls `dbg.bp(location)` (pybag), uses `_safe_get(bp, "GetId")` and `_safe_get(bp, "GetOffset")` because the breakpoint object's attrs depend on the comtypes wrapper. Returns `Breakpoint(id=..., location=..., enabled=True, hit_count=0, address=...)` with `id=-1` / `address=None` if the safe getters fail.

- **`step_over()`** (`session.py:403-421`): calls `dbg.stepo(count=1)`, queries `dbg.exec_status().lower()`, maps to `"exited"` if `"no_debuggee"` in status, `"crashed"` if `"break"` and `.lastevent` output contains `"chance"`, else `"stopped"`. Returns `StepResult(status, current_frame=_fallback_backtrace(max=1))`.

- **`continue_execution()`** (`session.py:423-428`): runs `dbg.cmd("g", quiet=True)`. Intentionally uses the command instead of `dbg.go()` because `go()` blocks until the next event — the comment notes "for 'just resume' behavior we set the status without waiting."

- **`_require_attached()`** (`session.py:432-435`): raises `DebugSessionError("Not attached. Call attach_process first.")` if `_dbg is None`.

- **`_safe_get()`** (`session.py:437-445`): staticmethod helper that tries `getattr(obj, method_name)()` and returns `default` on any exception or non-callable.

### `src/debugbridge/server.py`

Forty-six lines of FastMCP wiring.

- **`build_app()`** (`server.py:17-23`): constructs a `FastMCP("debugbridge")` instance and a fresh `DebugSession()`, then calls `tools.register(mcp, session)`. Returns `(mcp, session)`. Separate from `run()` so tests can exercise the server without a socket.

- **`run(transport, host, port)`** (`server.py:26-46`): builds the app, then:
  - For HTTP: sets `mcp.settings.host` and `mcp.settings.port`, calls `mcp.run(transport="streamable-http")`. Comment at `server.py:39-41` notes FastMCP 1.27 uses `"streamable-http"` internally but the CLI exposes the alias `"http"`.
  - For stdio: `mcp.run(transport="stdio")`.

Endpoint: `http://{host}:{port}/mcp` — the MCP Streamable HTTP convention. Default bind `127.0.0.1:8585`.

### `src/debugbridge/tools.py`

Eight tool adapters. All registered on a single `FastMCP` instance by `register(mcp, session)` at `tools.py:27-110`. Each function is a thin wrapper over a `DebugSession` method — no tool is longer than ~25 lines.

**Tier A — crash triage:**

1. **`attach_process(pid=None, process_name=None, conn_str=None) -> AttachResult`** (`tools.py:32-59`):
   - Requires either `pid` or `process_name`.
   - `process_name` lookup is "not yet supported — pass pid" (`tools.py:50-55`).
   - If `conn_str` given → `session.attach_remote(conn_str, pid)`, else `session.attach_local(pid)`.

2. **`get_exception() -> ExceptionInfo | None`** (`tools.py:61-69`): docstring says "the first tool an AI client should call after `attach_process`." Returns `None` for a clean break with no crash.

3. **`get_callstack(max_frames=64) -> list[CallFrame]`** (`tools.py:71-78`).

4. **`get_threads() -> list[ThreadInfo]`** (`tools.py:80-83`).

5. **`get_locals(frame_index=0) -> list[Local]`** (`tools.py:85-93`). Docstring calls out the STL-container limitation.

**Tier B — active debugging:**

6. **`set_breakpoint(location: str) -> Breakpoint`** (`tools.py:97-100`). Accepts `"module!symbol"` or `"file.cpp:42"`.

7. **`step_next() -> StepResult`** (`tools.py:102-105`). Single source-line step over.

8. **`continue_execution() -> None`** (`tools.py:107-110`).

Return types are all annotated with Pydantic models from `models.py` — FastMCP uses those annotations to auto-generate the MCP tool schemas exposed via `list_tools`.

---

## 3. Request flow — MCP client call → response

When Claude Code (or any MCP client) calls a tool, here is the exact path:

1. **Transport layer (FastMCP / mcp 1.27.x):** Client opens Streamable HTTP connection to `http://127.0.0.1:8585/mcp`. Server is a Uvicorn ASGI app (see `e2e_smoke.py:59` — the readiness signal is the `"Uvicorn running"` log line).
2. **MCP session init:** Client calls `session.initialize()` (handshake); server responds with capabilities.
3. **Tool dispatch:** Client calls `session.call_tool("get_callstack", {"max_frames": 10})`. FastMCP routes to the function registered at `tools.py:72-78`.
4. **Adapter:** The registered function calls `session.get_callstack(max_frames=10)` where `session` is the singleton `DebugSession` created in `build_app()`.
5. **Session method:** Acquires `self._lock` (COM is single-threaded), asserts attached, runs `dbg.cmd(".lines -e")` and `dbg.cmd("kn f")`, parses the text output with `_FRAME_LINE_RE`, returns `list[CallFrame]`.
6. **Serialization:** FastMCP sees the return type annotation `list[CallFrame]`, serializes each Pydantic model to JSON, delivers as the MCP tool-call result.
7. **Client side:** The result is available as `result.structuredContent` (the parsed JSON) and `result.content` (stringified). See `e2e_smoke.py:124-137` for actual usage — the smoke test reads `attach.structuredContent.get("status")` to verify.

Key properties:
- **Single session per server:** There is exactly one `DebugSession` instance for the lifetime of the server process. Re-calling `attach_process` implicitly detaches the prior target (`session.py:134` calls `_close_locked()` first).
- **Serialization:** All methods on `DebugSession` take `self._lock` — concurrent MCP requests are serialized. COM's single-threaded requirement makes this non-negotiable.
- **Error handling:** Session methods can raise `DebugSessionError` (only `_require_attached()` raises it). Attach methods catch all exceptions and return `AttachResult(status="failed", message=str(e))`. Other methods propagate — FastMCP converts exceptions to MCP error responses.

---

## 4. The crash_app test fixture

**Binary path:** `D:\Projects\BridgeIt\tests\fixtures\crash_app\build\Debug\crash_app.exe` (verified to exist, alongside `crash_app.pdb`).

**Source:** `tests/fixtures/crash_app/crash.cpp` (76 lines). C++17, includes `<cstdio>`, `<stdexcept>`, `<process.h>` (for `_getpid`). Compiled with `/Zi /Od /MTd /EHsc` (full PDB, no optimizations, debug CRT statically linked so no DLL deps) and linked with `/DEBUG /INCREMENTAL:NO`. Each crash function is `NOINLINE` so it shows up by name in the stack.

**Modes (`crash.cpp:25-51`):**

| Arg | Function | Effect | Exception |
|-----|----------|--------|-----------|
| `null` | `crash_null(int)` at L25-34 | `*(int*)nullptr = 99` | `EXCEPTION_ACCESS_VIOLATION` (`0xC0000005`) |
| `stack` | `crash_stack_overflow(int)` at L36-40 | Infinite recursion w/ 64-int stack padding | `EXCEPTION_STACK_OVERFLOW` (`0xC00000FD`) |
| `throw` | `crash_throw()` at L42-44 | `throw std::runtime_error("boom")` uncaught | C++ exception `0xE06D7363` |
| `wait` | `wait_for_stdin()` at L46-51 | `printf` pid, flush, block on `fgets` | None (for breakpoint / attach-before-crash tests) |

`crash_null` (the primary test target) declares `int local_counter = 42`, `const char* local_tag = "about-to-deref-null"`, and `int* bad_pointer = nullptr` before the deref — these are the locals the debugger sees, and they're simple enough that `dv /t /v` returns them cleanly (no STL). This is deliberate.

**`main()` at `crash.cpp:53-76`** prints `"crash_app pid=%d\n"` on startup, then dispatches on `argv[1]`. Missing or unknown mode prints usage and returns 2.

**Build (`build.ps1`, 20 lines):**
- Resolves to its own directory, creates `build/` if missing.
- `cmake -S . -B build -G "Visual Studio 17 2022" -A x64`
- `cmake --build build --config Debug`
- Output: `build/Debug/crash_app.exe` + `build/Debug/crash_app.pdb`.

**Consumers of the binary:**
- `tests/conftest.py:25` — defines `_CRASH_APP` constant.
- `tests/conftest.py:31-32` — auto-skip marker logic uses `_CRASH_APP.exists()`.
- `tests/conftest.py:49-75` — `crash_app_waiting` fixture spawns with `argv=["wait"]`.
- `scripts/e2e_smoke.py:29` — `CRASH_APP` constant for the smoke test.

---

## 5. Integration testing approach

**Auto-skip logic lives in `tests/conftest.py:28-45`:**

```python
def pytest_collection_modifyitems(config, items):
    skip_reason = None
    if not _CRASH_APP.exists():
        skip_reason = f"crash_app not built at {_CRASH_APP}. Run tests/fixtures/crash_app/build.ps1."
    elif not check_debugging_tools().ok:
        skip_reason = "Windows Debugging Tools not installed (run `debugbridge doctor`)."
    if skip_reason is None:
        ensure_dbgeng_on_path()  # prep for pybag import
        return
    skip = pytest.mark.skip(reason=skip_reason)
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
```

Two prerequisites checked:
1. The built `crash_app.exe` exists.
2. `check_debugging_tools().ok` — all five components (`dbgsrv.exe`, `cdb.exe`, `dbgeng.dll`, `symsrv.dll`, `dbghelp.dll`) resolvable.

When both pass, `ensure_dbgeng_on_path()` is called **before** any test imports pybag. Tests marked `@pytest.mark.integration` receive a `pytest.mark.skip` otherwise. The CI workflow runs `pytest -m "not integration"` (see `.github/workflows/ci.yml:33`) to skip them explicitly.

**Pybag access pattern** (`tests/test_session_integration.py:83-91`): the one test that launches a process under the debugger (`test_catch_null_deref_crash_via_create`) imports pybag inside the test body after calling `ensure_dbgeng_on_path()`:
```python
from debugbridge.env import ensure_dbgeng_on_path
ensure_dbgeng_on_path()
from pybag.userdbg import UserDbg
dbg = UserDbg()
dbg.create(f'"{crash_app_path}" null', initial_break=True)
```
It then hands the raw `UserDbg` to the session by assignment: `session._dbg = dbg  # type: ignore[attr-defined]` (comment: "hand-off for test only"). This is the only test in the codebase that pokes at private state; everything else uses the public `attach_local` / `attach_remote` path.

**Attach pattern:**
- `initial_break=True` is always used (`session.py:140`, `session.py:159`, `test_session_integration.py:91`). For a running process this injects a synthetic break so `wait()` returns promptly and the session is in a stoppable state; for a crashed process the actual crash event supersedes.
- After `attach`, no explicit `go()` is needed to inspect state — pybag's `attach` with `initial_break=True` leaves the target paused.

**Integration tests (`test_session_integration.py`):**
- `test_attach_local_to_waiting_process` (L26-39) — attach a spawned `wait`-mode process, assert `status=="attached"`.
- `test_get_threads_returns_at_least_main` (L42-53) — attach, `get_threads()`, assert ≥ 1 thread, assert exactly one `is_current`, assert all `tid > 0`.
- `test_get_callstack_returns_frames` (L56-69) — attach, `get_callstack(max_frames=32)`, assert ≥ 1 frame with a valid IP or function name.
- `test_catch_null_deref_crash_via_create` (L72-116) — the key end-to-end test: launches `crash_app null` under the debugger via `UserDbg.create(...initial_break=True)`, calls `dbg.go()` (which blocks until the null deref fires), then `get_exception()` → asserts `code==0xC0000005`, `code_name=="EXCEPTION_ACCESS_VIOLATION"`, then `get_callstack()` → asserts `"crash_null"` appears in the top 5 frames. Cleanup does `dbg.terminate()` + `dbg.Release()` with `contextlib.suppress`.

**Unit test counterparts:**
- `test_parsers.py` exercises `_FRAME_LINE_RE` and `_parse_locals` against realistic WinDbg output samples with no pybag dependency. These are the regression guard against WinDbg format drift.

---

## 6. `scripts/e2e_smoke.py` — the Phase 2a blueprint

This is 178 lines and is the template Phase 2a's `debugbridge fix` will likely follow. Key pieces:

**Constants (`e2e_smoke.py:28-30`):**
```python
ROOT = Path(__file__).resolve().parent.parent
CRASH_APP = ROOT / "tests" / "fixtures" / "crash_app" / "build" / "Debug" / "crash_app.exe"
MCP_URL = "http://127.0.0.1:8585/mcp"
```

**Server lifecycle — `spawn_server()` async context manager (L37-73):**
- Launches `subprocess.Popen(["uv", "run", "debugbridge", "serve", "--port", "8585"], cwd=ROOT, stdout=PIPE, stderr=STDOUT, text=True, bufsize=1, creationflags=CREATE_NEW_PROCESS_GROUP)`.
- **Readiness probe:** reads `proc.stdout` line-by-line, waits up to 30 seconds for the string `"Uvicorn running"` to appear (NOT the `"DebugBridge serving on..."` line that `cli.py:63-64` prints — that comes BEFORE uvicorn is actually listening).
- **Shutdown:** on Windows sends `CTRL_BREAK_EVENT` (hence the `CREATE_NEW_PROCESS_GROUP` flag); on POSIX, `terminate()`. Gives 5 seconds for clean exit, then `.kill()`.

**Crash app spawn — `spawn_crash_app_waiting()` (L76-92):**
- `Popen([CRASH_APP, "wait"], stdin=PIPE, stdout=PIPE, stderr=PIPE, bufsize=0)`.
- Reads the first stdout line, asserts `"crash_app pid="` is in it.
- `time.sleep(0.3)` to let the process reach the `fgets()` call.
- Returns the `Popen` object. Caller uses `.pid` to attach.

**MCP client flow — `run_client_flow(pid)` (L95-146):**
```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client(MCP_URL) as (read, write, _session_id_cb):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        # tools.tools[].name → set of tool names
        attach = await session.call_tool("attach_process", {"pid": pid})
        # attach.content, attach.structuredContent
        status = (attach.structuredContent or {}).get("status")
        threads = await session.call_tool("get_threads", {})
        stack = await session.call_tool("get_callstack", {"max_frames": 10})
```

**Expected tool set** (asserted at `e2e_smoke.py:107-116`): exactly
```
{attach_process, continue_execution, get_callstack, get_exception,
 get_locals, get_threads, set_breakpoint, step_next}
```

**Shutdown order (`e2e_smoke.py:149-174`):** stop the server first (which detaches pybag, releasing `crash_app`), *then* `crash.kill()`. Comment at L162 explains: "DbgEng holds the attached process; detach via another MCP call before killing so Windows will honor SIGKILL. In practice we just stop the server (which detaches implicitly) and then kill."

---

## 7. External surfaces Phase 2a will plug into

### How the fix agent connects as an MCP client

The fix agent is a Python process that acts as an MCP client against `debugbridge serve`. It uses the exact same API `e2e_smoke.py` uses:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("http://127.0.0.1:8585/mcp") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("attach_process", {"pid": pid})
        # ... etc
```

These imports come from the `mcp[cli]>=1.27,<1.28` dependency already declared in `pyproject.toml:22` — no new dep needed for Phase 2a's client side.

### What the 8 tools actually return (wire shape)

When called via MCP, `call_tool(...)` returns a `CallToolResult` with:
- `result.content: list[TextContent]` — stringified JSON, one blob.
- `result.structuredContent: dict | list | None` — the parsed structured result FastMCP extracted from the Pydantic model.

Shapes (derived from `models.py`):

| Tool | structuredContent shape |
|------|-------------------------|
| `attach_process(pid)` | `{"pid": int, "process_name": str\|null, "is_remote": bool, "status": "attached"\|"failed", "message": str\|null}` |
| `get_exception()` | `{"code": int, "code_name": str, "address": int, "description": str, "is_first_chance": bool, "faulting_thread_tid": int\|null}` OR `null` |
| `get_callstack(max_frames)` | `[{"index": int, "function": str\|null, "module": str\|null, "file": str\|null, "line": int\|null, "instruction_pointer": int}, ...]` |
| `get_threads()` | `[{"id": int, "tid": int, "state": str, "is_current": bool, "frame_count": int\|null}, ...]` |
| `get_locals(frame_index)` | `[{"name": str, "type": str, "value": str, "address": int\|null, "truncated": bool}, ...]` |
| `set_breakpoint(location)` | `{"id": int, "location": str, "enabled": bool, "hit_count": int, "address": int\|null}` |
| `step_next()` | `{"status": "stopped"\|"crashed"\|"exited", "current_frame": CallFrame\|null}` |
| `continue_execution()` | `null` (no return value) |

The real strings for e.g. `get_callstack` can be seen in `e2e_smoke.py:137` where it logs `stack.structuredContent` directly — at steady state that log shows a list of frame dicts.

### Server process management

`e2e_smoke.py` shows the pattern:
- **Start:** `subprocess.Popen(["uv", "run", "debugbridge", "serve", "--port", "8585"], creationflags=CREATE_NEW_PROCESS_GROUP)` + parse stdout for `"Uvicorn running"` (30s timeout).
- **Stop (Windows):** `proc.send_signal(signal.CTRL_BREAK_EVENT)` + `proc.wait(timeout=5)` + fallback `proc.kill()`.
- **Port:** hard-coded 8585 in both CLI default and smoke script.
- **Endpoint:** `http://127.0.0.1:8585/mcp`.

Phase 2a's `debugbridge fix` command will need to reproduce this — either by auto-starting the server if not already running (matching the `GOAL.md` directive "auto-started as a subprocess if not running") or by requiring the user to run it separately.

---

## 8. Gaps and limitations in Phase 1 (Phase 2a will need to work around)

### `get_locals` — STL rendering is weak

- **`session.py:329-344` docstring** acknowledges: "DbgEng's expression evaluator renders STL containers as raw memory layouts. Simple primitives, pointers, and POD structs come through reliably; `std::string` / `std::vector` / etc. will appear as opaque binary."
- **Impact on Phase 2a:** When the fix agent queries locals for a frame containing STL types, it will get values like `class std::basic_string<char,...> mystr = <raw bytes>`. The briefing generator should probably flag STL types and not try to present their values as literal strings; rely on the `type` field + variable name + the nearby `file`/`line` for the agent's mental model instead.
- **Workaround hints:** The `crash_app` fixture deliberately uses only primitives (`int local_counter = 42`, `const char* local_tag`, `int* bad_pointer`) so Phase 1 tests pass cleanly. Phase 2a should either mirror this in any new test fixtures, or accept degraded output for STL-heavy locals.

### Source file resolution via `.lines -e` + `kn f` parsing

- **How it works** (`session.py:200-247`): `get_callstack` enables global source-line annotation with `.lines -e`, runs `kn f`, and parses `"[c:\src\crash.cpp @ 42]"` out of each line.
- **Reliability:**
  - **Requires PDBs.** If the running binary has no PDB (release build, stripped module), `kn f` will emit frames with no `[file @ line]` suffix — the regex `_FRAME_LINE_RE` makes `file` and `line` optional (`session.py:81`: `(?:\s+\[(?P<file>.+?)\s+@\s+(?P<line>\d+)\])?`), so frames parse but `file`/`line` come back `None`.
  - **Requires the symbol server or matching PDB path.** WinDbg's default behavior is to look in the PDB location baked into the binary, then the symbol path (`_NT_SYMBOL_PATH`). No symbol-server setup in Phase 1 code.
  - **Kernel / system frames** (`KERNEL32!BaseThreadInitThunk+0x14`) typically have no source info. This is normal and expected.
  - **Format drift risk:** if WinDbg changes the `[file @ line]` format, `_FRAME_LINE_RE` breaks silently (frames still parse, but file/line go missing). Unit tests at `test_parsers.py:22-44` guard against this.
- **Fallback path:** When `_parse_callstack` returns zero frames (e.g. `.lines -e` errored, or `kn f` output was completely swallowed), `_fallback_backtrace` is used — it reads `dbg.backtrace_list()` and resolves names via `dbg.get_name_by_offset()`, but `file` and `line` are always `None` in the fallback. Comment at `session.py:210-214`.
- **Phase 2a implication:** A briefing generator should treat `file`/`line` as best-effort. Not every frame will resolve. The agent should still be able to proceed from just `module!function+disp` + the faulting address.

### Process termination mid-query

- **Current behavior:** `DebugSession` methods assume `_dbg` is a valid `UserDbg` holding a live target. If the attached process terminates between calls, `dbg.cmd(...)` will throw. None of the read-only methods (`get_callstack`, `get_threads`, `get_locals`, `get_exception`) have termination-detection logic — the thrown exception propagates.
- **Attach failures handled:** `attach_local` / `attach_remote` catch all exceptions and return `AttachResult(status="failed", message=str(e))` (`session.py:149-151`). But once attached, teardown races are not specifically handled.
- **`close()` is defensive:** both `detach()` and `Release()` are wrapped in `contextlib.suppress(Exception)` at `session.py:179-183` — cleanup on a dead process won't crash the server.
- **`step_over` has some awareness:** it maps `"no_debuggee"` in `exec_status()` to `status="exited"` (`session.py:410-417`). But the read-only methods don't do this.
- **Phase 2a implication:** the fix agent should expect that long-running sessions may have a process disappear; it should handle an MCP tool-call error by re-attaching or bailing out, not by retrying blindly.

### Pydantic contract gaps Phase 2a may need to extend

Looking at the models against the GOAL.md expectations:

- **No `Briefing` bundle.** GOAL.md calls for a "human-readable briefing Markdown file" containing exception + stack + locals + threads. That composition happens on the client side; models.py has no `Briefing` type. Phase 2a can compose these from existing MCP-returned payloads OR add a new model.
- **No `faulting_frame` shortcut.** `ExceptionInfo` has `address` (the faulting IP) and `faulting_thread_tid`, but no pre-resolved `{module, function, file, line}` for the fault. The client has to call `get_callstack` separately and find frame 0 (or walk frames matching `address`). Straightforward but worth noting.
- **`CallFrame.instruction_pointer` is misnamed.** Per the comment at `session.py:227-229`, this field actually holds the RETURN ADDRESS (RetAddr column from `kn f`), not the true IP. The docstring on `CallFrame.instruction_pointer` (`models.py:33`) says "RIP/EIP for the frame", which is misleading. The frame-0 IP is more accurately read from `ExceptionInfo.address`. Phase 2a should not rely on `CallFrame.instruction_pointer == fault address`.
- **No `locals_error` or partial-success signal.** If `get_locals` can't enumerate (dv prints an error), it currently returns `[]`. The caller can't distinguish "no locals in scope" from "debugger failed to enumerate."
- **No dbg-session metadata tool.** There's no `get_status()` / `ping()` tool that tells a client "am I attached, to what pid, is it still alive?" The agent has to infer state from tool results.
- **Remote attach untested.** `attach_remote` (`session.py:153-170`) and the `conn_str` parameter on `attach_process` (`tools.py:57-58`) exist but are not exercised in CI (confirmed in `PROJECT.md:66`). Phase 2a's `--conn-str` flag is explicitly listed as a CLI option — this will be the first real exercise of that path.

### Other operational gaps

- **No HTTP health endpoint.** Readiness is detected by tailing stdout for `"Uvicorn running"` (`e2e_smoke.py:59`). Phase 2a's auto-start logic will need the same pattern or have to rely on TCP-connect-poll.
- **No per-request timeouts.** Any MCP call that ends up inside `dbg.cmd(...)` can block indefinitely (e.g. if symbol resolution stalls against a slow symbol server). The session lock means a stuck call blocks all other requests.
- **Single-session model.** One `DebugSession` per server process. If Phase 2a ever needs to attach to two processes concurrently, the server needs rework. (Not in 2a scope — GOAL.md `--pid` is single-valued.)
- **Logging is print-only.** The CLI writes via `rich.Console`; `DebugSession` emits nothing. No structured logging. Debugging a failed tool call from the client side means looking at the exception message and nothing else.
- **No PyPI release.** Wheel at `dist/debugbridge-0.1.0-py3-none-any.whl` builds cleanly, but Phase 2c handles the actual publish.

---

## 9. Dependencies — exact versions in play

From `pyproject.toml:21-27`:

| Dep | Range | Used for |
|-----|-------|----------|
| `mcp[cli]` | `>=1.27,<1.28` | FastMCP server, `ClientSession`, `streamablehttp_client` |
| `Pybag` | `>=2.2.16` | `UserDbg`, the DbgEng COM wrapper |
| `pydantic` | `>=2` | All 7 wire-contract models |
| `typer` | `>=0.12` | CLI framework |
| `rich` | `>=13` | Terminal rendering (tables, colors) |

Dev (`pyproject.toml:30-35`):
- `pytest>=8`, `pytest-asyncio>=0.24` (async integration tests if needed), `ruff>=0.7`, `pyright>=1.1`.

Build system (`pyproject.toml:40-42`): `uv_build>=0.9.26,<0.10.0`. Python requires `>=3.11` (`pyproject.toml:10`).

Pyright config: `typeCheckingMode = "basic"`, `reportMissingTypeStubs = false` (pybag has no stubs — see `pyproject.toml:59-63`).

---

## 10. What Phase 2a inherits that it can rely on

**Stable contracts:**
- The 8 MCP tool names and signatures are fixed (GOAL.md: "No change to the existing 8 MCP tools' public signatures").
- The Pydantic model field names / types are the wire format. Additions are safe; removals are not.
- `DebugSession` is the single pybag consumer. Phase 2a's agent goes through MCP, not through direct imports (GOAL.md: "never calls `DebugSession` directly").
- Lazy pybag import pattern is load-bearing — `debugbridge doctor` / `version` must continue to work on a machine without Debugging Tools.
- HTTP endpoint is `http://127.0.0.1:8585/mcp`, port configurable via `--port`.

**Reusable plumbing:**
- `env.check_debugging_tools()` + `env.ensure_dbgeng_on_path()` — Phase 2a can call these before spawning the server.
- `tests/conftest.py:28-45` auto-skip pattern — Phase 2a integration tests can reuse the same gate.
- `crash_app_waiting` fixture — Phase 2a tests can use it as-is to get a live PID for `fix --pid N`.
- `scripts/e2e_smoke.py` — the full server-spawn + MCP-client flow is a drop-in template for how `debugbridge fix` should orchestrate its own server subprocess.

**Known-good end-to-end proof:** `uv run python scripts/e2e_smoke.py` is the repeatable acceptance evidence for Phase 1 — Phase 2a should add its own analogous script (or CLI command) that runs `debugbridge fix --pid <wait_pid> --auto ...` end-to-end against the same `crash_app null` crash and asserts a non-empty `.patch` at `.debugbridge/patches/`.

---

*Mapping produced 2026-04-15 against commit `d514fb4` on `main`.*
