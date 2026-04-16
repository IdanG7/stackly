"""DebugBridge fix-loop subpackage — the autonomous / interactive crash-repair agent.

Phase 2a. All modules here are pure Python at import time — no pybag, no MCP,
no subprocess launch. Heavy imports (mcp client, claude CLI subprocess) happen
inside function bodies. This preserves the Phase 1 invariant that the top-level
``debugbridge`` CLI loads on machines without Windows Debugging Tools installed.

Public surface is exposed through :mod:`debugbridge.fix.dispatcher` (lands in
task 2a.2.2 for handoff mode and task 2a.3.5 for autonomous mode).
"""
