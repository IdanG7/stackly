import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import './styles.css'
import Lenis from 'lenis'
import 'lenis/dist/lenis.css'

const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches

if (!prefersReducedMotion) {
  const lenis = new Lenis({
    lerp: 0.09,
    duration: 1.1,
    easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
    smoothWheel: true,
    syncTouch: false,
    wheelMultiplier: 1,
    touchMultiplier: 1.6,
  })
  const raf = (time) => {
    lenis.raf(time)
    requestAnimationFrame(raf)
  }
  requestAnimationFrame(raf)
  window.__lenis = lenis
}

// Smooth scroll for anchor links (overrides default jump)
document.addEventListener('click', (e) => {
  const link = e.target.closest?.('a[href^="#"]')
  if (!link) return
  const hash = link.getAttribute('href')
  if (hash === '#' || hash.length < 2) return
  const el = document.querySelector(hash)
  if (!el) return
  e.preventDefault()
  if (window.__lenis) window.__lenis.scrollTo(el, { offset: -80, duration: 1.2 })
  else el.scrollIntoView({ behavior: 'smooth', block: 'start' })
})

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>
)
