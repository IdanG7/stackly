"""Typer CLI for Stackly.

Kept intentionally free of pybag / session imports at module load time so that
``stackly doctor`` and ``stackly version`` work on a machine without
Windows Debugging Tools installed. The server is only imported inside ``serve``.
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.table import Table

from stackly import __version__
from stackly.env import (
    check_claude_bypass_acknowledged,
    check_claude_cli,
    check_debugging_tools,
)

app = typer.Typer(
    name="stackly",
    help="Remote crash capture MCP server for native Windows applications.",
    add_completion=False,
    no_args_is_help=True,
)

console = Console()


@app.command()
def serve(
    transport: str = typer.Option(
        "http",
        "--transport",
        "-t",
        help='Transport: "http" (Streamable HTTP, recommended) or "stdio".',
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind host for HTTP transport."),
    port: int = typer.Option(8585, "--port", "-p", help="Bind port for HTTP transport."),
    skip_env_check: bool = typer.Option(
        False, "--skip-env-check", help="Start even if Debugging Tools look absent (debug use)."
    ),
) -> None:
    """Start the Stackly MCP server."""
    if transport not in ("http", "stdio"):
        console.print(f"[red]Invalid transport: {transport}. Use 'http' or 'stdio'.[/red]")
        raise typer.Exit(code=2)

    if not skip_env_check:
        result = check_debugging_tools()
        if not result.ok:
            console.print("[red]Windows Debugging Tools not found.[/red]\n")
            console.print(result.guidance or "")
            console.print(
                "\nRe-run with --skip-env-check to start anyway (server will fail on first attach)."
            )
            raise typer.Exit(code=1)

    # Deferred import — loads pybag, which requires dbgeng.dll.
    from stackly.server import run

    if transport == "http":
        console.print(
            f"[green]Stackly[/green] serving on [bold]http://{host}:{port}/mcp[/bold]"
        )
    else:
        console.print("[green]Stackly[/green] serving on stdio")
    run(transport=transport, host=host, port=port)  # type: ignore[arg-type]


@app.command()
def doctor() -> None:
    """Check that Windows Debugging Tools and Claude Code prereqs are present."""
    result = check_debugging_tools()
    claude_result = check_claude_cli()
    bypass_result = check_claude_bypass_acknowledged()

    table = Table(title="Stackly environment check", show_header=True)
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Path")

    all_items = sorted({*result.found.keys(), *result.missing})
    for name in all_items:
        if name in result.found:
            table.add_row(name, "[green]found[/green]", result.found[name])
        else:
            table.add_row(name, "[red]missing[/red]", "—")

    # Phase 2a additions — claude CLI (hard fail) and bypass acknowledgement (warning).
    if claude_result.ok:
        table.add_row(
            "claude CLI",
            "[green]found[/green]",
            claude_result.found.get("claude", "—"),
        )
    else:
        table.add_row("claude CLI", "[red]missing[/red]", "—")

    if bypass_result.ok:
        table.add_row(
            "claude bypass ack'd",
            "[green]found[/green]",
            bypass_result.found.get("claude-bypass-ack", "—"),
        )
    else:
        # Yellow, not red — missing bypass ack is a warning, not a hard failure.
        table.add_row("claude bypass ack'd", "[yellow]warning[/yellow]", "—")

    console.print(table)

    hard_fail = (not result.ok) or (not claude_result.ok)

    if not hard_fail:
        console.print("\n[green]All required components are present.[/green]")
        if not bypass_result.ok:
            console.print(
                "\n[yellow]Warning: claude bypass-permission prompt not yet acknowledged.[/yellow]\n"
            )
            console.print(bypass_result.guidance or "")
        raise typer.Exit(code=0)

    if not result.ok:
        console.print(f"\n[yellow]Missing: {', '.join(result.missing)}[/yellow]\n")
        console.print(result.guidance or "")

    if not claude_result.ok:
        console.print("\n[yellow]Missing: claude CLI[/yellow]\n")
        console.print(claude_result.guidance or "")

    if not bypass_result.ok:
        console.print(
            "\n[yellow]Warning: claude bypass-permission prompt not yet acknowledged.[/yellow]\n"
        )
        console.print(bypass_result.guidance or "")

    raise typer.Exit(code=1)


@app.command()
def fix(
    pid: int = typer.Option(..., "--pid", help="PID of the target process."),
    repo: str = typer.Option(".", "--repo", help="Path to the git repository."),
    conn_str: str | None = typer.Option(
        None, "--conn-str", help="Remote dbgsrv connection string."
    ),
    build_cmd: str | None = typer.Option(
        None, "--build-cmd", help="Build command to validate fix."
    ),
    test_cmd: str | None = typer.Option(None, "--test-cmd", help="Test command to validate fix."),
    auto: bool = typer.Option(False, "--auto", help="Run autonomously (headless claude -p)."),
    host: str = typer.Option("127.0.0.1", "--host", help="MCP server host."),
    port: int = typer.Option(8585, "--port", help="MCP server port."),
    model: str = typer.Option("sonnet", "--model", help="Claude model for autonomous mode."),
    max_attempts: int = typer.Option(
        3, "--max-attempts", help="Max fix attempts in autonomous mode."
    ),
) -> None:
    """Run the crash-fix agent against a live process."""
    import shutil
    from pathlib import Path

    from stackly.fix.worktree import is_git_repo

    repo_path = Path(repo).resolve()

    if not repo_path.exists() or not is_git_repo(repo_path):
        console.print(f"[red]Error:[/red] {repo_path} is not a git repository.")
        raise typer.Exit(code=1)

    if not shutil.which("claude"):
        console.print("[red]Error:[/red] claude CLI not found on PATH. Run `stackly doctor`.")
        raise typer.Exit(code=1)

    # Lazy import -- don't load fix/ until actually needed to preserve
    # the Phase 1 invariant that cli.py loads without pybag.
    from stackly.fix.dispatcher import run_autonomous, run_handoff

    if auto:
        result = run_autonomous(
            repo=repo_path,
            pid=pid,
            host=host,
            port=port,
            build_cmd=build_cmd,
            test_cmd=test_cmd,
            model=model,
            max_attempts=max_attempts,
            conn_str=conn_str,
        )
    else:
        result = run_handoff(
            repo=repo_path,
            pid=pid,
            host=host,
            port=port,
            conn_str=conn_str,
        )

    if not result.ok:
        raise typer.Exit(code=1)


@app.command()
def watch(
    pid: int = typer.Option(..., "--pid", help="PID of the target process."),
    repo: str = typer.Option(".", "--repo", help="Path to the git repository."),
    host: str = typer.Option("127.0.0.1", "--host", help="MCP server host."),
    port: int = typer.Option(8585, "--port", help="MCP server port."),
    conn_str: str | None = typer.Option(
        None, "--conn-str", help="Remote dbgsrv connection string."
    ),
    build_cmd: str | None = typer.Option(
        None, "--build-cmd", help="Build command to validate fix."
    ),
    test_cmd: str | None = typer.Option(None, "--test-cmd", help="Test command to validate fix."),
    auto: bool = typer.Option(False, "--auto", help="Run autonomously (headless claude -p)."),
    model: str = typer.Option("sonnet", "--model", help="Claude model for autonomous mode."),
    max_attempts: int = typer.Option(
        3, "--max-attempts", help="Max fix attempts in autonomous mode."
    ),
    max_crashes: int = typer.Option(
        1,
        "--max-crashes",
        help="Max crashes to handle before exiting (stay-resident mode >1; default one-shot).",
    ),
    max_wait_minutes: int | None = typer.Option(
        None,
        "--max-wait-minutes",
        help="Hard deadline for each watch (minutes). None = wait forever.",
    ),
    poll_seconds: int = typer.Option(
        1,
        "--poll-seconds",
        help="Poll interval in seconds. Clamped to 1s minimum by pybag.",
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress the Rich spinner while waiting."),
) -> None:
    """Watch a process for crashes and auto-dispatch the fix agent."""
    import shutil
    from pathlib import Path

    from stackly.fix.worktree import is_git_repo

    repo_path = Path(repo).resolve()

    if not repo_path.exists() or not is_git_repo(repo_path):
        console.print(f"[red]Error:[/red] {repo_path} is not a git repository.")
        raise typer.Exit(code=1)

    if auto and not shutil.which("claude"):
        console.print("[red]Error:[/red] claude CLI not found on PATH. Run `stackly doctor`.")
        raise typer.Exit(code=1)

    # Lazy import — don't load watch/ until actually needed to preserve
    # the Phase 1 invariant that cli.py loads without pybag.
    from stackly.watch.dispatcher import run_watch

    exit_code = run_watch(
        repo=repo_path,
        pid=pid,
        host=host,
        port=port,
        auto=auto,
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        model=model,
        max_attempts=max_attempts,
        conn_str=conn_str,
        max_crashes=max_crashes,
        max_wait_minutes=max_wait_minutes,
        poll_seconds=poll_seconds,
        quiet=quiet,
    )

    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command()
def version() -> None:
    """Print Stackly's version and exit."""
    console.print(f"stackly {__version__}")


def main() -> None:
    """Entry point wrapper — lets us set exit codes consistently."""
    try:
        app()
    except typer.Exit:
        raise
    except Exception as e:  # pragma: no cover — top-level safety net
        console.print(f"[red]Fatal:[/red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
