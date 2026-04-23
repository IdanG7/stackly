export default function Closer() {
  return (
    <section className="theme-ink h-rule reveal" id="install">
      <div className="layout-grid">
        <div className="gutter-col" aria-hidden="true"></div>
        <div className="content-3col">
          <div className="show-xl" aria-hidden="true"></div>

          <div className="closer-spread">
            <div className="closer-left">
              <h2 className="closer-display">
                Attach.<br />Read the crash.<br />Ship the <em>fix</em>.
              </h2>
              <p className="closer-lede">
                Connect your AI coding assistant to the live Windows debugger.
                Inspect any running or crashed process — local or remote — without
                stack-trace copy-paste.
              </p>
            </div>

            <div className="closer-right">
              <div className="closer-divider"></div>
              <pre className="closer-shell">
<span className="prompt">$ </span>uv pip install stackly{'\n'}
<span className="prompt">$ </span>stackly doctor       <span className="dim"># verify toolchain</span>{'\n'}
<span className="prompt">$ </span>stackly serve --port 8585
              </pre>
              <p className="closer-prereq">
                Requires Windows Debugging Tools (part of the Windows SDK).
              </p>
            </div>
          </div>

          <div className="show-xl" aria-hidden="true"></div>
        </div>
      </div>
    </section>
  )
}
