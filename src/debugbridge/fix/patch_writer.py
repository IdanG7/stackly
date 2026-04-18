"""Write patch files and failure reports to .debugbridge/patches/ (task 2a.3.3)."""

from __future__ import annotations

from pathlib import Path

from debugbridge.fix.models import AttemptRecord


def _patches_dir(repo: Path) -> Path:
    d = repo / ".debugbridge" / "patches"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_patch(repo: Path, crash_hash: str, diff: str) -> Path:
    """Write a unified diff to ``.debugbridge/patches/crash-<hash>.patch``.

    Creates the patches directory (and all parents) if it does not exist.
    All writes use UTF-8 encoding.
    """
    out = _patches_dir(repo) / f"crash-{crash_hash}.patch"
    out.write_text(diff, encoding="utf-8")
    return out


def write_failure_report(
    repo: Path,
    crash_hash: str,
    attempts: list[AttemptRecord],
    final_msg: str,
) -> Path:
    """Write a Markdown failure report to ``.debugbridge/patches/crash-<hash>.failed.md``.

    Lists each attempt with its Claude response (truncated to 2K chars),
    build output (truncated to 2K chars), cost, and test results.
    """
    out = _patches_dir(repo) / f"crash-{crash_hash}.failed.md"
    lines: list[str] = [f"# Fix failure report — crash-{crash_hash}\n"]
    lines.append(f"\n**Final status:** {final_msg}\n")
    for a in attempts:
        lines.append(f"\n## Attempt {a.attempt}\n")
        lines.append(f"- Build: {'passed' if a.build_ok else 'FAILED'}\n")
        if a.test_ok is not None:
            lines.append(f"- Tests: {'passed' if a.test_ok else 'FAILED'}\n")
        lines.append(f"- Cost: ${a.claude_result.total_cost_usd:.4f}\n")
        lines.append(f"\n### Claude response\n\n{a.claude_result.result[:2000]}\n")
        lines.append(f"\n### Build output\n\n```\n{a.build_output[:2000]}\n```\n")
    out.write_text("".join(lines), encoding="utf-8")
    return out
