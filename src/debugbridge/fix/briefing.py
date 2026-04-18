"""Crash-briefing assembly (tasks 2a.1.3 + 2a.1.4).

``extract_source_snippets`` walks a call stack, resolves in-repo file paths,
merges overlapping plus/minus N-line ranges per file, and returns a dict of
``repo-relative Path -> snippet string`` for use by the briefing renderer.

``render_briefing`` turns a :class:`CrashCapture` plus extracted snippets into
a structured Markdown document that Claude Code reads as its crash briefing.

``write_briefing`` writes that Markdown to disk with UTF-8 encoding and
unix line endings.

Design notes (snippet extractor):
- Frames without ``file`` or ``line`` metadata are silently dropped.
- Paths outside the repo (stdlib, third-party, Windows system files) are
  silently dropped - the agent should only propose fixes to user code.
- Non-existent files are silently dropped (symbol pointed at a file the
  user doesn't have locally).
- Overlapping ranges inside one file are merged into a single block.
- Each block is prefixed with ``// lines LO-HI`` - renders as a harmless
  line comment in C/C++/C#/Java/Go/JS/TS/Rust and most other languages
  the fix agent might be asked to repair.
"""

from __future__ import annotations

from pathlib import Path

from debugbridge.fix.models import CallFrame, CrashCapture

__all__ = ["append_retry_feedback", "extract_source_snippets", "render_briefing", "write_briefing"]


