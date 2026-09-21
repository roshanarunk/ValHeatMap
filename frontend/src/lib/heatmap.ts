/**
 * Canvas heatmap renderer.
 *
 * Density is accumulated into a Float32 grid, not into canvas pixels. That
 * matters: canvas alpha clamps at 1.0, so with ~19k kill points the hot
 * areas saturate more than 100x over and every bit of structure is
 * destroyed *before* it can be normalised -- the result is a white blob.
 * Accumulating in floats keeps the full dynamic range, and the colour ramp
 * is applied afterwards against a percentile-based ceiling.
 *
 * Pipeline:
 *   1. splat each point into a float grid with a quadratic falloff kernel
 *   2. pick a saturation ceiling from a high percentile of non-empty cells
 *      (not the max -- one freak hotspot would flatten everything else)
 *   3. apply a perceptual curve, then the colour ramp, into ImageData
 */

import type { Vec2 } from './types'

export type RampName = 'inferno' | 'ice' | 'toxic' | 'duel'

/** Control stops as [position, r, g, b]. Interpolated into a 256-entry LUT. */
const RAMPS: Record<RampName, [number, number, number, number][]> = {
  // Cool -> hot, matching the convention players expect from map overlays:
  // sparse activity reads blue/violet, genuine hotspots read orange/white.
  inferno: [
    [0.0, 38, 24, 120],
    [0.18, 74, 42, 168],
    [0.38, 150, 44, 148],
    [0.58, 214, 68, 96],
    [0.76, 246, 132, 44],
    [0.9, 252, 198, 84],
    [1.0, 255, 248, 220],
  ],
  ice: [
    [0.0, 16, 32, 92],
    [0.25, 22, 84, 158],
    [0.5, 32, 150, 196],
    [0.72, 90, 206, 222],
    [0.88, 164, 232, 238],
    [1.0, 240, 253, 255],
  ],
  toxic: [
    [0.0, 18, 54, 40],
    [0.28, 26, 104, 68],
    [0.55, 74, 168, 70],
    [0.78, 158, 214, 66],
    [0.92, 214, 240, 96],
    [1.0, 247, 255, 214],
  ],
  // Single-hue red, for the "deaths" view.
  duel: [
    [0.0, 52, 16, 40],
    [0.28, 116, 24, 58],
    [0.55, 186, 42, 62],
    [0.78, 232, 88, 70],
    [0.92, 248, 150, 120],
    [1.0, 255, 232, 220],
  ],
}

const LUT_CACHE = new Map<RampName, Uint8ClampedArray>()

function buildLut(name: RampName): Uint8ClampedArray {
  const cached = LUT_CACHE.get(name)
  if (cached) return cached
  const stops = RAMPS[name]
  const lut = new Uint8ClampedArray(256 * 3)
  for (let i = 0; i < 256; i++) {
    const pos = i / 255
    let lo = stops[0]
    let hi = stops[stops.length - 1]
    for (let s = 0; s < stops.length - 1; s++) {
      if (pos >= stops[s][0] && pos <= stops[s + 1][0]) {
        lo = stops[s]
        hi = stops[s + 1]
        break
      }
    }
    const span = hi[0] - lo[0]
    const f = span === 0 ? 0 : (pos - lo[0]) / span
    lut[i * 3] = lo[1] + (hi[1] - lo[1]) * f
    lut[i * 3 + 1] = lo[2] + (hi[2] - lo[2]) * f
    lut[i * 3 + 2] = lo[3] + (hi[3] - lo[3]) * f
  }
  LUT_CACHE.set(name, lut)
  return lut
}

export interface HeatmapOptions {
  /** Points in normalised [0,1] map space. */
  points: Vec2[]
  /** Splat radius in device pixels. */
  radius: number
  /** Overall opacity of the rendered field, 0..1. */
  intensity: number
  ramp: RampName
  /**
   * Fraction of the density range treated as "full heat". Lower values make
   * more of the map read as hot. The ceiling is taken from this percentile
   * of non-empty cells so a single freak hotspot cannot flatten the rest.
   */
  percentile?: number
  /** Hide the faintest cells so isolated one-off kills don't fog the map. */
  floor?: number
}

