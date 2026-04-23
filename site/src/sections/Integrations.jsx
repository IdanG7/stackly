import { useState } from 'react'

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      try { document.execCommand('copy') } catch {}
      document.body.removeChild(ta)
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 1200)
  }
  return (
    <button
      className="copy-btn"
      type="button"
      onClick={onClick}
      data-copied={copied ? 'true' : 'false'}
    >
      {copied ? '[ copied ]' : '[ copy ]'}
    </button>
  )
}

const JSON_BLOB = `{"mcpServers": {
  "stackly": {
    "url": "http://localhost:8585/mcp"
  }
}}`

const CODEX_TOML = `[mcp_servers.stackly]
url = "http://localhost:8585/mcp"`

export default function Integrations() {
  return (
    <section className="theme-ink h-rule reveal">
      <div className="layout-grid">
        <div className="gutter-col js-gutter" data-lines="auto"></div>

        <div className="content-pad">
          <div className="fig-section-head">Fig. 1 — MCP client registrations</div>

          <div className="fig-grid">
            <div className="fig-cell">
              <div className="fig-cell-head">
                <span className="fig-cell-label">Fig. 1a — Claude Desktop</span>
                <CopyButton text={JSON_BLOB} />
              </div>
              <div className="fig-cell-file">%APPDATA%\Claude\claude_desktop_config.json</div>
              <pre>{JSON_BLOB}</pre>
            </div>

            <div className="fig-cell">
              <div className="fig-cell-head">
                <span className="fig-cell-label">Fig. 1b — Cursor</span>
                <CopyButton text={JSON_BLOB} />
              </div>
              <div className="fig-cell-file">.cursor/mcp.json</div>
              <pre>{JSON_BLOB}</pre>
            </div>

            <div className="fig-cell">
              <div className="fig-cell-head">
                <span className="fig-cell-label">Fig. 1c — Claude Code CLI</span>
                <CopyButton text={'claude mcp add stackly http://localhost:8585/mcp'} />
              </div>
              <div className="fig-cell-file">terminal</div>
              <pre>{'$ claude mcp add stackly \\\n  http://localhost:8585/mcp'}</pre>
            </div>

            <div className="fig-cell">
              <div className="fig-cell-head">
                <span className="fig-cell-label">Fig. 1d — Codex CLI</span>
                <CopyButton text={CODEX_TOML} />
              </div>
              <div className="fig-cell-file">~/.codex/config.toml</div>
              <pre>{CODEX_TOML}</pre>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
