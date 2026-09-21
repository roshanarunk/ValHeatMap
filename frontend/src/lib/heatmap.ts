/**
 * Canvas heatmap renderer.
 *
 * Density is accumulated into an offscreen alpha field using radial
 * gradient splats, then the field is colourised through a lookup ramp in a
 * single pass over the pixels. This is the standard two-pass approach: it
 * keeps per-point cost to one gradient fill and makes the colour ramp
 * independent of point count, so 10k kills render as fast as 100.
 */

import type { Vec2 } from './types'

export type RampName = 'inferno' | 'ice' | 'toxic' | 'duel'

/** Control stops as [position, r, g, b]. Interpolated into a 256-entry LUT. */
const RAMPS: Record<RampName, [number, number, number, number][]> = {
  // Classic heat: transparent -> violet -> magenta -> amber -> white.
  inferno: [
    [0.0, 12, 8, 38],
    [0.25, 88, 24, 108],
    [0.5, 186, 42, 96],
    [0.72, 242, 118, 40],
    [0.9, 252, 206, 96],
    [1.0, 255, 250, 224],
  ],
  ice: [
    [0.0, 6, 18, 44],
    [0.3, 16, 78, 130],
    [0.6, 34, 160, 190],
    [0.85, 122, 226, 231],
    [1.0, 236, 253, 255],
  ],
  toxic: [
    [0.0, 10, 30, 18],
    [0.35, 22, 96, 60],
    [0.65, 96, 178, 54],
    [0.88, 196, 232, 74],
    [1.0, 247, 255, 214],
  ],
  // Single-hue Valorant red, for the "deaths" view.
  duel: [
    [0.0, 26, 10, 18],
    [0.35, 110, 20, 48],
    [0.65, 198, 40, 62],
    [0.88, 248, 110, 96],
    [1.0, 255, 226, 214],
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
  /** Peak opacity of the rendered field, 0..1. */
  intensity: number
  ramp: RampName
  /**
   * Density at which the ramp saturates. `auto` derives it from the
   * observed peak so sparse selections stay readable; a fixed number keeps
   * colours comparable across different filters.
   */
  saturation?: number | 'auto'
}

/** Scratch canvas for the density field, reused across renders. */
let scratch: HTMLCanvasElement | null = null

function getScratch(w: number, h: number): HTMLCanvasElement {
  if (!scratch) scratch = document.createElement('canvas')
  if (scratch.width !== w || scratch.height !== h) {
    scratch.width = w
    scratch.height = h
  }
  return scratch
}

export function renderHeatmap(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  opts: HeatmapOptions,
): void {
  const { points, radius, intensity, ramp } = opts
  if (width <= 0 || height <= 0) return
  if (points.length === 0) return

  const field = getScratch(width, height)
  const fctx = field.getContext('2d', { willReadFrequently: true })
  if (!fctx) return
  fctx.clearRect(0, 0, width, height)

  // Pass 1: accumulate density. Each point contributes a radial falloff;
  // overlapping splats sum via 'lighter' to build the density field.
  fctx.globalCompositeOperation = 'lighter'
  // Per-splat alpha is low so that density, not a single point, drives
  // saturation. Scaled up slightly for sparse sets so they stay visible.
  const splatAlpha = points.length > 400 ? 0.28 : points.length > 120 ? 0.42 : 0.6
  for (const p of points) {
    const cx = p.x * width
    const cy = p.y * height
    if (cx < -radius || cy < -radius || cx > width + radius || cy > height + radius) continue
    const g = fctx.createRadialGradient(cx, cy, 0, cx, cy, radius)
    g.addColorStop(0, `rgba(255,255,255,${splatAlpha})`)
    g.addColorStop(0.45, `rgba(255,255,255,${splatAlpha * 0.5})`)
    g.addColorStop(1, 'rgba(255,255,255,0)')
    fctx.fillStyle = g
    fctx.beginPath()
    fctx.arc(cx, cy, radius, 0, Math.PI * 2)
    fctx.fill()
  }
  fctx.globalCompositeOperation = 'source-over'

  // Pass 2: colourise the field through the ramp.
  const img = fctx.getImageData(0, 0, width, height)
  const data = img.data
  const lut = buildLut(ramp)

  let peak = 0
  if (opts.saturation === 'auto' || opts.saturation === undefined) {
    for (let i = 3; i < data.length; i += 4) if (data[i] > peak) peak = data[i]
  } else {
    peak = Math.max(1, Math.min(255, opts.saturation * 255))
  }
  if (peak <= 0) return
  const scale = 255 / peak

  for (let i = 0; i < data.length; i += 4) {
    const a = data[i + 3]
    if (a === 0) continue
    const v = Math.min(255, a * scale) | 0
    const o = v * 3
    data[i] = lut[o]
    data[i + 1] = lut[o + 1]
    data[i + 2] = lut[o + 2]
    // Fade the low end so the field dissolves into the map rather than
    // ending in a hard edge.
    data[i + 3] = Math.min(255, v * intensity * 1.6)
  }
  fctx.putImageData(img, 0, 0)
  ctx.drawImage(field, 0, 0)
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
