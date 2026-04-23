function BrandMark() {
  return (
    <svg
      className="brand-mark"
      width="22"
      height="22"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <rect x="3" y="3" width="12" height="12" />
      <rect x="9" y="9" width="12" height="12" />
    </svg>
  )
}

export default function Masthead() {
  return (
    <section className="theme-bone">
      <div className="layout-grid">
        <div className="gutter-col js-gutter" data-lines="1"></div>
        <div className="masthead">
          <div className="masthead-left">
            <BrandMark />
            <span>CASE 2026-0423 / Stackly</span>
          </div>
          <div className="masthead-right">
            <span>Filed — 23 Apr 2026</span>
            <span>v0.1.0 · Phase 2a</span>
          </div>
        </div>
      </div>
    </section>
  )
}
