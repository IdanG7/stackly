# DebugBridge — 60-Second Demo Script

**Mode:** hand-off (matches 2a default).

**CONSTRAINT:** This script describes hand-off mode only. Do NOT demonstrate autonomous PR creation (2a opt-in behind `--auto`) or unattended crash monitoring (scoped to 2.5). The demo shows a developer explicitly invoking `debugbridge fix` against a known crashed PID — nothing else.

---

## Script

| Beat | Time | Visual | Voiceover |
|------|------|--------|-----------|
| Hook | 0:00–0:05 | `crash_app.exe` window on a remote test machine, red "Application has stopped working" error dialog center-screen. Text overlay: "Your C++ app crashed on a test machine. What now?" | Your C++ app just crashed on a test machine across the office. What now? |
| Problem | 0:05–0:15 | Split-screen montage: developer walking to the test machine, reading a stack trace, copying text into Slack, pasting into a chat window, tabbing between windows. A clock in the corner ticks from 00:00 to 30:00. | You walk over, read the stack, copy it into Slack, paste it into Claude, write a fix. Thirty minutes gone. Multiply by five crashes a day and your whole week is wrecked. |
| Solution setup | 0:15–0:25 | Cut to the developer's own machine. Clean terminal, single line typed: `debugbridge fix --pid 4892 --repo .` Cursor blinks. Enter is pressed at 0:23. | DebugBridge flips this. From your dev machine, run one command pointed at the crashed process and your repository. |
| Live capture | 0:25–0:40 | Terminal streams output: `Capturing crash…` then `47 stack frames` then `12 local variables` then `Writing briefing…` then `Launching Claude Code…`. A Claude Code window slides in from the right with the briefing already visible in its context panel. No headless flag, no `.patch` file written, no PR. | DebugBridge captures the crash, pulls forty-seven stack frames and twelve local variables into a structured briefing, then hands it off to Claude Code with all the context already preloaded. No copy, no paste. |
| AI diagnosis | 0:40–0:50 | Zoom into the Claude Code chat panel. Claude's first message reads: "Null dereference at `render_target` in `draw.cpp:127`. Proposed fix:" followed by a unified-diff patch preview. The developer's cursor hovers over the patch — does not click apply. | Claude reads the briefing and spots a null dereference on render_target in draw.cpp at line one twenty-seven, then proposes a patch you can review right there in your editor. |
| CTA | 0:50–1:00 | Full-screen card: large `debugbridge.dev` URL, GitHub octocat logo below, three small agent-name chips underneath: "Claude Code · Cursor · Claude Desktop". Static, no animation. | Hand-off, not magic. You stay in the driver's seat. Works with Claude Code, Cursor, and Claude Desktop. Install from GitHub. debugbridge.dev. |

**Voiceover word count:** 147 words

---

## Read-aloud test

Before recording the final take, the narrator must run this test:

1. Start a stopwatch.
2. Read the full Voiceover column aloud at a natural, conversational pace — the pace you would use explaining the tool to a colleague over coffee. Do not rush. Do not artificially slow down.
3. Stop the watch at the end of the CTA line.
4. **Target:** 60–65 seconds total.
5. **If the reading lands under 60s:** pace is too fast. Re-read more naturally; do not pad the script.
6. **If the reading lands over 65s:** the script is too long. Cut words from the Problem or Live-capture beats (the two longest rows) until a natural re-read lands in the 60–65s window. Do NOT compensate by speeding up delivery — a rushed voiceover reads as low-confidence and will undercut the product.
7. Repeat until three consecutive reads all land inside the 60–65s window. That is your cue that the script is recording-ready.

The on-screen timestamps in the table above are fixed production targets. The beat boundaries (0:05, 0:15, 0:25, 0:40, 0:50) are the cues the editor will use when syncing voiceover to B-roll, so the narrator's delivery must respect them within roughly ±1 second per beat.
