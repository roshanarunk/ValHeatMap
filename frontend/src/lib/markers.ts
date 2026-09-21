/**
 * Map markers: plant-site badges and spot pins.
 *
 * The previous design drew one large translucent circle per plant cluster,
 * sized by sample. At dataset scale those circles overlap into an
 * unreadable mess. This replaces them with compact badges -- a site letter
 * for the site itself, small numbered pins for the common plant spots
 * within it -- which stay legible however many plants sit underneath.
 */

export interface MarkerStyle {
  /** Device-pixel scale, so markers stay a constant on-screen size. */
  dpr: number
  /** Rotation applied to the map, so labels can be kept upright. */
  rotation?: number
}

const FONT = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif'

function roundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  ctx.beginPath()
  ctx.moveTo(x + r, y)
  ctx.arcTo(x + w, y, x + w, y + h, r)
  ctx.arcTo(x + w, y + h, x, y + h, r)
  ctx.arcTo(x, y + h, x, y, r)
  ctx.arcTo(x, y, x + w, y, r)
  ctx.closePath()
}

/**
 * Draw a label upright regardless of map rotation. Without this, rotating
 * the map to a team's perspective would leave every label upside down.
 */
function upright(
  ctx: CanvasRenderingContext2D,
  cx: number,
  cy: number,
  rotation: number,
  draw: () => void,
): void {
  ctx.save()
  ctx.translate(cx, cy)
  if (rotation) ctx.rotate(-rotation)
  draw()
  ctx.restore()
}

/** The big square badge naming a site: "A", "B", "C". */
export function drawSiteBadge(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  site: string,
  style: MarkerStyle,
): void {
  const { dpr, rotation = 0 } = style
  const size = 26 * dpr
  upright(ctx, x, y, rotation, () => {
    ctx.shadowColor = 'rgba(0,0,0,0.55)'
    ctx.shadowBlur = 8 * dpr
    ctx.shadowOffsetY = 1.5 * dpr
    roundRect(ctx, -size / 2, -size / 2, size, size, 6 * dpr)
    ctx.fillStyle = 'rgba(12, 17, 28, 0.92)'
    ctx.fill()
    ctx.shadowColor = 'transparent'
    ctx.lineWidth = 1.5 * dpr
    ctx.strokeStyle = 'rgba(255,255,255,0.22)'
    ctx.stroke()

    ctx.font = `700 ${15 * dpr}px ${FONT}`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillStyle = '#ffffff'
    ctx.fillText(site.toUpperCase(), 0, 0.5 * dpr)
  })
}

export interface SpotPin {
  x: number
  y: number
  index: number
  winRate: number
  plants: number
  reliable: boolean
}

/**
 * A numbered pin for one plant spot. Colour carries the win rate, the
 * number keys it to the list beside the map, and the size stays constant
 * so dense clusters remain readable.
 */
export function drawSpotPin(
  ctx: CanvasRenderingContext2D,
  pin: SpotPin,
  size: number,
  style: MarkerStyle,
): void {
  const { dpr, rotation = 0 } = style
  const cx = pin.x * size
  const cy = pin.y * size
  const r = 11 * dpr

  const [fill, text] = winRateColors(pin.winRate, pin.reliable)

  upright(ctx, cx, cy, rotation, () => {
    ctx.shadowColor = 'rgba(0,0,0,0.5)'
    ctx.shadowBlur = 6 * dpr
    ctx.shadowOffsetY = 1 * dpr
    roundRect(ctx, -r, -r, r * 2, r * 2, 5 * dpr)
    ctx.fillStyle = fill
    ctx.fill()
    ctx.shadowColor = 'transparent'
    ctx.lineWidth = 1.25 * dpr
    ctx.strokeStyle = pin.reliable ? 'rgba(255,255,255,0.5)' : 'rgba(255,255,255,0.22)'
    if (!pin.reliable) ctx.setLineDash([3 * dpr, 2 * dpr])
    ctx.stroke()
    ctx.setLineDash([])

    ctx.font = `700 ${11 * dpr}px ${FONT}`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillStyle = text
    ctx.fillText(String(pin.index), 0, 0.5 * dpr)
  })
}

/**
 * Win-rate colour: red below 50%, green above, neutral slate at even. The
 * scale is deliberately blunt -- the number is on the pin, the colour is
 * only there to let you scan for outliers.
 */
export function winRateColors(rate: number, reliable = true): [string, string] {
  const t = Math.max(0, Math.min(1, rate))
  const alpha = reliable ? 0.95 : 0.55
  let r: number
  let g: number
  let b: number
  if (t < 0.5) {
    const k = t / 0.5
    r = 198 - 40 * k
    g = 58 + 70 * k
    b = 70 + 40 * k
  } else {
    const k = (t - 0.5) / 0.5
    r = 158 - 112 * k
    g = 128 + 90 * k
    b = 110 + 20 * k
  }
  return [`rgba(${r | 0}, ${g | 0}, ${b | 0}, ${alpha})`, '#ffffff']
}

/** Same scale as a CSS colour, for list rows beside the map. */
export function winRateCss(rate: number, reliable = true): string {
  return winRateColors(rate, reliable)[0]
}
