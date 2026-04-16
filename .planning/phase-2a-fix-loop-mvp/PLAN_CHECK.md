# Phase 2a Plan Check

**Verdict: APPROVED-WITH-FIXES**
**Confidence: HIGH** — plan is substantially complete and directly executable; the issues below are concrete but bounded.

## Goal coverage matrix

| # | Goal acceptance criterion (abbrev.) | Covered? | Tasks providing evidence | Confidence |
|---|-------------------------------------|----------|--------------------------|------------|
| 1 | Hand-off E2E with real crash_app    | ⚠ Weak   | 2a.1.1–2a.1.5, 2a.2.1, 2a.2.2, 2a.4.1; manual §7 | **M** |
| 2 | Autonomous E2E → `.patch`           | ✓ Strong | 2a.3.1–2a.3.9, 2a.4.1, 2a.4.2 | H |
| 3 | Agent↔server coupling strictly MCP  | ⚠ Gap in enforcement | Arch decision #1 pinned; Appendix A mentions grep check but no task implements it | M |
| 4 | Repo isolation, writes in `.debugbridge/` | ✓ Strong | 2a.2.1, 2a.3.1, 2a.3.3, 2a.3.5 (sentinel test) | H |
| 5 | Build/test user-parametric           | ✓ Strong | 2a.3.2, 2a.3.5, 2a.3.7 | H |
| 6 | Cost tracking + 3-attempt cap       | ✓ Strong | 2a.3.4, 2a.3.5, 2a.3.6, 2a.3.9 | H |
| 7 | Tests prove it                       | ✓ Strong | 2a.4.2 + per-feature unit tests + CI skip markers | H |

**Overall:** 5 strong, 2 weak-but-resolvable. No outright gap.

---

## Critical issues (fix before executing)

### C1. `compute_crash_hash` location contradicts itself

**PLAN.md:143** (task 2a.1.2, action): *"...put the hash computation in this file..."* (i.e. `mcp_client.py`)
**PLAN.md:245** (task 2a.3.1, action): *"Also add `compute_crash_hash(capture: CrashCapture) -> str` here (imported by `mcp_client.capture_crash` — acceptable: no cycle...)"* (i.e. `worktree.py`)

These two tasks contradict each other. Executor will either:
- Implement it twice (wasted work), or
- Skip one, hit an import error when the other task's acceptance test runs.

**Fix:** Commit to ONE location. Recommendation — put it in `worktree.py` (since worktrees name themselves by it), and have `mcp_client.capture_crash` import it. Update PLAN.md:143 to say "crash-hash computation lives in `worktree.py` (task 2a.3.1); `capture_crash` imports it." Then 2a.3.1 depends on 2a.0.3 only, and 2a.1.2 depends on 2a.0.3 + 2a.3.1. Dep graph re-orders cleanly.

### C2. `AttemptRecord` schema is retroactively mutated by task 2a.3.7

**PLAN.md:336** (task 2a.3.7, action): *"...rename to `pass_ok: bool` and add `test_ok: bool | None` to the record model (update 2a.0.3 task's acceptance expectations if it was closed — this is a backward-compatible model extension)"*

Problem: 2a.0.3 runs first. If it ships with `build_ok` and no `test_ok`, then 2a.3.7 has to touch the closed task's model + its round-trip test. TDD ordering breaks; a later task mutates an earlier task's artifact.

**Fix:** Define the final schema now, in 2a.0.3. `AttemptRecord` should be:
```python
class AttemptRecord(BaseModel):
    attempt: int
    claude_result: ClaudeRunResult
    build_ok: bool
    build_output: str
    test_ok: bool | None = None      # None = not run
    test_output: str | None = None
    duration_s: float
```
Then 2a.3.7 only adds *usage* of the field; it doesn't amend the model. Update PLAN.md:113 accordingly, and remove the retroactive language in 2a.3.7.

### C3. "No `DebugSession` import in `fix/`" constraint has no enforcing task

**Goal criterion 3** says coupling is *"strictly MCP."* The plan pins this as an architecture decision and mentions a grep/CI rule in Appendix A (PLAN.md:568), but **no task in §4 actually adds that check.** It's in the appendix, not the task list — so `gsd-executor` has no instruction to write it.

Without a concrete check, a future execution-time shortcut ("just import DebugSession for this one thing") becomes undetectable.

