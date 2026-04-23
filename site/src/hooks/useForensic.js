import { useEffect } from 'react'

function wireReveals() {
  const els = document.querySelectorAll('.reveal')
  if (!('IntersectionObserver' in window)) {
    els.forEach((el) => el.classList.add('visible'))
    return () => {}
  }
  const obs = new IntersectionObserver(
    (entries, observer) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible')
          observer.unobserve(entry.target)
        }
      })
    },
    { rootMargin: '0px 0px -10% 0px', threshold: 0.1 },
  )
  els.forEach((el) => obs.observe(el))
  return () => obs.disconnect()
}

export default function useForensic() {
  useEffect(() => wireReveals(), [])
}
