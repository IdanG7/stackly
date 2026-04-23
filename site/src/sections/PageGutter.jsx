import { useEffect, useRef, useState } from 'react'

const LINE_HEIGHT_PX = 24

export default function PageGutter() {
  const ref = useRef(null)
  const [lines, setLines] = useState(1)

  useEffect(() => {
    const measure = () => {
      const frame = document.querySelector('.frame')
      if (!frame) return
      const h = frame.getBoundingClientRect().height
      setLines(Math.max(1, Math.floor(h / LINE_HEIGHT_PX)))
    }

    measure()

    const frame = document.querySelector('.frame')
    let ro
    if (frame && 'ResizeObserver' in window) {
      ro = new ResizeObserver(measure)
      ro.observe(frame)
    }
    window.addEventListener('resize', measure)
    if (document.fonts?.ready) document.fonts.ready.then(measure)
    const timer = setTimeout(measure, 800)

    return () => {
      ro?.disconnect()
      window.removeEventListener('resize', measure)
      clearTimeout(timer)
    }
  }, [])

  return (
    <div className="page-gutter" aria-hidden="true" ref={ref}>
      <div className="page-gutter-stack">
        {Array.from({ length: lines }, (_, i) => (
          <span key={i}>{String(i + 1).padStart(3, '0')}</span>
        ))}
      </div>
    </div>
  )
}
