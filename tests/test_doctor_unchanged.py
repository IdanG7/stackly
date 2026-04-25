"""Regression test: doctor output must not grow beyond Phase 1 + Phase 2a checks.

CONTEXT.md states no new external deps are added in Phase 2.5.
This test encodes that invariant: the set of environment checks reported by
`stackly doctor` must remain exactly the four items shipped in Phases 1 and 2a.
If someone adds a new external-tool dependency and wires it into `doctor`, this
test will fail — prompting them to update the constraint in CONTEXT.md deliberately.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from stackly.cli import app


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_doctor_does_not_add_new_environment_checks() -> None:
    """doctor output contains exactly the Phase 1 + Phase 2a checks — no 2.5 additions."""
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])

    # doctor may exit 0 (all found) or 1 (something missing) — both are valid
    assert result.exit_code in (0, 1), (
        f"Unexpected exit code {result.exit_code}; output: {result.output}"
    )

    text = _strip_ansi(result.output + (result.stderr or ""))

    # Phase 1 + Phase 2a checks must all appear
    expected_checks = {"dbgeng", "cdb", "claude CLI", "claude bypass ack'd"}
    for check in expected_checks:
        assert check.lower() in text.lower(), (
            f"Missing expected doctor check: {check!r}\nFull output:\n{text}"
        )

    # No Phase 2.5 entries should have slipped in
    forbidden_new = {"anyio", "watch", "streamablehttp"}
    for forbidden in forbidden_new:
        assert forbidden not in text.lower(), (
            f"Unexpected new doctor check found: {forbidden!r}\nFull output:\n{text}"
        )
