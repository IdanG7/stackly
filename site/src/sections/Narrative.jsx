import { useEffect, useRef, useState } from 'react'

const EXHIBITS = [
  {
    roman: 'II',
    eyebrow: 'Exhibit B · Capture',
    heading: ['Resolve the full call stack', 'at the faulting frame.'],
    body:
      'When an exception halts the target, Stackly walks the full call stack and resolves symbols to source coordinates. Your agent reads the crash in the same terms as the debugger — frames, modules, files, and line numbers.',
    caption: '† frame 00 — symbols still resolve cleanly.',
  },
  {
    roman: 'III',
    eyebrow: 'Exhibit C · Transport',
    heading: ['Across the network,', 'not the file system.'],
    body:
      'Stackly runs on your workstation and connects to Microsoft’s dbgsrv over TCP. Raw debugger state streams from the remote target into your local MCP client. No mapped drives, no file sync, no crash dump shuffle.',
    caption: '† any Windows host reachable over TCP.',
  },
  {
    roman: 'IV',
    eyebrow: 'Exhibit D · Inspection',
    heading: ['Read every local', 'at the halt point.'],
    body:
      'get_locals(frame) returns every binding visible in the requested frame. Pointer and struct fields expand one level, so null dereferences and corrupted members surface immediately — without instrumentation, without extra logging.',
    caption: '† null dereference, observed directly.',
  },
  {
    roman: 'V',
    eyebrow: 'Exhibit E · Thread census',
    heading: ['Enumerate every thread,', 'find the one that crashed.'],
    body:
      'get_threads() returns every thread with its id, state, and top frame. The faulting thread is immediate; the surrounding threads — suspended, blocked, waiting — reveal the race or deadlock that led to it.',
    caption: '† thread 6 — the only one that raised the fault.',
  },
]

function VizB() {
  const frames = [
    { label: 'FrameStore::flush',            src: 'frame_store.cpp:412',   kind: 'fault' },
    { label: 'RenderPipeline::commit_layer', src: 'render_pipe.cpp:188',   kind: 'live' },
    { label: 'WorkerPool::execute_batch',    src: 'worker_job.cpp:92',     kind: 'live' },
    { label: 'WorkerThread::run',            src: 'worker_thread.cpp:41',  kind: 'live' },
    { label: '⋯ 11 more frames',             src: '',                      kind: 'muted' },
  ]
  return (
    <svg width="540" height="360" viewBox="0 0 540 360" style={{ overflow: 'visible', maxWidth: '100%' }}>
      <text x="0" y="18" fontSize="10" fontFamily="JetBrains Mono" fill="var(--bone-t3)" letterSpacing="2">
        thread 6 · stack
      </text>

      {frames.map((f, i) => {
        const y = 40 + i * 62
        const fault = f.kind === 'fault'
        const muted = f.kind === 'muted'
        const boxX = fault ? 48 : 56
        const boxW = fault ? 476 : 460
        const h = 48
        return (
          <g key={i} opacity={muted ? 0.5 : 1}>
            <text x="24" y={y + 30} fontFamily="JetBrains Mono" fontSize="11" fill="var(--bone-t3)" textAnchor="end">
              {String(i).padStart(2, '0')}
            </text>
            <rect
              x={boxX} y={y} width={boxW} height={h}
              fill={fault ? 'var(--red-soft)' : 'transparent'}
              stroke={fault ? 'var(--red)' : 'var(--line)'}
              strokeWidth="1"
            />
            <text
              x={boxX + 14} y={y + 20}
              fontFamily="JetBrains Mono" fontSize="13"
              fill={fault ? 'var(--red)' : 'var(--bone-t1)'}
              fontWeight={fault ? 600 : 400}
            >
              {f.label}
            </text>
            {f.src && (
              <text x={boxX + 14} y={y + 37} fontFamily="JetBrains Mono" fontSize="10.5" fill="var(--bone-t3)">
                {f.src}
              </text>
            )}
            {fault && (
              <text x={boxX + boxW - 16} y={y + 28} fontFamily="Switzer" fontSize="13" fill="var(--red)">†</text>
            )}
          </g>
        )
      })}
    </svg>
  )
}

