export default function Footer() {
  return (
    <section className="theme-bone h-rule" style={{ paddingBottom: '3rem' }}>
      <div className="layout-grid">
        <div className="gutter-col js-gutter" data-lines="2"></div>
        <div className="footer-row">
          <div className="footer-left">
            <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
              <rect x="3" y="3" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.5" />
              <rect x="9" y="9" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.5" />
            </svg>
            <span>Stackly · MIT · Phase 2a</span>
          </div>
          <div className="footer-right">
            <a href="https://github.com/">github</a>
            <a href="https://github.com/">issues</a>
            <a href="https://github.com/">RFC / v1 plan</a>
          </div>
          <span className="footer-note">end of case file.</span>
        </div>
      </div>
    </section>
  )
}