def _repo_relative(repo: Path, candidate: Path) -> Path | None:
    """Return ``candidate`` as a repo-relative Path, or None if it's outside the repo."""
    try:
        repo_resolved = repo.resolve()
        cand_resolved = candidate.resolve()
    except OSError:
        return None
    try:
        return cand_resolved.relative_to(repo_resolved)
    except ValueError:
        return None


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or touching inclusive ranges. Input need not be sorted."""
    if not ranges:
        return []
    sorted_ranges = sorted(ranges)
    merged: list[tuple[int, int]] = [sorted_ranges[0]]
    for lo, hi in sorted_ranges[1:]:
        prev_lo, prev_hi = merged[-1]
        if lo <= prev_hi + 1:
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def extract_source_snippets(
    repo: Path,
    callstack: list[CallFrame],
    context_lines: int = 15,
    max_files: int = 5,
) -> dict[Path, str]:
    """Return ``{repo-relative path: snippet}`` for in-repo frames with file/line.

    Each snippet concatenates one or more ``// lines LO-HI`` blocks separated by
    a blank line. Blocks within a file are merged when their plus/minus context
    ranges touch or overlap. Output is capped at ``max_files`` distinct files -
    earlier frames win (they are typically closer to the crash site).
    """
    # Accumulate ranges per repo-relative path, in the order files were first seen.
    per_file: dict[Path, list[tuple[int, int]]] = {}
    order: list[Path] = []

    for frame in callstack:
        if frame.file is None or frame.line is None:
            continue

        rel = _repo_relative(repo, Path(frame.file))
        if rel is None:
            continue

        abs_path = (repo / rel).resolve()
        if not abs_path.exists() or not abs_path.is_file():
            continue

        lo = max(1, frame.line - context_lines)
        hi = frame.line + context_lines

        if rel not in per_file:
            if len(order) >= max_files:
                continue  # Cap reached; skip new files.
            per_file[rel] = []
            order.append(rel)
        per_file[rel].append((lo, hi))

    # Render each file's snippet.
    out: dict[Path, str] = {}
    for rel in order:
        abs_path = (repo / rel).resolve()
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        total = len(lines)

        blocks: list[str] = []
        for lo, hi in _merge_ranges(per_file[rel]):
            clamped_hi = min(hi, total)
            header = f"// lines {lo}-{clamped_hi}"
            body = "\n".join(lines[lo - 1 : clamped_hi])
            blocks.append(f"{header}\n{body}")
        out[rel] = "\n\n".join(blocks)

    return out


def render_briefing(
    capture: CrashCapture,
    snippets: dict[Path, str],
    build_cmd: str | None = None,
) -> str:
    """Render a Markdown crash briefing for Claude Code consumption.

    Sections appear in a fixed order: Crash briefing header, Exception,
    Call stack, Locals at frame 0, Source context, Your task, Constraints,
    Available MCP tools.

    Never renders Python ``None`` as literal text -- uses ``"?"``, ``"---"``
    or ``"n/a"`` instead.
    """
    parts: list[str] = []
    parts.append(f"# Crash briefing — crash-{capture.crash_hash}\n")
    parts.append(f"\n**PID:** {capture.pid}")
    if capture.process_name:
        parts.append(f" | **Process:** {capture.process_name}")
    if capture.binary_path:
        parts.append(f" | **Binary:** {capture.binary_path}")
    parts.append("\n")

    # Exception
    parts.append("\n## Exception\n\n")
    if capture.exception is not None:
        exc = capture.exception
        parts.append(f"- **Code:** {exc.code_name} (`0x{exc.code:08X}`)\n")
        parts.append(f"- **Address:** `0x{exc.address:016X}`\n")
        parts.append(f"- **Description:** {exc.description or 'n/a'}\n")
        parts.append(f"- **First chance:** {exc.is_first_chance}\n")
    else:
        parts.append("No exception on last event (process paused without crash).\n")

    # Call stack
    parts.append("\n## Call stack\n\n")
    if capture.callstack:
        parts.append(
            "| # | Module | Function | File | Line |\n|---|--------|----------|------|------|\n"
        )
        for f in capture.callstack:
            parts.append(
                f"| {f.index} "
                f"| {f.module or '?'} "
                f"| {f.function or '?'} "
                f"| {f.file or '---'} "
                f"| {f.line if f.line is not None else '---'} |\n"
            )
    else:
        parts.append("_No call stack frames captured._\n")

    # Locals
    parts.append("\n## Locals at frame 0\n\n")
    if capture.locals_:
        parts.append("| Name | Type | Value |\n|------|------|-------|\n")
        for loc in capture.locals_:
            val = loc.value.replace("|", "\\|")  # escape pipes in table
            parts.append(f"| {loc.name} | {loc.type} | {val} |\n")
    else:
        parts.append("_No locals captured._\n")

    # Source context
    parts.append("\n## Source context\n\n")
    if snippets:
        for path, snippet in snippets.items():
            parts.append(f"### `{path}`\n\n```cpp\n{snippet}\n```\n\n")
    else:
        parts.append(
            "_No in-repo source files referenced by the stack"
            " --- agent should use MCP to call"
            " get_callstack/get_locals for more frames._\n"
        )

    # Your task
    parts.append("\n## Your task\n\n")
    parts.append("1. Read the source files listed above.\n")
    parts.append("2. Identify the root cause of the crash.\n")
    parts.append("3. Apply the minimal fix.\n")
    if build_cmd:
        parts.append(f"4. Run the build command: `{build_cmd}`\n")
    else:
        parts.append("4. Do not attempt to build (no build command provided).\n")
    parts.append("5. Summarize the root cause in 1-2 sentences.\n")

    # Constraints
    parts.append("\n## Constraints\n\n")
    parts.append("- Only edit files listed in the Source context section.\n")
    parts.append("- Prefer surgical edits over rewrites.\n")
    parts.append("- Do not modify third-party or standard library code.\n")
    parts.append("- Do not add tests for unrelated behavior.\n")

    # Available MCP tools
    parts.append("\n## Available MCP tools\n\n")
    tools = [
        "attach_process",
        "detach_process",
        "get_exception",
        "get_callstack",
        "get_threads",
        "get_locals",
        "set_breakpoint",
        "step_next",
        "continue_execution",
    ]
    for t in tools:
        parts.append(f"- `mcp__debugbridge__{t}`\n")
    parts.append(
        "\nDo NOT call `detach_process` or `continue_execution`"
        " --- those would release or resume the target.\n"
    )

    return "".join(parts)


def write_briefing(path: Path, content: str) -> None:
    """Write briefing content to a file with UTF-8 encoding and ``\\n`` line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def append_retry_feedback(
    briefing_path: Path,
    attempt_num: int,
    build_output: str,
    claude_result_text: str | None = None,
) -> None:
    """Append a 'Previous attempt' section to an existing briefing file.

    The build output is truncated to 2000 characters to keep briefings
    within reasonable token budgets.  Claude's previous response (if
    available) is included as a blockquote capped at 500 characters.
    """
    section = f"\n\n## Previous attempt {attempt_num}\n\n"
    if claude_result_text:
        section += f"Claude proposed:\n> {claude_result_text[:500]}\n\n"
    if len(build_output) > 2000:
        build_output = build_output[:2000] + "\n[output truncated]"
    section += f"Build failed:\n\n```\n{build_output}\n```\n\nPlease produce a different fix taking this build error into account.\n"
    with open(briefing_path, "a", encoding="utf-8") as f:
        f.write(section)
