"""Pydantic types returned by MCP tools.

These are the wire contract between MCP clients and Stackly. Tools in
``stackly.tools`` must not invent ad-hoc dicts — every return value goes
through one of these models so FastMCP can serialize it predictably.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AttachResult(BaseModel):
    """Outcome of an ``attach_process`` call."""

    pid: int
    process_name: str | None = None
    is_remote: bool = False
    status: Literal["attached", "failed"]
    message: str | None = None


class CallFrame(BaseModel):
    """One frame in a call stack."""

    index: int = Field(description="0 = innermost frame")
    function: str | None = None
    module: str | None = None
    file: str | None = None
    line: int | None = None
    instruction_pointer: int = Field(description="RIP/EIP for the frame")


class ThreadInfo(BaseModel):
    """One OS thread in the attached process."""

    id: int = Field(description="DbgEng's internal thread index")
    tid: int = Field(description="Windows thread ID")
    state: str = Field(description='"running" | "stopped" | "exited" | unknown')
    is_current: bool = False
    frame_count: int | None = None


class ExceptionInfo(BaseModel):
    """Current or last-raised exception on the attached process."""

    code: int = Field(description="NTSTATUS / exception code, e.g. 0xC0000005")
    code_name: str = Field(description="e.g. EXCEPTION_ACCESS_VIOLATION")
    address: int = Field(description="Faulting instruction address")
    description: str = ""
    is_first_chance: bool = True
    faulting_thread_tid: int | None = None


class Local(BaseModel):
    """A local variable / parameter in a stack frame."""

    name: str
    type: str
    value: str = Field(description="Stringified value; may be truncated for large structures")
    address: int | None = None
    truncated: bool = False


class Breakpoint(BaseModel):
    """A breakpoint set via ``set_breakpoint``."""

    id: int
    location: str = Field(description='Original spec: "module!symbol" or "file.cpp:42"')
    enabled: bool = True
    hit_count: int = 0
    address: int | None = None


class StepResult(BaseModel):
    """Outcome of a ``step_next`` call."""

    status: Literal["stopped", "crashed", "exited"]
    current_frame: CallFrame | None = None
