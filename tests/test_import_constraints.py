"""Enforce architecture constraint: fix/ must not import from debugbridge.session."""

import re
from pathlib import Path


def test_fix_does_not_import_debugsession():
    """No Python file under src/debugbridge/fix/ may import from debugbridge.session."""
    fix_dir = Path(__file__).resolve().parent.parent / "src" / "debugbridge" / "fix"
    pattern = re.compile(r"from\s+debugbridge\.session\s+import")

    violations: list[str] = []
    for py_file in fix_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), 1):
            if pattern.search(line):
                violations.append(
                    f"{py_file.relative_to(fix_dir.parent.parent.parent)}:{lineno}: {line.strip()}"
                )

    assert not violations, (
        "src/debugbridge/fix/ must not import from debugbridge.session -- "
        "the fix agent talks to the server via MCP only.\n" + "\n".join(violations)
    )
