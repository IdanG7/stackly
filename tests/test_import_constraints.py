"""Enforce architecture constraint: fix/ must not import from stackly.session."""

import re
from pathlib import Path


def test_fix_does_not_import_debugsession():
    """No Python file under src/stackly/fix/ may import from stackly.session."""
    fix_dir = Path(__file__).resolve().parent.parent / "src" / "stackly" / "fix"
    pattern = re.compile(r"from\s+stackly\.session\s+import")

    violations: list[str] = []
    for py_file in fix_dir.rglob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), 1):
            if pattern.search(line):
                violations.append(
                    f"{py_file.relative_to(fix_dir.parent.parent.parent)}:{lineno}: {line.strip()}"
                )

    assert not violations, (
        "src/stackly/fix/ must not import from stackly.session -- "
        "the fix agent talks to the server via MCP only.\n" + "\n".join(violations)
    )
