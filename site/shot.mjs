import puppeteer from 'puppeteer'

const URL = process.env.URL || 'http://localhost:5174'
const W = 1440
const H = 900

const landmarks = [
  { name: '01-hero',         scroll: 0 },
  { name: '02-hero-bottom',  scroll: 600 },
  { name: '03-integrations', selector: '.fig-grid', offset: 120 },
  { name: '04-exhibit-b',    selector: '[data-idx="0"]', offset: 80 },
  { name: '05-exhibit-c',    selector: '[data-idx="1"]', offset: 80 },
  { name: '06-exhibit-d',    selector: '[data-idx="2"]', offset: 80 },
  { name: '07-exhibit-e',    selector: '[data-idx="3"]', offset: 80 },
  { name: '08-ledger-opener',selector: '.ledger-opener', offset: 40 },
  { name: '09-ledger-head',  selector: '.ledger-head', offset: 60 },
  { name: '10-ledger-open',  selector: '#tools',      offset: 80, clickNth: 2 },
  { name: '11-closer',       selector: '#install',    offset: 40 },
  { name: '12-footer',       scrollToBottom: true },
]

const browser = await puppeteer.launch({ headless: 'new' })
const page = await browser.newPage()
await page.setViewport({ width: W, height: H, deviceScaleFactor: 1 })
page.on('console', (msg) => { if (msg.type() === 'error') console.log('[page err]', msg.text()) })
page.on('pageerror', (e) => console.log('[page err]', e.message))

console.log('loading', URL)
await page.goto(URL, { waitUntil: 'networkidle2', timeout: 60000 })
await new Promise(r => setTimeout(r, 2500))

for (const lm of landmarks) {
  if (lm.scrollToBottom) {
    await page.evaluate(() => window.scrollTo({ top: document.body.scrollHeight }))
  } else if (lm.selector) {
    await page.evaluate((sel, off) => {
      const el = document.querySelector(sel)
      if (el) {
        const top = el.getBoundingClientRect().top + window.scrollY
        window.scrollTo({ top: top - (off || 0) })
      }
    }, lm.selector, lm.offset || 0)
  } else {
    await page.evaluate(y => window.scrollTo({ top: y }), lm.scroll || 0)
  }
  await new Promise(r => setTimeout(r, 900))

  if (lm.clickNth != null) {
    await page.evaluate((n) => {
      const btns = document.querySelectorAll('.ledger-item-btn')
      if (btns[n]) btns[n].click()
    }, lm.clickNth)
    await new Promise(r => setTimeout(r, 700))
  }

  await page.screenshot({ path: `shots/${lm.name}.png` })
  console.log('shot', lm.name)
}

await browser.close()
console.log('done')