**Fix:** Add a tiny task (call it 2a.0.4) or extend 2a.4.3 with an explicit sub-action:
> Add to CI: `if rg -q "from debugbridge\.session" src/debugbridge/fix; then echo "fix/ must not import DebugSession directly"; exit 1; fi` as a new step in `.github/workflows/ci.yml` before `ruff check`.

Tiny. But without it, constraint is aspirational.

---

## Concerns (should address before executing; not hard blockers)

### N1. Hand-off mode has no automated test at all

Task 2a.4.2's "hand-off variant" is handwaved (PLAN.md:454): *"at minimum: the smoke script prints 'About to hand off — expect claude to start interactively. Press Ctrl+C after verifying.'"* That's not a test — it's a manual ritual.

Goal criterion 1 says the hand-off mode is *"tested"*. The most this plan does is assert the argv shape (task 2a.2.2) with a monkeypatched `subprocess.run`. That proves the CLI *invokes* claude correctly, not that the end-to-end flow *works* (claude finds the briefing, reads it, the MCP server is reachable from claude's context, etc.).

**Fix:** Either (a) accept that 2a.2.2's argv-assertion is sufficient proof of criterion 1 and update GOAL.md to match, or (b) add an integration test that invokes `claude -p` (headless, not interactive) against the hand-off-shaped prompt — verifying the briefing + MCP config work end-to-end in the non-interactive harness. Option (b) is stronger and reuses the headless wrapper that task 2a.3.4 builds anyway.

### N2. R9 (port 8585 bound by non-DebugBridge process) has no concrete implementation task

Risk R9's mitigation (PLAN.md:470): *"if `capture_crash` gets back MCP errors (not our 9 tools), fail fast"* — but no task lists "tool-presence check" in its acceptance criteria. Will executor remember to add this?

**Fix:** Extend task 2a.1.2's acceptance with one bullet:
> After `session.initialize()`, call `list_tools` and assert the result contains `attach_process` and `detach_process`. If not, raise `DebugSessionError("Port {port} is bound by a non-DebugBridge MCP server; pass --port N or stop the other process")`.

### N3. Sentinel-file test in 2a.3.5 has a soft failure mode

Task 2a.3.5 asserts *"no new files exist outside `.debugbridge/`"* (PLAN.md:316). But the monkeypatched claude-headless fn is likely stubbed to do nothing (no real edits). So the test passes trivially — doesn't prove claude *can't* escape the worktree, only that the stub doesn't.

**Fix:** Add a second test variant where the monkeypatched claude stub *tries* to write `main_tree_pollution.txt` at the repo root (outside worktree), and assert the file ends up inside the worktree (because Claude Code's `cwd` is the worktree), not in the main tree. Proves the cwd-based sandbox is real.

### N4. Task 2a.1.2 has an embedded structural dependency on 2a.3.1 not expressed in the graph

Appendix B says 2a.1.2 depends on 2a.0.3 and 2a.1.1. But the task body (PLAN.md:143) computes the crash hash inline — except per C1 above, that logic should move to 2a.3.1's `worktree.py`. Once C1 is fixed, 2a.1.2 picks up a dependency on 2a.3.1. This reshuffles the execution order:

- Before fix: `2a.0.3 → 2a.1.1 → 2a.1.2` (capture depends only on server + models)
- After fix: `2a.0.3 → 2a.2.1 → 2a.3.1 → 2a.1.2` (capture depends on worktree for hash compute)

Critical path may lengthen by 1. Update Appendix B.

### N5. Test file naming inconsistency

Plan creates `tests/test_cli.py` for the `fix` command's CLI tests (PLAN.md:399) but all other fix-subpackage tests are `test_fix_*.py`. Inconsistent.

**Fix:** Rename to `tests/test_fix_cli.py` or move the test into an existing `test_cli.py` if one is planned. Minor.

### N6. M-sized tasks still flagged but not split

5 tasks are flagged M (2a.1.2, 2a.2.2, 2a.3.4, 2a.3.5, 2a.4.2). Plan suggests splits for each, but doesn't pre-commit. Executor will decide mid-task whether to split, which is fine but adds mid-flight planning overhead.

**Fix:** Either (a) pre-split in PLAN.md (safer, more atomic), or (b) explicitly accept that `gsd-executor` handles the split call at execution time (current plan). The plan chose (b), which is defensible — noting as a concern, not a blocker.

---

## Nice-to-haves (address during execution if time permits)

- **Optional: `/health` endpoint** on `debugbridge serve` to give the ensure-logic a definitive probe. Current TCP probe is weaker. Out of Phase 2a scope by the plan's own admission; file for Phase 2c.
- **Briefing template versioning.** If the briefing Markdown format changes across releases, stored `.debugbridge/briefings/crash-<hash>.md` files become stale. A `<!-- briefing-schema: 1 -->` header would help future tooling. Cosmetic.
- **Rich progress in no-TTY** — 2a.3.9 says "emits plain prints in a no-TTY env". Rich does this natively, but worth verifying in CI logs.

---

## Handling of research-surfaced open items

| RESEARCH.md item | Plan handling | OK? |
|-|-|-|
| Missing `detach_process` MCP tool | Task 2a.0.1 adds it | ✓ |
| `--max-budget-usd` counts MCP tool tokens | R3 mitigation: per-attempt budget + max_attempts + `MAX_MCP_OUTPUT_TOKENS` env (PLAN.md:464) | ✓ |
| First-run `bypassPermissions` prompt | Task 2a.0.2 doctor check + README | ✓ |
| Session resumption (stretch) | Task 2a.3.10 marked optional, feature-flagged | ✓ |

No research item slipped through.

---

## Constraint compliance (from GOAL.md §Constraints)

| Constraint | Honored? | Evidence |
|-|-|-|
| No breaking change to 8 MCP tools | ✓ | 2a.0.1 adds `detach_process` (new, not modified); `scripts/e2e_smoke.py` expected set goes 8→9 |
| Pybag imports stay lazy | ✓ | `cli.py`'s `fix` command lazy-imports `dispatcher` (2a.4.1); only `session.py` adds a method (no new import at file scope) |
| No direct `DebugSession` import in fix/ code | ⚠ See C3 | Pinned as decision, enforcement mechanism missing from task list |
| New deps justified | ✓ | **Zero new deps** — all stdlib + existing |
| New code under `src/debugbridge/fix/` | ✓ | Only 4 surgical Phase-1 extensions (session.detach, tools.detach_process, cli.fix, env.check_claude_cli), each unavoidable |

---

## Affirmed decisions

1. **Zero new Python dependencies.** Strong — keeps supply chain tiny, eases PyPI release in Phase 2c.
2. **Dog-food MCP coupling.** Agent exercises the same surface customers will rely on. If MCP is missing something, the agent finds out first.
3. **Git worktree per fix.** Cleaner than branching the user's repo; leaves main tree untouched; easy diff extraction.
4. **User-parametric build/test commands.** Deliberately boring — avoids build-system auto-detect rabbit hole.
5. **Hand-off primary + autonomous secondary.** Matches user's stated preference; shared capture pipeline reduces duplication.
6. **Lazy `cli.py` imports for the `fix` command.** Preserves Phase 1's critical invariant (`doctor` works without Debugging Tools).
7. **`@pytest.mark.slow` tier for claude-dependent tests.** Keeps CI fast and free; makes the expensive tests opt-in for dev machines only.
8. **Stretch task 2a.3.10 correctly marked skippable.** Goal criterion 6 is satisfied without it.

---

## Recommendation

**Proceed to execution after these 3 pre-execution fixes:**

1. **C1** — pin `compute_crash_hash` to `worktree.py`; update 2a.1.2's action text and dependency chain to import it from there.
2. **C2** — define `AttemptRecord` with `test_ok: bool | None = None` in 2a.0.3 from the start; remove retroactive language from 2a.3.7.
3. **C3** — add an explicit sub-action to task 2a.4.3 (or a new task 2a.0.4) that adds the `rg` / grep CI step enforcing "no `DebugSession` import in `fix/`."

Each is a small edit to PLAN.md. Total fix time: 5–10 minutes.

**Optional-but-recommended before execution:**
- **N1** — decide whether hand-off mode gets a real automated test (headless via `claude -p` with the hand-off-shaped prompt) or whether argv-assertion is the committed evidence for Goal criterion 1. Update GOAL.md if the latter.
- **N2** — add the tool-presence check to 2a.1.2's acceptance (1 bullet).

**Defer to execution-time decisions:**
- N3, N4, N5, N6 are either minor or already handled by the "flagged M → split if needed" convention. No pre-execution change needed.

**Blocking issues: 0.** The plan is good. The fixes above are hygiene, not rescue.
