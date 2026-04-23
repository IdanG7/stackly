"""DebugSession — the only module in Stackly allowed to touch pybag.

All MCP tools funnel through a single ``DebugSession`` instance. The session
serializes DbgEng access with a lock (COM is single-threaded), converts pybag's
raw structures to Pydantic models, and keeps pybag's imports deferred so the
rest of Stackly can load without Windows Debugging Tools installed.

Why pybag imports are lazy: ``pybag/__init__.py`` tries to load ``dbgeng.dll``
at import time and raises ``FileNotFoundError`` when the DLL is missing. If a
user runs ``stackly doctor`` without Debugging Tools installed, we want a
friendly diagnostic — not a stack trace from deep inside ``ctypes``.

Why we use WinDbg commands (``k``, ``.lastevent``, ``.exr -1``) for some data:
pybag's wrappers for ``GetLineByOffset`` and ``GetLastEventInformation`` raise
``E_NOTIMPL_Error``. Rather than reaching through to raw comtypes and wrestling
with out-parameters, we use ``dbg.cmd()`` which already does the plumbing and
returns stable, parsable text output.
"""

from __future__ import annotations

import contextlib
import re
import threading
from typing import TYPE_CHECKING, Any

from stackly.models import (
    AttachResult,
    Breakpoint,
    CallFrame,
    ExceptionInfo,
    Local,
    StepResult,
    ThreadInfo,
)

if TYPE_CHECKING:  # pybag is only imported at method-call time
    from pybag.userdbg import UserDbg


class DebugSessionError(Exception):
    """Raised when a session method can't complete (wrapped and returned to MCP)."""


# Well-known NTSTATUS exception codes → WinDbg-style names. Kept small; we only
# decode the codes users actually see in crashes. Unknown codes stringify as hex.
_EXCEPTION_CODE_NAMES: dict[int, str] = {
    0xC0000005: "EXCEPTION_ACCESS_VIOLATION",
    0xC00000FD: "EXCEPTION_STACK_OVERFLOW",
    0x80000003: "EXCEPTION_BREAKPOINT",
    0x80000004: "EXCEPTION_SINGLE_STEP",
    0xC0000094: "EXCEPTION_INT_DIVIDE_BY_ZERO",
    0xC0000095: "EXCEPTION_INT_OVERFLOW",
    0xC000001D: "EXCEPTION_ILLEGAL_INSTRUCTION",
    0xC0000025: "EXCEPTION_NONCONTINUABLE_EXCEPTION",
    0xC0000026: "EXCEPTION_INVALID_DISPOSITION",
    0xC0000008: "EXCEPTION_INVALID_HANDLE",
    0xE06D7363: "CPP_EXCEPTION",  # Microsoft C++ exception magic
}


def _decode_exception_code(code: int) -> str:
    code &= 0xFFFFFFFF
    return _EXCEPTION_CODE_NAMES.get(code, f"0x{code:08X}")


# Output of the "k" command with flags "n f l" (numbers, frame addresses, line info):
#
#    # ChildEBP RetAddr
#   00 0012fe54 75e87c04 myapp!crash_null+0x2a [c:\src\crash.cpp @ 42]
#
# We parse only the lines that begin with a frame index; WinDbg prepends a
# header row and sometimes inserts "WARNING" rows between frames.
_FRAME_LINE_RE = re.compile(
    r"""^\s*
        (?P<idx>[0-9a-fA-F]+)\s+
        (?P<child>[0-9a-fA-F`]+)\s+
        (?P<ret>[0-9a-fA-F`]+)\s+
        (?P<sym>\S+)
        (?:\s+\[(?P<file>.+?)\s+@\s+(?P<line>\d+)\])?
    """,
    re.VERBOSE,
)

# Output of ".lastevent":
#
#   Last event: 1234.5678: Access violation - code c0000005 (first chance)
#     debugger time: Thu Apr 15 23:35:45.123 2026
_LASTEVENT_RE = re.compile(
    r"Last event:\s+(?P<pid>[0-9a-fA-F]+)\.(?P<tid>[0-9a-fA-F]+):\s+"
    r"(?P<desc>.+?)\s+-\s+code\s+(?P<code>[0-9a-fA-F]+)\s+"
    r"\((?P<chance>first|second) chance\)",
    re.IGNORECASE,
)

