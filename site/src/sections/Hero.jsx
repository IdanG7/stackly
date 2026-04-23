import { useEffect, useLayoutEffect, useRef } from 'react'

function StubR() {
  return (
    <svg className="annotation-stub" viewBox="0 0 100 14" preserveAspectRatio="none">
      <path d="M 0 7 C 40 7, 52 12, 100 12" />
    </svg>
  )
}

export default function Hero() {
  const scopeRef = useRef(null)

  useLayoutEffect(() => {
    const scope = scopeRef.current
    if (!scope) return

    const update = () => {
      const anchors = scope.querySelectorAll('[data-anchor-id]')
      anchors.forEach((a) => {
        const id = a.dataset.anchorId
        const notes = scope.querySelectorAll(`[data-note-id="${id}"]`)
        if (!notes.length) return
        const anchorRect = a.getBoundingClientRect()
        notes.forEach((n) => {
          const parent = n.offsetParent
          if (!parent) return
          const parentRect = parent.getBoundingClientRect()
          const top = anchorRect.top - parentRect.top
          n.style.top = `${Math.max(0, top)}px`
        })
      })
    }

    update()
    window.addEventListener('resize', update)
    const ro = 'ResizeObserver' in window ? new ResizeObserver(update) : null
    if (ro) ro.observe(scope)
    if (document.fonts?.ready) document.fonts.ready.then(update)
    const t1 = setTimeout(update, 300)
    const t2 = setTimeout(update, 900)

    return () => {
      window.removeEventListener('resize', update)
      ro?.disconnect()
      clearTimeout(t1)
      clearTimeout(t2)
    }
  }, [])

  return (
    <section className="theme-bone h-rule reveal">
      <div className="layout-grid">
        <div className="gutter-col" aria-hidden="true"></div>

        <div className="content-3col" ref={scopeRef}>
          <div className="show-xl margin-col-l">
            <div className="annotation annotation-l" data-note-id="1">
              captured live — not parsed after the fact. <sup>1</sup>
              <StubR />
            </div>
            <div className="annotation annotation-l" data-note-id="2">
              the thread that raised the fault. <sup>2</sup>
              <StubR />
            </div>
            <div className="annotation annotation-l" data-note-id="3">
              locals captured at the faulting frame. <sup>3</sup>
              <StubR />
            </div>
          </div>

          <div className="hero-cover content-center">
            <div className="hero-case-strip">
              <span>Severity <b>P0</b></span>
              <span className="sep">/</span>
              <span>Captured <b>live</b></span>
              <span className="sep">/</span>
              <span>Case <b>2026-0423</b></span>
              <span className="spacer" />
              <span>Filed <b>23 Apr 2026</b></span>
            </div>
            <h1 className="display-xl">
              When a Windows process crashes on a remote machine,{' '}
              your AI coding assistant is <em>locked out</em>.
            </h1>

            <div className="hero-split">
              <div className="hero-evidence">
                <div className="eyebrow">Exhibit A · Crash telemetry dump</div>
                <pre className="crash-dump">
<span className="accent anchor" data-anchor-id="1">ACCESS_VIOLATION<sup>1</sup></span> (0xC0000005) reading <span className="addr-box">0x0000_0000</span>{'\n'}
MODULE:  RenderCore.dll{'\n'}
THREAD:  <span className="anchor" data-anchor-id="2">6<sup>2</sup></span> (Suspended){'\n'}
{'\n'}
TOP FRAMES:{'\n'}
  00 FrameStore::flush()            frame_store.cpp:412{'\n'}
  01 RenderPipeline::commit_layer() render_pipe.cpp:188{'\n'}
  02 WorkerPool::execute_batch()    worker_job.cpp:92{'\n'}
<span className="hidden-frames">[ — 11 more frames hidden — ]</span>{'\n'}
LAST 3 LOCALS (Frame 00):{'\n'}
  <span className="this-null anchor" data-anchor-id="3">this = nullptr<sup>3</sup></span>{'\n'}
  frame_id = 0x4b{'\n'}
  tick     = 1714829331
                </pre>
              </div>

              <div className="hero-support">
                <p className="lede">
                  Stackly attaches to the running target via Microsoft&apos;s
                  <span className="dbgsrv-inline">dbgsrv.exe</span>
                  and exposes live debugger state — exceptions, call stacks, threads,
                  and locals — to any MCP-compatible client: Claude Code, Cursor,
                  Codex, and Claude Desktop. Work against the local machine or across
                  the network, without stack-trace copy-paste.
                </p>
                <div className="cta-row">
                  <button className="stamp-cta" type="button">Attach &amp; Inspect</button>
                  <a className="link-arrow" href="#install">
                    docs/quick-start<span className="chev">→</span>
                  </a>
                </div>
              </div>
            </div>
          </div>

          <div className="show-xl" aria-hidden="true"></div>

          <ol className="footnote-list hide-xl">
            <li><sup>1</sup> captured live — not parsed after the fact.</li>
            <li><sup>2</sup> the thread that raised the fault.</li>
            <li><sup>3</sup> locals captured at the faulting frame.</li>
          </ol>
        </div>
      </div>
    </section>
  )
}
