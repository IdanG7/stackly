import useForensic from './hooks/useForensic.js'

import CropMarks from './sections/CropMarks.jsx'
import PageGutter from './sections/PageGutter.jsx'
import Masthead from './sections/Masthead.jsx'
import Hero from './sections/Hero.jsx'
import Integrations from './sections/Integrations.jsx'
import Narrative from './sections/Narrative.jsx'
import Tools from './sections/Tools.jsx'
import Closer from './sections/Closer.jsx'
import Footer from './sections/Footer.jsx'

export default function App() {
  useForensic()

  return (
    <>
      <div className="grain-overlay" aria-hidden="true" />
      <main className="frame">
        <PageGutter />
        <CropMarks />
        <Masthead />
        <Hero />
        <Integrations />
        <Narrative />
        <Tools />
        <Closer />
        <Footer />
      </main>
    </>
  )
}