/** Reusable buffers, keyed by size, so panning/resizing doesn't reallocate. */
let densityBuf: Float32Array | null = null
let densityLen = 0
let imageCanvas: HTMLCanvasElement | null = null

function getDensity(len: number): Float32Array {
  if (!densityBuf || densityLen !== len) {
    densityBuf = new Float32Array(len)
    densityLen = len
  } else {
    densityBuf.fill(0)
  }
  return densityBuf
}

/**
 * Precomputed radial kernel: weight by squared distance, so a splat falls
 * off smoothly to zero at its edge. Cached per radius.
 */
let kernel: Float32Array | null = null
let kernelRadius = -1

function getKernel(r: number): Float32Array {
  if (kernel && kernelRadius === r) return kernel
  const size = r * 2 + 1
  const k = new Float32Array(size * size)
  const r2 = r * r
  for (let dy = -r; dy <= r; dy++) {
    for (let dx = -r; dx <= r; dx++) {
      const d2 = dx * dx + dy * dy
      if (d2 > r2) continue
      // (1 - (d/r)^2)^2 -- a smooth bump, cheaper than a true gaussian and
      // visually indistinguishable once summed over many points.
      const t = 1 - d2 / r2
      k[(dy + r) * size + (dx + r)] = t * t
    }
  }
  kernel = k
  kernelRadius = r
  return k
}

export function renderHeatmap(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  opts: HeatmapOptions,
): void {
  const { points, intensity, ramp } = opts
  if (width <= 0 || height <= 0 || points.length === 0) return

  // Accumulate at reduced resolution: the field is smooth by construction,
  // so a half-scale grid is visually identical and ~4x faster to fill.
  const scaleDown = 2
  const gw = Math.max(1, Math.ceil(width / scaleDown))
  const gh = Math.max(1, Math.ceil(height / scaleDown))
  const radius = Math.max(1, Math.round(opts.radius / scaleDown))

  const density = getDensity(gw * gh)
  const k = getKernel(radius)
  const ksize = radius * 2 + 1

  // --- pass 1: accumulate ------------------------------------------------
  for (let i = 0; i < points.length; i++) {
    const px = Math.round(points[i].x * gw)
    const py = Math.round(points[i].y * gh)
    if (px < -radius || py < -radius || px > gw + radius || py > gh + radius) continue

    const x0 = Math.max(0, px - radius)
    const x1 = Math.min(gw - 1, px + radius)
    const y0 = Math.max(0, py - radius)
    const y1 = Math.min(gh - 1, py + radius)

    for (let y = y0; y <= y1; y++) {
      const krow = (y - py + radius) * ksize
      const drow = y * gw
      for (let x = x0; x <= x1; x++) {
        const w = k[krow + (x - px + radius)]
        if (w > 0) density[drow + x] += w
      }
    }
  }

  // --- pass 2: choose a ceiling -----------------------------------------
  // Sampling a high percentile of *non-empty* cells keeps the scale tied to
  // typical hotspot density rather than to the single hottest pixel.
  const percentile = opts.percentile ?? 0.995
  let ceiling = 0
  {
    const sample: number[] = []
    // Stride-sample for speed; the field is smooth so this is representative.
    const stride = Math.max(1, Math.floor(density.length / 40000))
    for (let i = 0; i < density.length; i += stride) {
      if (density[i] > 0) sample.push(density[i])
    }
    if (sample.length === 0) return
    sample.sort((a, b) => a - b)
    ceiling = sample[Math.min(sample.length - 1, Math.floor(sample.length * percentile))]
    // Guard against a degenerate field where everything is equal.
    if (!(ceiling > 0)) ceiling = sample[sample.length - 1] || 1
  }

  // --- pass 3: colourise -------------------------------------------------
  if (!imageCanvas) imageCanvas = document.createElement('canvas')
  if (imageCanvas.width !== gw || imageCanvas.height !== gh) {
    imageCanvas.width = gw
    imageCanvas.height = gh
  }
  const ictx = imageCanvas.getContext('2d')
  if (!ictx) return
  const img = ictx.createImageData(gw, gh)
  const data = img.data
  const lut = buildLut(ramp)
  const floor = opts.floor ?? 0.04

  for (let i = 0; i < density.length; i++) {
    const raw = density[i]
    if (raw <= 0) continue
    let t = raw / ceiling
    if (t > 1) t = 1
    if (t < floor) continue

    // Perceptual curve: sqrt lifts the mid-range so moderately busy areas
    // stay visible instead of collapsing into the background.
    const shaped = Math.sqrt(t)
    const idx = (shaped * 255) | 0
    const o = idx * 3
    const p = i * 4
    data[p] = lut[o]
    data[p + 1] = lut[o + 1]
    data[p + 2] = lut[o + 2]
    // Fade in the low end so the field dissolves into the map rather than
    // ending on a hard edge, and never fully hide the base map.
    const alpha = Math.min(1, shaped * 1.35) * intensity
    data[p + 3] = (alpha * 235) | 0
  }
  ictx.putImageData(img, 0, 0)

  // Scale the reduced-resolution field back up; smoothing hides the grid.
  ctx.save()
  ctx.imageSmoothingEnabled = true
  ctx.imageSmoothingQuality = 'high'
  ctx.drawImage(imageCanvas, 0, 0, gw, gh, 0, 0, width, height)
  ctx.restore()
}