function VizC() {
  // Animated packets travel between the two boxes.
  // Outbound: x=180 → x=380 (request, ink-colored).
  // Inbound:  x=380 → x=180 (live state, red).
  return (
    <svg width="560" height="280" viewBox="0 0 560 280" style={{ overflow: 'visible', maxWidth: '100%' }}>
      <text x="0" y="18" fontSize="10" fontFamily="JetBrains Mono" fill="var(--bone-t3)" letterSpacing="2">mcp · tcp</text>

      {/* Dev box */}
      <text x="100" y="66" fontFamily="JetBrains Mono" fontSize="10" fill="var(--bone-t3)" textAnchor="middle">DEV WORKSTATION</text>
      <rect x="20" y="80" width="160" height="110" fill="none" stroke="var(--line)" />
      <text x="100" y="120" fontFamily="JetBrains Mono" fontSize="13" fill="var(--bone-t1)" textAnchor="middle">AI coding agent</text>
      <text x="100" y="144" fontFamily="JetBrains Mono" fontSize="11" fill="var(--bone-t3)" textAnchor="middle">(mcp client)</text>
      <text x="100" y="176" fontFamily="JetBrains Mono" fontSize="10" fill="var(--bone-t3)" textAnchor="middle">mac · linux · win</text>

      {/* Wires */}
      <line x1="180" y1="120" x2="380" y2="120" stroke="var(--line)" strokeDasharray="3 4" />
      <line x1="380" y1="150" x2="180" y2="150" stroke="var(--red-a)" strokeDasharray="3 4" />

      {/* Labels */}
      <text x="280" y="110" fontFamily="JetBrains Mono" fontSize="10" fill="var(--bone-t2)" textAnchor="middle" letterSpacing="1.5">request</text>
      <text x="280" y="170" fontFamily="JetBrains Mono" fontSize="10" fill="var(--red)" textAnchor="middle" letterSpacing="1.5">live state</text>

      {/* Outbound animated packets */}
      <circle cx="180" cy="120" r="2.5" fill="var(--bone-t1)" className="packet pkt-out pkt-d0" />
      <circle cx="180" cy="120" r="2.5" fill="var(--bone-t1)" className="packet pkt-out pkt-d1" />
      <circle cx="180" cy="120" r="2.5" fill="var(--bone-t1)" className="packet pkt-out pkt-d2" />

      {/* Inbound animated packets */}
      <circle cx="380" cy="150" r="2.5" fill="var(--red)" className="packet pkt-in pkt-d0" />
      <circle cx="380" cy="150" r="2.5" fill="var(--red)" className="packet pkt-in pkt-d1" />
      <circle cx="380" cy="150" r="2.5" fill="var(--red)" className="packet pkt-in pkt-d2" />

      {/* Remote box */}
      <text x="460" y="66" fontFamily="JetBrains Mono" fontSize="10" fill="var(--bone-t3)" textAnchor="middle">REMOTE TEST BOX</text>
      <rect x="380" y="80" width="160" height="110" fill="none" stroke="var(--line)" />
      <text x="460" y="120" fontFamily="JetBrains Mono" fontSize="13" fill="var(--bone-t1)" textAnchor="middle">dbgsrv.exe</text>
      <text x="460" y="144" fontFamily="JetBrains Mono" fontSize="11" fill="var(--red)" textAnchor="middle">:5555</text>
      <text x="460" y="176" fontFamily="JetBrains Mono" fontSize="10" fill="var(--bone-t3)" textAnchor="middle">myapp.exe · thread 6 <tspan fill="var(--red)">†</tspan></text>
    </svg>
  )
}

function VizD() {
  return (
    <div style={{ width: '100%', maxWidth: 420, padding: '0 1rem' }}>
      <table className="locals-table">
        <thead>
          <tr>
            <th>name</th><th>type</th><th style={{ textAlign: 'right' }}>value</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td className="name-red col-name anchor">this<sup>†</sup></td>
            <td className="col-type">RenderPipeline*</td>
            <td className="val-red col-val">0x0000000000000000</td>
          </tr>
          <tr>
            <td className="col-name">chunk_count</td>
            <td className="col-type">int32_t</td>
            <td className="col-val">142</td>
          </tr>
          <tr className="faded">
            <td className="col-name">pBuffer</td>
            <td className="col-type">void*</td>
            <td className="col-val">0x000001f3a2b10000</td>
          </tr>
          <tr className="faded">
            <td className="col-name">frame_id</td>
            <td className="col-type">uint32_t</td>
            <td className="col-val">0x4b</td>
          </tr>
          <tr className="faded">
            <td className="col-name">tick</td>
            <td className="col-type">int64_t</td>
            <td className="col-val">1714829331</td>
          </tr>
        </tbody>
      </table>
    </div>
  )
}

