"""End-to-end smoke test for debugbridge fix --auto.

This script exercises the full pipeline against a real crash_app process.
Requires: Windows Debugging Tools, claude CLI authenticated, crash_app built.

Usage:
  uv run python scripts/e2e_fix_smoke.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRASH_APP = ROOT / "tests" / "fixtures" / "crash_app" / "build" / "Debug" / "crash_app.exe"


def log(msg: str) -> None:
    print(f"[e2e-fix] {msg}", flush=True)


def main() -> int:
    if not CRASH_APP.exists():
        log(f"crash_app not built at {CRASH_APP}")
        return 1

    # Launch crash_app in wait mode
    log("launching crash_app wait...")
    crash = subprocess.Popen(
        [str(CRASH_APP), "wait"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert crash.stdout is not None
    line = crash.stdout.readline().decode(errors="replace")
    log(f"crash_app: {line.rstrip()}")
    time.sleep(0.3)

    try:
        log(
            f"running: debugbridge fix --pid {crash.pid} --repo {ROOT} "
            f'--auto --build-cmd "python -c \'print(1)\'"'
        )
        result = subprocess.run(
            [
                "uv",
                "run",
                "debugbridge",
                "fix",
                "--pid",
                str(crash.pid),
                "--repo",
                str(ROOT),
                "--auto",
                "--build-cmd",
                "python -c \"print('build ok')\"",
                "--max-attempts",
                "1",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        log(f"exit code: {result.returncode}")
        log(f"stdout:\n{result.stdout}")
        if result.stderr:
            log(f"stderr:\n{result.stderr}")

        # Check for patch file
        patches = list((ROOT / ".debugbridge" / "patches").glob("crash-*.patch"))
        if patches:
            log(f"patch written: {patches[0]}")
            log(f"patch content:\n{patches[0].read_text(encoding='utf-8')[:500]}")
        else:
            log("no patch file found")
            failed = list((ROOT / ".debugbridge" / "patches").glob("crash-*.failed.md"))
            if failed:
                log(f"failure report: {failed[0]}")

        return result.returncode
    except subprocess.TimeoutExpired:
        log("TIMEOUT after 300s")
        return 1
    finally:
        if crash.poll() is None:
            crash.kill()
            crash.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