/** Discrete point rendering, used for the "duel"/scatter views. */
export interface DotStyle {
  fill: string
  stroke?: string
  radius: number
  alpha?: number
}

export function drawDot(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  style: DotStyle,
): void {
  ctx.save()
  ctx.globalAlpha = style.alpha ?? 1
  ctx.beginPath()
  ctx.arc(x, y, style.radius, 0, Math.PI * 2)
  ctx.fillStyle = style.fill
  ctx.fill()
  if (style.stroke) {
    ctx.lineWidth = Math.max(1, style.radius * 0.35)
    ctx.strokeStyle = style.stroke
    ctx.stroke()
  }
  ctx.restore()
}

export function drawDuelLine(
  ctx: CanvasRenderingContext2D,
  from: { x: number; y: number },
  to: { x: number; y: number },
  color: string,
  alpha = 0.5,
): void {
  ctx.save()
  ctx.globalAlpha = alpha
  ctx.strokeStyle = color
  ctx.lineWidth = 1.25
  ctx.beginPath()
  ctx.moveTo(from.x, from.y)
  ctx.lineTo(to.x, to.y)
  ctx.stroke()
  ctx.restore()
}

/** Blue -> red diverging scale for win-rate visuals. */
export function winRateColor(rate: number, alpha = 1): string {
  const t = Math.max(0, Math.min(1, rate))
  // 0.5 is neutral slate; below trends blue, above trends green.
  const r = t < 0.5 ? 86 + (150 - 86) * (t / 0.5) : 150 - (150 - 46) * ((t - 0.5) / 0.5)
  const g = t < 0.5 ? 122 + (160 - 122) * (t / 0.5) : 160 + (222 - 160) * ((t - 0.5) / 0.5)
  const b = t < 0.5 ? 220 - (220 - 150) * (t / 0.5) : 150 - (150 - 120) * ((t - 0.5) / 0.5)
  return `rgba(${r | 0}, ${g | 0}, ${b | 0}, ${alpha})`
}

/** Sample the ramp for UI legends, as CSS colour stops. */
export function rampStops(name: RampName, steps = 12): string[] {
  const lut = buildLut(name)
  const out: string[] = []
  for (let i = 0; i < steps; i++) {
    const idx = Math.round((i / (steps - 1)) * 255) * 3
    out.push(`rgb(${lut[idx]}, ${lut[idx + 1]}, ${lut[idx + 2]})`)
  }
  return out
}