function VizE() {
  const threads = [
    { tid: 0,  state: 'running', segs: [[90, 440]] },
    { tid: 6,  state: 'fault',   segs: [[90, 300]] },
    { tid: 12, state: 'blocked', segs: [[90, 440]] },
    { tid: 18, state: 'waiting', segs: [[140, 440]] },
    { tid: 24, state: 'running', segs: [[90, 440]] },
  ]

  return (
    <svg width="520" height="260" viewBox="0 0 520 260" style={{ overflow: 'visible', maxWidth: '100%' }}>
      <text x="0" y="16" fontSize="10" fontFamily="JetBrains Mono" fill="var(--bone-t3)" letterSpacing="2">get_threads()</text>

      {threads.map((t, i) => {
        const y = 56 + i * 34
        const fault = t.state === 'fault'
        return (
          <g key={i}>
            <text x="0" y={y + 4} fontSize="11" fontFamily="JetBrains Mono" fill="var(--bone-t2)">
              thread {String(t.tid).padStart(2, ' ')}
            </text>
            <line x1="90" y1={y} x2="440" y2={y} stroke="var(--line)" strokeDasharray="1 3" />
            {t.segs.map(([x1, x2], j) => (
              <rect
                key={j}
                x={x1}
                y={y - 4}
                width={x2 - x1}
                height="8"
                fill={fault ? 'var(--red)' : 'var(--bone-t2)'}
                opacity={fault ? 1 : 0.45}
              />
            ))}
            {fault && (
              <g>
                <line x1="300" y1={y - 12} x2="300" y2={y + 12} stroke="var(--red)" strokeWidth="1" />
                <text x="300" y={y - 16} fontSize="11" fontFamily="Switzer" fill="var(--red)" textAnchor="middle">†</text>
              </g>
            )}
            <text
              x="452"
              y={y + 4}
              fontSize="10"
              fontFamily="JetBrains Mono"
              fill={fault ? 'var(--red)' : 'var(--bone-t3)'}
              fontWeight={fault ? 600 : 400}
              letterSpacing={fault ? '0.08em' : '0'}
            >
              {fault ? 'FAULT' : t.state}
            </text>
          </g>
        )
      })}

      <line x1="90" y1="230" x2="440" y2="230" stroke="var(--line)" />
      <text x="90" y="248" fontSize="10" fontFamily="JetBrains Mono" fill="var(--bone-t3)">0µs</text>
      <text x="440" y="248" fontSize="10" fontFamily="JetBrains Mono" fill="var(--bone-t3)" textAnchor="end">840</text>
    </svg>
  )
}

const VIZ = [VizB, VizC, VizD, VizE]

export default function Narrative() {
  const [active, setActive] = useState(0)
  const blockRefs = useRef([])

  useEffect(() => {
    const els = blockRefs.current.filter(Boolean)
    if (!els.length || !('IntersectionObserver' in window)) return
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const idx = Number(entry.target.dataset.idx)
            setActive(idx)
          }
        })
      },
      { rootMargin: '-40% 0px -40% 0px', threshold: 0 },
    )
    els.forEach((el) => observer.observe(el))
    return () => observer.disconnect()
  }, [])

  const pad = (n) => String(n).padStart(2, '0')

  return (
    <section className="theme-bone h-rule narrative-section">
      <div className="layout-grid">
        <div className="gutter-col js-gutter" data-lines="auto"></div>
        <div className="narrative-column">
          <div className="narrative-head">
            <div className="eyebrow">Section II · The exhibits</div>
            <div className="narr-count">04 exhibits · scroll to review</div>
          </div>

          <div className="narrative-grid">
            <div className="narrative-left">
              {EXHIBITS.map((ex, i) => {
                const Viz = VIZ[i]
                return (
                  <article
                    key={i}
                    ref={(el) => (blockRefs.current[i] = el)}
                    data-idx={i}
                    className={`exhibit-block${active === i ? ' active' : ''}`}
                  >
                    <span className="roman-bg">{ex.roman}</span>
                    <div className="eyebrow">{ex.eyebrow}</div>
                    <h2 className="display-md">
                      {ex.heading[0]}
                      <br />
                      {ex.heading[1]}
                    </h2>
                    <p className="copy-max exhibit-body">
                      {ex.body}
                    </p>
                    <div className="viz-mobile"><Viz /></div>
                    <div className="viz-mobile-caption">{ex.caption}</div>
                  </article>
                )
              })}
            </div>

            <aside className="narrative-sticky">
              <div className="sticky-head">
                <span>Now viewing · {EXHIBITS[active].eyebrow}</span>
                <span className="idx-nav">{pad(active + 1)} / 04</span>
              </div>
              <div className="viz-stack" data-active={active}>
                {VIZ.map((Viz, i) => (
                  <div className="viz-slot" key={i} data-idx={i}>
                    <Viz />
                  </div>
                ))}
              </div>
              <div className="sticky-foot">
                <span key={active} className="footnote-caption swap">
                  {EXHIBITS[active].caption}
                </span>
                <div className="nav-dots" aria-hidden="true">
                  {EXHIBITS.map((_, i) => (
                    <span key={i} className={`dot${i === active ? ' on' : ''}`} />
                  ))}
                </div>
              </div>
            </aside>
          </div>
        </div>
      </div>
    </section>
  )
}
