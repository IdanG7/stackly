"""Tests for WinDbg output parsers in session.py. No pybag needed.

These guard against format drift in ``k n f l``, ``.lastevent``, ``.exr -1``,
and ``dv /t /v`` output. If pybag ever changes its cmd() output encoding or
WinDbg ships a different format, these tests break noisily.
"""

from __future__ import annotations

from stackly.session import DebugSession, _decode_exception_code


def test_decode_exception_code_known() -> None:
    assert _decode_exception_code(0xC0000005) == "EXCEPTION_ACCESS_VIOLATION"
    assert _decode_exception_code(0xC00000FD) == "EXCEPTION_STACK_OVERFLOW"


def test_decode_exception_code_unknown_is_hex() -> None:
    assert _decode_exception_code(0xDEADBEEF) == "0xDEADBEEF"


def test_parse_callstack_x64_with_source_info() -> None:
    output = """\
 # Child-SP          RetAddr               Call Site
00 00000053`abc12340 00007ff6`12341234     myapp!crash_null+0x2a [c:\\src\\crash.cpp @ 42]
01 00000053`abc12380 00007ff6`12345678     myapp!main+0x18 [c:\\src\\crash.cpp @ 87]
02 00000053`abc12400 00007ffe`5c2a1234     KERNEL32!BaseThreadInitThunk+0x14
"""
    s = DebugSession()
    frames = s._parse_callstack(output, max_frames=10)
    assert len(frames) == 3

    assert frames[0].function == "crash_null+0x2a"
    assert frames[0].module == "myapp"
    assert frames[0].file and frames[0].file.endswith("crash.cpp")
    assert frames[0].line == 42

    assert frames[1].function == "main+0x18"
    assert frames[1].line == 87

    # Frame without source info still parses.
    assert frames[2].module == "KERNEL32"
    assert frames[2].file is None
    assert frames[2].line is None


def test_parse_callstack_respects_max_frames() -> None:
    # 5 frames in, 3 out.
    output = "\n".join(f"0{i} 00000000`00000000 00000000`00000000 myapp!f{i}+0x0" for i in range(5))
    s = DebugSession()
    frames = s._parse_callstack(output, max_frames=3)
    assert len(frames) == 3


def test_parse_callstack_ignores_garbage() -> None:
    output = """\
*** WARNING: Unable to verify checksum for myapp.exe
 # Child-SP          RetAddr               Call Site
00 00000053`abc12340 00007ff6`12341234     myapp!crash_null+0x2a
"""
    s = DebugSession()
    frames = s._parse_callstack(output, max_frames=10)
    assert len(frames) == 1
    assert frames[0].function == "crash_null+0x2a"


def test_split_sym() -> None:
    assert DebugSession._split_sym("myapp!crash_null+0x2a") == ("myapp", "crash_null+0x2a")
    assert DebugSession._split_sym("bare_symbol") == (None, "bare_symbol")
    assert DebugSession._split_sym("") == (None, None)


def test_parse_locals_simple() -> None:
    # Address column, type, name, value — tab/space separated.
    output = """\
00000053`abc12348  int i = 0n42
00000053`abc12350  char * name = 0x00007ff6`12345678 "hello"
00000053`abc12358  unknown_type_field_only
"""
    locals_ = DebugSession._parse_locals(output)
    assert len(locals_) == 2  # third line has no "=", skipped
    assert locals_[0].name == "i"
    assert locals_[0].type == "int"
    assert locals_[0].value == "0n42"
    assert locals_[1].name == "name"
    assert "hello" in locals_[1].value


def test_parse_locals_truncates_long_value() -> None:
    long_value = "x" * 500
    output = f"00000000`00000000  char[500] buf = {long_value}"
    locals_ = DebugSession._parse_locals(output)
    assert len(locals_) == 1
    assert locals_[0].truncated
    assert locals_[0].value.endswith("…")
    assert len(locals_[0].value) == 257  # 256 + ellipsis
