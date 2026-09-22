/**
 * Verify the diverging heatmap's colours without a browser.
 *
 * The renderer only needs createImageData/putImageData/drawImage from the
 * 2D context, so a stub captures the pixels it writes and we assert on
 * those directly.
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const SIZE = 200

// --- a canvas stub that records what the renderer draws ------------------
let captured = null

class StubImageData {
  constructor(w, h) {
    this.width = w
    this.height = h
    this.data = new Uint8ClampedArray(w * h * 4)
  }
}

const stubCtx = {
  createImageData: (w, h) => new StubImageData(w, h),
  putImageData: (img) => {
    captured = img
  },
  drawImage: () => {},
  save: () => {},
  restore: () => {},
  imageSmoothingEnabled: true,
  imageSmoothingQuality: 'high',
}

globalThis.document = {
  createElement: () => ({
    width: 0,
    height: 0,
    getContext: () => stubCtx,
  }),
}

// --- compile the module with the real TypeScript compiler ---------------
// Regex-stripping types is fragile (object type literals look like code);
// tsc is already a dependency here, so use it.
const { execFileSync } = await import('node:child_process')
const dir = mkdtempSync(join(tmpdir(), 'vhm-'))
const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
// Run tsc's JS entry point with this node, rather than going through the
// npx shim, which does not spawn cleanly on Windows.
execFileSync(
  process.execPath,
  [`${FRONTEND}/node_modules/typescript/lib/tsc.js`,
   `${FRONTEND}/src/lib/heatmap.ts`, `${FRONTEND}/src/lib/types.ts`,
   '--outDir', dir, '--module', 'esnext', '--target', 'es2020',
   '--moduleResolution', 'bundler', '--skipLibCheck'],
  { cwd: FRONTEND, stdio: 'inherit' },
)
const { pathToFileURL } = await import('node:url')
const jsPath = join(dir, 'heatmap.js')
const { renderDivergingHeatmap, divergingStops } = await import(
  pathToFileURL(jsPath).href,
)

// --- helpers -------------------------------------------------------------
const cluster = (x, y, n) => Array.from({ length: n }, () => ({ x, y }))

function render(wins, losses) {
  captured = null
  renderDivergingHeatmap(stubCtx, SIZE, SIZE, {
    wins,
    losses,
    radius: 24,
    intensity: 1,
  })
  const img = captured
  // The field is accumulated at half resolution.
  const gw = Math.ceil(SIZE / 2)
  return (x, y) => {
    if (!img) return { r: 0, g: 0, b: 0, a: 0 }
    const px = Math.round(x * gw)
    const py = Math.round(y * Math.ceil(SIZE / 2))
    const i = (py * gw + px) * 4
    return { r: img.data[i], g: img.data[i + 1], b: img.data[i + 2], a: img.data[i + 3] }
  }
}

const failures = []
const check = (name, fn) => {
  try {
    fn()
    console.log(`  PASS  ${name}`)
  } catch (err) {
    failures.push(`${name}: ${err.message}`)
    console.log(`  FAIL  ${name}\n        ${err.message}`)
  }
}
const assert = (cond, msg) => {
  if (!cond) throw new Error(msg)
}

console.log('diverging heatmap colours\n')

check('kill-dominated spot is green, death-dominated is red', () => {
  const at = render(
    [...cluster(0.25, 0.25, 20), ...cluster(0.75, 0.75, 2)],
    [...cluster(0.25, 0.25, 2), ...cluster(0.75, 0.75, 20)],
  )
  const win = at(0.25, 0.25)
  const loss = at(0.75, 0.75)
  assert(win.g > win.r, `winning spot should be green, got rgb(${win.r},${win.g},${win.b})`)
  assert(loss.r > loss.g, `losing spot should be red, got rgb(${loss.r},${loss.g},${loss.b})`)
})

check('evenly contested spot sits mid-gradient', () => {
  // The midpoint is amber -- on the ramp between the two ends, not a
  // third colour. A grey midpoint made the field look like separate red
  // and green layers rather than one scale.
  const at = render(cluster(0.5, 0.5, 10), cluster(0.5, 0.5, 10))
  const even = at(0.5, 0.5)
  const mid = divergingStops(3)[1].match(/\d+/g).map(Number)
  const dist = Math.hypot(even.r - mid[0], even.g - mid[1], even.b - mid[2])
  assert(
    dist < 40,
    `10 kills vs 10 deaths should match the legend midpoint rgb(${mid}), ` +
      `got rgb(${even.r},${even.g},${even.b})`,
  )
  assert(even.a > 0, 'a busy contested spot should still be drawn')
})

check('colour moves monotonically from losing to winning', () => {
  // The scale has to be readable as an ordering: more wins at a spot must
  // never make it look *more* like a loss.
  const ratios = [0, 0.25, 0.5, 0.75, 1]
  const greenness = ratios.map((share) => {
    const wins = Math.round(20 * share)
    const losses = 20 - wins
    const at = render(
      wins ? cluster(0.5, 0.5, wins) : [{ x: 0.9, y: 0.9 }],
      losses ? cluster(0.5, 0.5, losses) : [{ x: 0.1, y: 0.1 }],
    )
    const p = at(0.5, 0.5)
    return p.g - p.r
  })
  for (let i = 1; i < greenness.length; i++) {
    assert(
      greenness[i] >= greenness[i - 1],
      `win share ${ratios[i]} should be at least as green as ${ratios[i - 1]} ` +
        `(g-r: ${greenness.join(', ')})`,
    )
  }
})

check('one shared ceiling, so imbalance is not normalised away', () => {
  const at = render(
    [...cluster(0.3, 0.3, 40), ...cluster(0.7, 0.7, 3)],
    [...cluster(0.3, 0.3, 1), ...cluster(0.7, 0.7, 2)],
  )
  const lopsided = at(0.3, 0.3)
  const mild = at(0.7, 0.7)
  assert(
    lopsided.g - lopsided.r > mild.g - mild.r,
    `40-vs-1 should be greener than 3-vs-2 (${lopsided.g - lopsided.r} vs ${mild.g - mild.r})`,
  )
})

check('empty areas stay transparent', () => {
  const at = render(cluster(0.2, 0.2, 10), cluster(0.2, 0.2, 4))
  assert(at(0.9, 0.9).a === 0, 'far corner should not be painted')
})

check('no points renders nothing', () => {
  const at = render([], [])
  assert(at(0.5, 0.5).a === 0, 'empty input should draw nothing')
})

check('one-sided field does not divide by zero', () => {
  const at = render(cluster(0.5, 0.5, 10), [])
  const p = at(0.5, 0.5)
  assert(p.a > 0, 'kills-only field should still render')
  assert(p.g > p.r, 'kills-only field should be green')
})

check('legend runs red -> neutral -> green', () => {
  const stops = divergingStops(11)
  assert(stops.length === 11, `expected 11 stops, got ${stops.length}`)
  const rgb = (s) => s.match(/\d+/g).map(Number)
  const [lowR, lowG] = rgb(stops[0])
  const [highR, highG] = rgb(stops[stops.length - 1])
  assert(lowR > lowG, `low end should be red, got ${stops[0]}`)
  assert(highG > highR, `high end should be green, got ${stops[stops.length - 1]}`)
})

console.log()
if (failures.length) {
  console.log(`FAILED (${failures.length})`)
  process.exit(1)
}
console.log('all checks passed')
