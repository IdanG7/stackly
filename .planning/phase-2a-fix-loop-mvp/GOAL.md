# Phase 2a — Fix-loop MVP — Goal

## The phase goal (one sentence)

A developer can run `debugbridge fix --pid N --repo D:/myapp` on a live crashed Windows process and get back either an interactive Claude Code session preloaded with rich crash context, or (with `--auto`) a validated `.patch` file that addresses the crash — without DebugBridge ever touching the developer's main working tree.

## Success criteria (phase exits when all are true)

1. **Hand-off mode works end-to-end:**
   - `debugbridge fix --pid N --repo PATH` connects to the `debugbridge serve` MCP server, captures crash state (exception + stack + locals + threads), writes a human-readable briefing Markdown file, and launches Claude Code interactively in `PATH` with the briefing loaded as context.
   - Tested with a real crashed `crash_app.exe` (null-deref mode) — the developer sees a Claude Code session with stack trace and source file hints already on screen.

2. **Autonomous mode works end-to-end:**
   - `debugbridge fix --pid N --repo PATH --auto --build-cmd "cmake --build build --config Debug"` runs without user interaction, creates a git worktree at `.debugbridge/wt-<hash>/`, launches Claude Code headless (`claude -p`), allows Claude Code to edit files within the worktree, runs the build command after the edit, and on build success emits a unified diff at `.debugbridge/patches/crash-<hash>.patch`.
   - Iteration cap: if build fails, one retry with build error as additional context; after 2nd failure, write `.debugbridge/patches/crash-<hash>.failed.md` with diagnostic info and exit non-zero.

3. **Agent ↔ server coupling is strictly MCP:**
   - The `fix` CLI is an MCP client. It talks to `debugbridge serve` (auto-started as a subprocess if not running) over Streamable HTTP. No direct `from debugbridge.session import DebugSession`.
   - This means the agent exercises the same code path a customer's Claude Code would — if something is missing from the MCP surface, the agent will expose it first.

4. **Repo isolation is real:**
   - Every file write lives inside `$REPO/.debugbridge/`.
   - The user's working tree, branches, and remote state are untouched by autonomous mode — worktrees are local, diffs are just files.
   - Hand-off mode opens Claude Code in the user's tree but doesn't modify anything itself.
   - `.debugbridge/` is added to the user's `.gitignore` automatically on first run if not present.

5. **Build/test validation is user-parametric:**
   - `--build-cmd` and `--test-cmd` run inside the worktree after Claude Code's edit pass.
   - Exit code determines pass/fail. Output captured, fed back to Claude Code on retry.
   - Zero auto-detection in v1 — boring but correct.

6. **Cost tracking exists:**
   - Each autonomous fix run reports tokens in/out and estimated cost on completion (parsed from Claude Code's headless output or Anthropic API response).
   - Hard 3-attempt cap prevents runaway token spend.

7. **Tests prove it:**
   - At least one integration test that drives `debugbridge fix --pid N --auto` against a running `crash_app.exe` in a throwaway worktree and asserts a non-empty patch is produced.
   - Unit tests for the briefing-format generator, worktree path hashing, and CLI argument parsing.
   - All tests pass in CI where Claude Code and Debugging Tools are unavailable — integration tests auto-skip.

## Non-goals (explicitly out of scope)

- Crash auto-detection — Phase 2.5
- PyPI publish — Phase 2c
- Non-Windows platforms — Phase 3
- Cloud relay — Phase 4
- "Smart" build-system auto-detection
- Multi-crash batch processing
- A daemon / watcher mode
- Claude Code UI customization

## Constraints to respect

- No change to the existing 8 MCP tools' public signatures (they're shipping in 2c).
- `DebugSession` remains the single pybag consumer; the fix agent doesn't get to reach around it.
- Hard rule from Phase 1: pybag import must stay lazy — don't regress.
- Windows-only for now. The CLI may print "not supported on $platform" on non-Windows for flags that need Windows-specific features.

## Acceptance demo

When Phase 2a is done, this exact sequence works on a fresh Windows dev machine with DebugBridge installed:

```powershell
# Terminal A: start a crashing process
D:\Projects\BridgeIt\tests\fixtures\crash_app\build\Debug\crash_app.exe null
# (crashes)

# Terminal B: quick manual attach before crash
D:\Projects\BridgeIt\tests\fixtures\crash_app\build\Debug\crash_app.exe wait
# (blocks on stdin; note PID)

# Terminal C: invoke the fix agent
debugbridge fix --pid <PID> --repo D:/Projects/BridgeIt `
    --auto `
    --build-cmd "cmake --build tests/fixtures/crash_app/build --config Debug"

# Expected output:
#   [debugbridge] attaching to pid <PID>...
#   [debugbridge] captured: EXCEPTION_ACCESS_VIOLATION @ myapp!crash_null+0x2a
#   [debugbridge] working in worktree .debugbridge/wt-a1b2c3d4
#   [claude-code] analyzing crash...
#   [claude-code] proposed fix: add null-check before dereference
#   [claude-code] applying edit to crash.cpp...
#   [debugbridge] running build command...
#   [debugbridge] build passed
#   [debugbridge] patch written: .debugbridge/patches/crash-a1b2c3d4.patch
#   [debugbridge] tokens: 24K in / 2.1K out, est cost $0.18
#   [debugbridge] apply with: git apply .debugbridge/patches/crash-a1b2c3d4.patch
```
