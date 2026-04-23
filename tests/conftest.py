"""Pytest fixtures for integration tests.

Integration tests are marked ``@pytest.mark.integration`` and require:
  - Windows Debugging Tools installed (or discoverable via env.ensure_dbgeng_on_path)
  - tests/fixtures/crash_app/build/Debug/crash_app.exe built (run build.ps1)

The ``PYBAG_INTEGRATION=1`` env var is NOT required — if the binary exists and
Debugging Tools are present, integration tests run. They auto-skip otherwise so
CI stays green.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Prepend the Debugging Tools path BEFORE any test imports pybag.
# Tests that touch pybag (integration tests) assume this already happened.
from stackly.env import check_debugging_tools, ensure_dbgeng_on_path

_CRASH_APP = Path(__file__).parent / "fixtures" / "crash_app" / "build" / "Debug" / "crash_app.exe"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration tests when prerequisites are missing."""
    skip_reason: str | None = None
    if not _CRASH_APP.exists():
        skip_reason = (
            f"crash_app not built at {_CRASH_APP}. Run tests/fixtures/crash_app/build.ps1."
        )
    elif not check_debugging_tools().ok:
        skip_reason = "Windows Debugging Tools not installed (run `stackly doctor`)."

    if skip_reason is None:
        ensure_dbgeng_on_path()
        return

    skip = pytest.mark.skip(reason=skip_reason)
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def crash_app_waiting() -> Iterator[subprocess.Popen]:
    """Launch crash_app in ``wait`` mode so it blocks on stdin.

    Yields the Popen; pass .pid to attach. Tests must call ``proc.stdin.write(b'\\n')``
    or terminate the process when done — the fixture does a hard kill on teardown
    as a safety net.
    """
    proc = subprocess.Popen(
        [str(_CRASH_APP), "wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    # Wait for the first line of stdout so we know the program has printed its PID.
    # This avoids races where we attach before main() has even started.
    assert proc.stdout is not None
    line = proc.stdout.readline().decode(errors="replace")
    assert "crash_app pid=" in line, f"unexpected first line: {line!r}"
    # Give the process one extra moment to reach the fgets() call.
    time.sleep(0.2)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def crash_app_path() -> Path:
    """Absolute path to the built crash_app.exe."""
    return _CRASH_APP
