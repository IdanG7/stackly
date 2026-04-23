import { useState } from 'react'

const TOOLS = [
  {
    name: 'attach_process',
    sig: '(pid_or_name, transport?)',
    purpose: 'Attach to a local or remote process.',
    long:
      'Attaches the Stackly server to a running or crashed Windows process. Works against the local machine or, via dbgsrv over TCP, against a process on a remote box.',
    params: [
      ['pid_or_name', 'int | str', 'PID, or executable name like "myapp.exe"'],
      ['transport', 'str?', '"tcp:server=<host>,port=<n>" for remote dbgsrv; omit for local'],
    ],
    call: 'attach_process("myapp.exe", transport="tcp:server=192.168.1.10,port=5555")',
    ret: '{ "status": "attached", "pid": 4208, "module": "myapp.exe" }',
  },
  {
    name: 'get_exception',
    sig: '()',
    purpose: 'Read the current exception / crash info.',
    long:
      'Returns the structured exception record at the current halt point — code, kind, faulting address, and the thread that raised it.',
    params: [],
    call: 'get_exception()',
    ret: '{ "code": "0xC0000005", "kind": "ACCESS_VIOLATION", "address": "0x0", "thread": 6 }',
  },
  {
    name: 'get_callstack',
    sig: '(thread?)',
    purpose: 'Full stack with file paths and line numbers.',
    long:
      'Walks the call stack for the given thread (defaults to the halted one), resolving symbols and source coordinates wherever debug info exists.',
    params: [
      ['thread', 'int?', 'thread id; defaults to the thread that raised the exception'],
    ],
    call: 'get_callstack(thread=6)',
    ret: '[ { "frame": 0, "symbol": "FrameStore::flush", "source": "frame_store.cpp:412" }, ... ]',
  },
  {
    name: 'get_threads',
    sig: '()',
    purpose: 'Enumerate every thread and its state.',
    long:
      'Lists every thread in the target process with id, state (running / suspended / blocked / faulting), and its top frame. Useful for spotting deadlocks or races around the crash.',
    params: [],
    call: 'get_threads()',
    ret: '[ { "tid": 0, "state": "running", "top": "main" }, { "tid": 6, "state": "fault", "top": "FrameStore::flush" }, ... ]',
  },
  {
    name: 'get_locals',
    sig: '(frame)',
    purpose: 'Local variables for a given stack frame.',
    long:
      'Evaluates locals visible in the specified frame. Pointer and struct fields are expanded one level so null dereferences reveal themselves immediately.',
    params: [
      ['frame', 'int', 'frame index returned by get_callstack (0 = faulting frame)'],
    ],
    call: 'get_locals(frame=0)',
    ret: '[ { "name": "this", "type": "RenderPipeline*", "value": "0x0" }, { "name": "chunk_count", "type": "int32_t", "value": 142 }, ... ]',
  },
  {
    name: 'set_breakpoint',
    sig: '(at)',
    purpose: 'Breakpoint at file:line or module!symbol.',
    long:
      'Sets a software breakpoint and returns the resolved address so the AI can reason about what it will catch.',
    params: [
      ['at', 'str', '"frame_store.cpp:412" or "RenderCore.dll!FrameStore::flush"'],
    ],
    call: 'set_breakpoint(at="frame_store.cpp:412")',
    ret: '{ "id": 3, "address": "0x7ff6a2118a2c", "resolved": "frame_store.cpp:412" }',
  },
  {
    name: 'step_next',
    sig: '()',
    purpose: 'Step over one line.',
    long:
      'Advances the current thread by one source line, stepping over function calls. Halts and returns the new position.',
    params: [],
    call: 'step_next()',
    ret: '{ "stopped_at": "frame_store.cpp:413", "reason": "step" }',
  },
  {
    name: 'continue_execution',
    sig: '()',
    purpose: 'Resume the process.',
    long:
      'Detaches from the halt and lets the process continue until the next breakpoint, exception, or exit.',
    params: [],
    call: 'continue_execution()',
    ret: '{ "status": "running" }',
  },
]

function LedgerItem({ tool, open, onToggle }) {
  return (
    <div className={`ledger-item${open ? ' open' : ''}`}>
      <button
        type="button"
        className="ledger-item-btn"
        onClick={onToggle}
        aria-expanded={open}
      >
        <span className="li-name">{tool.name}</span>
        <span className="li-sig">{tool.sig}</span>
        <span className="li-purpose">{tool.purpose}</span>
        <span className="li-caret" aria-hidden="true">+</span>
      </button>
      <div className="ledger-detail" aria-hidden={!open}>
        <div className="ledger-detail-inner">
          <div className="ledger-detail-body">
            <div className="block">
              <p className="long">{tool.long}</p>
              <div>
                <h4>Parameters</h4>
                {tool.params.length === 0 ? (
                  <div className="params-empty">None.</div>
                ) : (
                  <div className="params">
                    {tool.params.map(([name, type, desc]) => (
                      <ParamRow key={name} name={name} type={type} desc={desc} />
                    ))}
                  </div>
                )}
              </div>
            </div>
            <div className="block">
              <div>
                <h4>Example call</h4>
                <pre><span className="prompt">›</span>{tool.call}</pre>
              </div>
              <div>
                <h4>Example response</h4>
                <pre><span className="retarrow">←</span>{tool.ret}</pre>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function ParamRow({ name, type, desc }) {
  return (
    <>
      <span className="p-name">{name}</span>
      <span className="p-type">{type}</span>
      <span className="p-desc">{desc}</span>
    </>
  )
}

export default function Tools() {
  const [openSet, setOpenSet] = useState(() => new Set([0]))

  const toggle = (i) => {
    setOpenSet((prev) => {
      const next = new Set(prev)
      next.has(i) ? next.delete(i) : next.add(i)
      return next
    })
  }

  return (
    <section className="theme-ink h-rule reveal" id="tools">
      <div className="layout-grid">
        <div className="gutter-col" aria-hidden="true"></div>
        <div className="ledger-wrap">
          <div className="ledger">
            <header className="ledger-opener">
              <div className="opener-eyebrow">Appendix A</div>
              <blockquote className="opener-quote">
                Not a parsed trace.<br />The debugger, <em>live</em>.
              </blockquote>
              <p className="opener-intro">
                Eight MCP tools. Each returns live state from the running target —
                exceptions, stacks, threads, locals, breakpoints, and execution
                control. Expand any row for its full signature, an example call, and
                an example response.
              </p>
            </header>

            <div className="ledger-head">
              <span>Tool ledger · 08</span>
              <span className="hint">click to expand</span>
            </div>

            <div>
              {TOOLS.map((t, i) => (
                <LedgerItem
                  key={t.name}
                  tool={t}
                  open={openSet.has(i)}
                  onToggle={() => toggle(i)}
                />
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