# Output of ".exr -1" (exception record at current event):
#
#   ExceptionAddress: 00007ff612341234 (myapp!crash_null+0x2a)
#      ExceptionCode: c0000005 (Access violation)
#     ExceptionFlags: 00000000
#   NumberParameters: 2
_EXR_ADDR_RE = re.compile(r"ExceptionAddress:\s+([0-9a-fA-F`]+)")


class DebugSession:
    """Serializes all DbgEng access behind a single lock.

    One session per Stackly server process. Re-attaching replaces the
    underlying pybag instance. ``close()`` detaches cleanly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dbg: UserDbg | None = None
        self._is_remote = False
        self._process_name: str | None = None

    # ---- Lifecycle ----

    def _make_userdbg(self) -> UserDbg:
        """Lazy import + construct a new UserDbg. Safe to call only from inside the lock."""
        # Deferred so `import stackly.session` works without Debugging Tools.
        from stackly.env import ensure_dbgeng_on_path

        ensure_dbgeng_on_path()

        from pybag.userdbg import UserDbg

        return UserDbg()

    def attach_local(self, pid: int) -> AttachResult:
        with self._lock:
            try:
                self._close_locked()
                self._dbg = self._make_userdbg()
                # initial_break=True: pause the target so we can inspect it.
                # For a running process this injects a break and wait() returns
                # promptly. For an already-crashed process the crash event
                # takes priority over the initial break anyway.
                self._dbg.attach(pid, initial_break=True)
                self._is_remote = False
                self._process_name = self._lookup_process_name(pid)
                return AttachResult(
                    pid=pid,
                    process_name=self._process_name,
                    is_remote=False,
                    status="attached",
                )
            except Exception as e:
                self._dbg = None
                return AttachResult(pid=pid, status="failed", message=str(e))

    def attach_remote(self, conn_str: str, pid: int) -> AttachResult:
        with self._lock:
            try:
                self._close_locked()
                self._dbg = self._make_userdbg()
                self._dbg.connect(conn_str)
                self._dbg.attach(pid, initial_break=True)
                self._is_remote = True
                self._process_name = self._lookup_process_name(pid)
                return AttachResult(
                    pid=pid,
                    process_name=self._process_name,
                    is_remote=True,
                    status="attached",
                )
            except Exception as e:
                self._dbg = None
                return AttachResult(pid=pid, is_remote=True, status="failed", message=str(e))

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def detach(self) -> None:
        """Public MCP-exposed variant of ``close()``.

        Same body as ``close()`` — both take the lock and call
        ``_close_locked()``. Two methods with identical bodies documents intent
        at the call site: ``close()`` is used from shutdown / destructors;
        ``detach()`` is what the MCP ``detach_process`` tool binds to, so a
        client can release pybag from the target without stopping the server.
        """
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._dbg is None:
            return
        with contextlib.suppress(Exception):
            self._dbg.detach()
        with contextlib.suppress(Exception):
            self._dbg.Release()
        self._dbg = None
        self._process_name = None

    def _lookup_process_name(self, pid: int) -> str | None:
        """Best-effort: search proc_list() for the pid's name."""
        if self._dbg is None:
            return None
        try:
            for p, name, _desc in self._dbg.proc_list():
                if p == pid:
                    return name
        except Exception:
            return None
        return None

    # ---- Read-only queries ----

    def get_callstack(self, max_frames: int = 64) -> list[CallFrame]:
        with self._lock:
            dbg = self._require_attached()
            # ".lines -e" globally enables source-line annotation on stack
            # output. "kn f" = frame numbers + frame addresses. Source lines
            # (when available) are appended as "[file @ line]" on each frame.
            dbg.cmd(".lines -e", quiet=True)
            output = dbg.cmd("kn f", quiet=True)
            frames = self._parse_callstack(output, max_frames=max_frames)

            # If the parser found zero frames (rare: the command was swallowed
            # or output format changed), fall back to backtrace_list() — it
            # always returns at least the top frame but has no file/line.
            if not frames:
                frames = self._fallback_backtrace(dbg, max_frames=max_frames)
            return frames

    def _parse_callstack(self, output: str, max_frames: int) -> list[CallFrame]:
        frames: list[CallFrame] = []
        for line in output.splitlines():
            m = _FRAME_LINE_RE.match(line)
            if not m:
                continue
            sym_full = m.group("sym")  # e.g. "myapp!crash_null+0x2a"
            module, function = self._split_sym(sym_full)
            file_name = m.group("file")
            line_num = int(m.group("line")) if m.group("line") else None
            # ChildEBP isn't the instruction pointer, but it's the only address
            # reliably in "k" output columns. For a truer IP we'd need stepping
            # per-frame — out of scope for MVP.
            ip_str = m.group("ret").replace("`", "")
            try:
                ip = int(ip_str, 16)
            except ValueError:
                ip = 0
            frames.append(
                CallFrame(
                    index=int(m.group("idx"), 16),
                    function=function,
                    module=module,
                    file=file_name,
                    line=line_num,
                    instruction_pointer=ip,
                )
            )
            if len(frames) >= max_frames:
                break
        return frames

    @staticmethod
    def _split_sym(sym: str) -> tuple[str | None, str | None]:
        """Split "module!symbol+0x2a" into (module, symbol-with-displacement)."""
        if "!" not in sym:
            return None, sym or None
        module, _, rest = sym.partition("!")
        return module or None, rest or None

    def _fallback_backtrace(self, dbg: UserDbg, max_frames: int) -> list[CallFrame]:
        frames: list[CallFrame] = []
        for i, f in enumerate(dbg.backtrace_list()):
            if i >= max_frames:
                break
            name = dbg.get_name_by_offset(f.InstructionOffset)
            module, function = self._split_sym(name)
            frames.append(
                CallFrame(
                    index=f.FrameNumber,
                    function=function,
                    module=module,
                    file=None,
                    line=None,
                    instruction_pointer=f.InstructionOffset,
                )
            )
        return frames

    def get_exception(self) -> ExceptionInfo | None:
        with self._lock:
            dbg = self._require_attached()
            last_event = dbg.cmd(".lastevent", quiet=True)
            m = _LASTEVENT_RE.search(last_event)
            if not m:
                return None
            code = int(m.group("code"), 16)
            desc = m.group("desc").strip()
            tid = int(m.group("tid"), 16)

            # Get the faulting address via the exception record. This is only
            # meaningful if the last event was actually an exception — for a
            # manual break the ExceptionRecord may be stale.
            exr = dbg.cmd(".exr -1", quiet=True)
            addr_match = _EXR_ADDR_RE.search(exr)
            address = 0
            if addr_match:
                try:
                    address = int(addr_match.group(1).replace("`", ""), 16)
                except ValueError:
                    address = 0

            return ExceptionInfo(
                code=code,
                code_name=_decode_exception_code(code),
                address=address,
                description=desc,
                is_first_chance=m.group("chance").lower() == "first",
                faulting_thread_tid=tid,
            )

    def get_threads(self) -> list[ThreadInfo]:
        with self._lock:
            dbg = self._require_attached()
            threads: list[ThreadInfo] = []
            current_idx = dbg.get_thread()
            # thread_list yields (sysid, teb_addr, symbol_at_pc). The DbgEng
            # thread INDEX isn't in the tuple — we get it from ordering, since
            # GetThreadIdsByIndex returns (indices, sysids) in the same order.
            ids, sysids = dbg._systems.GetThreadIdsByIndex()  # type: ignore[attr-defined]
            for idx, sysid in zip(ids, sysids, strict=False):
                threads.append(
                    ThreadInfo(
                        id=idx,
                        tid=sysid,
                        state=dbg.exec_status().lower() if idx == current_idx else "unknown",
                        is_current=(idx == current_idx),
                        frame_count=None,  # cheap to compute but needs per-thread switch
                    )
                )
            return threads

    def get_locals(self, frame_index: int = 0) -> list[Local]:
        """Best-effort local enumeration via WinDbg's ``dv`` command.

        Known limitation: DbgEng's expression evaluator renders STL containers
        as raw memory layouts. Simple primitives, pointers, and POD structs
        come through reliably; ``std::string`` / ``std::vector`` / etc. will
        appear as opaque binary. Noted in README and tracked for Phase 2.
        """
        with self._lock:
            dbg = self._require_attached()
            # Switch to requested frame, capture dv output, restore frame.
            # ".frame <n>" sets the scope; "dv /t /v" dumps variables w/ types.
            dbg.cmd(f".frame {frame_index}", quiet=True)
            output = dbg.cmd("dv /t /v", quiet=True)
            dbg.cmd(".frame 0", quiet=True)
            return self._parse_locals(output)

    @staticmethod
    def _parse_locals(output: str) -> list[Local]:
        """Parse lines like:  ``00007ffe`5c2a1234   class std::string mystr = ...``"""
        locals_: list[Local] = []
        for raw in output.splitlines():
            line = raw.rstrip()
            if not line or "=" not in line:
                continue
            # Address prefix is 8-16 hex chars (with optional backtick)
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            addr_str, rest = parts
            try:
                addr: int | None = int(addr_str.replace("`", ""), 16)
            except ValueError:
                addr = None
            # "<type...> name = <value>"  — split name vs value by first "="
            left, _, value = rest.partition("=")
            left = left.strip()
            value = value.strip()
            # Last whitespace-separated token on the left side is the name.
            type_and_name = left.rsplit(None, 1)
            if len(type_and_name) == 2:
                type_str, name = type_and_name
            else:
                type_str, name = "unknown", left
            truncated = len(value) > 256
            locals_.append(
                Local(
                    name=name,
                    type=type_str,
                    value=(value[:256] + "…") if truncated else value,
                    address=addr,
                    truncated=truncated,
                )
            )
        return locals_

    # ---- Active debugging (Tier B) ----

    def set_breakpoint(self, location: str) -> Breakpoint:
        with self._lock:
            dbg = self._require_attached()
            bp = dbg.bp(location)
            # pybag's breakpoints.set returns the IDebugBreakpoint; attrs depend
            # on the comtypes wrapper. We pull what we can safely.
            bp_id = self._safe_get(bp, "GetId", default=-1)
            addr = self._safe_get(bp, "GetOffset", default=None)
            return Breakpoint(
                id=bp_id if isinstance(bp_id, int) else -1,
                location=location,
                enabled=True,
                hit_count=0,
                address=addr if isinstance(addr, int) else None,
            )

    def step_over(self) -> StepResult:
        with self._lock:
            dbg = self._require_attached()
            dbg.stepo(count=1)
            status = dbg.exec_status().lower()
            frames = self._fallback_backtrace(dbg, max_frames=1)
            result_status = (
                "exited"
                if "no_debuggee" in status
                else (
                    "crashed"
                    if "break" in status and dbg.cmd(".lastevent", quiet=True).find("chance") >= 0
                    else "stopped"
                )
            )
            return StepResult(
                status=result_status,  # type: ignore[arg-type]
                current_frame=frames[0] if frames else None,
            )

    def continue_execution(self) -> None:
        with self._lock:
            dbg = self._require_attached()
            # go() blocks until the next event; for "just resume" behavior we
            # set the status without waiting. pybag's cmd("g") does that.
            dbg.cmd("g", quiet=True)

    # ---- Internals ----

    def _require_attached(self) -> UserDbg:
        if self._dbg is None:
            raise DebugSessionError("Not attached. Call attach_process first.")
        return self._dbg

    @staticmethod
    def _safe_get(obj: Any, method_name: str, default: Any = None) -> Any:
        method = getattr(obj, method_name, None)
        if not callable(method):
            return default
        try:
            return method()
        except Exception:
            return default
