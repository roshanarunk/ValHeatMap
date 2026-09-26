import type { RotationTransition, RotationZone } from './types'

export interface DrawRotationsOptions {
  selectedZone?: string | null
  selectedRoute?: { from: string; to: string } | null
  side?: 'defense' | 'attack' | 'all'
  onNeedRedraw?: () => void
}

const agentImageCache = new Map<string, HTMLImageElement>()

export function getAgentImage(url: string, onLoaded?: () => void): HTMLImageElement | null {
  if (!url) return null
  let img = agentImageCache.get(url)
  if (!img) {
    img = new Image()
    img.crossOrigin = 'anonymous'
    img.src = url
    if (onLoaded) {
      img.onload = () => onLoaded()
    }
    agentImageCache.set(url, img)
  }
  return img.complete && img.naturalWidth > 0 ? img : null
}

/** Get accent color based on zone name/role. */
export function getZoneColor(zoneName: string): string {
  const z = zoneName.toUpperCase()
  if (z.startsWith('A')) return '#38bdf8' // Cyan / Sky
  if (z.startsWith('B')) return '#c084fc' // Purple / Violet
  if (z.startsWith('C')) return '#34d399' // Emerald
  if (z.includes('CT') || z.includes('DEFENDER')) return '#2dd4bf' // Teal
  if (z.includes('T SPAWN') || z.includes('ATTACKER')) return '#fb923c' // Orange
  if (z.includes('MID') || z.includes('MARKET') || z.includes('BOBA') || z.includes('MAIL'))
    return '#facc15' // Amber / Gold
  return '#94a3b8' // Slate
}

/** Render macro rotation flow graph onto canvas. */
export function drawRotationGraph(
  ctx: CanvasRenderingContext2D,
  px: number,
  dpr: number,
  zones: RotationZone[],
  transitions: RotationTransition[],
  options: DrawRotationsOptions = {},
): void {
  if (!zones.length || !transitions.length) return

  const { selectedZone, selectedRoute, side = 'defense' } = options
  const zoneMap = new Map<string, RotationZone>()
  for (const z of zones) {
    zoneMap.set(z.id, z)
  }

  const maxCount = Math.max(...transitions.map((t) => t.count), 1)

  ctx.save()

  // 1. Draw Transition Curves
  for (const t of transitions) {
    const fromZ = zoneMap.get(t.from_zone)
    const toZ = zoneMap.get(t.to_zone)
    if (!fromZ || !toZ) continue

    const p1 = { x: fromZ.x * px, y: fromZ.y * px }
    const p2 = { x: toZ.x * px, y: toZ.y * px }

    const dx = p2.x - p1.x
    const dy = p2.y - p1.y
    const dist = Math.hypot(dx, dy)
    if (dist < 5 * dpr) continue

    // Lateral curvature so opposing routes (A->B vs B->A) don't overlap
    const nx = -dy / dist
    const ny = dx / dist
    const curvature = Math.min(36 * dpr, dist * 0.16)
    const cp = {
      x: (p1.x + p2.x) / 2 + nx * curvature,
      y: (p1.y + p2.y) / 2 + ny * curvature,
    }

    const isSelectedRoute =
      selectedRoute &&
      selectedRoute.from === t.from_zone &&
      selectedRoute.to === t.to_zone
    const isConnectedToSelectedZone =
      selectedZone &&
      (t.from_zone === selectedZone || t.to_zone === selectedZone)
    const isDimmed =
      (selectedRoute && !isSelectedRoute) ||
      (selectedZone && !isConnectedToSelectedZone)

    // Flow width scaled between 1.8px and 7px
    const baseW = 1.8 + (t.count / maxCount) * 5.2
    const lineWidth = (isSelectedRoute ? baseW * 1.5 : baseW) * dpr

    ctx.save()
    if (isDimmed) {
      ctx.globalAlpha = 0.15
      ctx.strokeStyle = '#64748b'
      ctx.fillStyle = '#64748b'
    } else {
      ctx.globalAlpha = isSelectedRoute ? 1.0 : isConnectedToSelectedZone ? 0.95 : 0.75
      if (isSelectedRoute) {
        ctx.strokeStyle = '#fef08a' // Bright yellow glow
        ctx.shadowColor = 'rgba(253, 224, 71, 0.6)'
        ctx.shadowBlur = 10 * dpr
      } else if (side === 'attack') {
        ctx.strokeStyle = t.win_rate >= 0.5 ? '#fb7185' : '#f97316'
      } else {
        // Defense: Cyan / Sky or Green for winning retakes
        ctx.strokeStyle = t.win_rate >= 0.5 ? '#38bdf8' : '#60a5fa'
      }
      ctx.fillStyle = ctx.strokeStyle
    }

    ctx.lineWidth = lineWidth
    ctx.lineCap = 'round'
    ctx.beginPath()
    ctx.moveTo(p1.x, p1.y)
    ctx.quadraticCurveTo(cp.x, cp.y, p2.x, p2.y)
    ctx.stroke()

    // Arrowhead at p2 pointing along tangent (p2 - cp)
    const tx = p2.x - cp.x
    const ty = p2.y - cp.y
    const angle = Math.atan2(ty, tx)
    const arrowLen = Math.max(7 * dpr, lineWidth * 2.2)

    ctx.beginPath()
    ctx.moveTo(p2.x, p2.y)
    ctx.lineTo(
      p2.x - arrowLen * Math.cos(angle - Math.PI / 6),
      p2.y - arrowLen * Math.sin(angle - Math.PI / 6),
    )
    ctx.lineTo(
      p2.x - (arrowLen * 0.7) * Math.cos(angle),
      p2.y - (arrowLen * 0.7) * Math.sin(angle),
    )
    ctx.lineTo(
      p2.x - arrowLen * Math.cos(angle + Math.PI / 6),
      p2.y - arrowLen * Math.sin(angle + Math.PI / 6),
    )
    ctx.closePath()
    ctx.fill()

    // Draw percentage badge at midpoint for major corridors
    if (!isDimmed && (t.outgoing_share >= 0.35 || isSelectedRoute)) {
      const midPoint = {
        x: 0.25 * p1.x + 0.5 * cp.x + 0.25 * p2.x,
        y: 0.25 * p1.y + 0.5 * cp.y + 0.25 * p2.y,
      }
      const label = `${Math.round(t.outgoing_share * 100)}%`
      ctx.save()
      ctx.font = `700 ${Math.max(8, 9 * dpr)}px ui-sans-serif, system-ui, sans-serif`
      const textWidth = ctx.measureText(label).width
      const padX = 4 * dpr
      const padY = 2 * dpr

      ctx.fillStyle = 'rgba(15, 23, 42, 0.85)'
      ctx.strokeStyle = isSelectedRoute ? '#fde047' : 'rgba(148, 163, 184, 0.4)'
      ctx.lineWidth = 1 * dpr
      ctx.beginPath()
      ctx.roundRect(
        midPoint.x - textWidth / 2 - padX,
        midPoint.y - 6 * dpr - padY,
        textWidth + padX * 2,
        12 * dpr + padY * 2,
        3 * dpr,
      )
      ctx.fill()
      ctx.stroke()

      ctx.fillStyle = isSelectedRoute ? '#fef08a' : '#f8fafc'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText(label, midPoint.x, midPoint.y)
      ctx.restore()
    }

    // Draw agent avatars along the transition curve
    if (!isDimmed && t.agents && t.agents.length > 0) {
      const displayAgents = t.agents.slice(0, 2)
      const hasBadge = t.outgoing_share >= 0.35 || isSelectedRoute
      const u = hasBadge ? 0.35 : 0.5
      const curvePt = {
        x: (1 - u) * (1 - u) * p1.x + 2 * (1 - u) * u * cp.x + u * u * p2.x,
        y: (1 - u) * (1 - u) * p1.y + 2 * (1 - u) * u * cp.y + u * u * p2.y,
      }

      const tanX = 2 * (1 - u) * (cp.x - p1.x) + 2 * u * (p2.x - cp.x)
      const tanY = 2 * (1 - u) * (cp.y - p1.y) + 2 * u * (p2.y - cp.y)
      const tLen = Math.hypot(tanX, tanY) || 1
      const normX = -tanY / tLen
      const normY = tanX / tLen

      displayAgents.forEach((ag, idx) => {
        const offset = (idx - (displayAgents.length - 1) / 2) * (16 * dpr)
        const ax = curvePt.x + normX * offset
        const ay = curvePt.y + normY * offset
        const avRadius = (isSelectedRoute ? 9 : 7.5) * dpr

        ctx.save()
        ctx.beginPath()
        ctx.arc(ax, ay, avRadius, 0, Math.PI * 2)
        ctx.fillStyle = '#090d16'
        ctx.fill()
        ctx.lineWidth = 1.2 * dpr
        ctx.strokeStyle = isSelectedRoute
          ? '#fef08a'
          : side === 'attack'
          ? '#fb7185'
          : '#38bdf8'
        ctx.stroke()
        ctx.clip()

        const img = getAgentImage(ag.icon, options.onNeedRedraw)
        if (img) {
          ctx.drawImage(img, ax - avRadius, ay - avRadius, avRadius * 2, avRadius * 2)
        } else {
          ctx.fillStyle = '#94a3b8'
          ctx.font = `700 ${Math.max(7, 8 * dpr)}px sans-serif`
          ctx.textAlign = 'center'
          ctx.textBaseline = 'middle'
          ctx.fillText(ag.agent.slice(0, 1), ax, ay)
        }
        ctx.restore()
      })
    }

    ctx.restore()
  }

  // 2. Draw Zone Nodes
  for (const z of zones) {
    const cx = z.x * px
    const cy = z.y * px
    if (cx < 0 || cy < 0 || cx > px || cy > px) continue

    const isSelected = selectedZone === z.id
    const color = getZoneColor(z.name)
    const radius = (isSelected ? 14 : 11) * dpr

    ctx.save()
    // Outer glow if selected
    if (isSelected) {
      ctx.shadowColor = 'rgba(250, 204, 21, 0.7)'
      ctx.shadowBlur = 12 * dpr
    }

    // Node background circle
    ctx.beginPath()
    ctx.arc(cx, cy, radius, 0, Math.PI * 2)
    ctx.fillStyle = 'rgba(10, 15, 29, 0.92)'
    ctx.fill()
    ctx.lineWidth = (isSelected ? 2.5 : 1.8) * dpr
    ctx.strokeStyle = isSelected ? '#fde047' : color
    ctx.stroke()

    // Node Title & Traffic Label
    ctx.font = `700 ${Math.max(8, 9.5 * dpr)}px ui-sans-serif, system-ui, sans-serif`
    ctx.textAlign = 'center'
    ctx.textBaseline = 'bottom'
    ctx.fillStyle = isSelected ? '#fef08a' : '#f8fafc'
    ctx.shadowColor = 'rgba(0, 0, 0, 0.9)'
    ctx.shadowBlur = 4 * dpr
    ctx.fillText(z.name, cx, cy - radius - 2 * dpr)

    // Traffic count pill inside the circle
    ctx.font = `600 ${Math.max(7, 7.5 * dpr)}px ui-sans-serif, system-ui, sans-serif`
    ctx.textBaseline = 'middle'
    ctx.fillStyle = isSelected ? '#fde047' : color
    const traffic = z.total_traffic ?? 0
    ctx.fillText(traffic > 0 ? `${traffic}` : '', cx, cy)

    ctx.restore()
  }

  ctx.restore()
}

/** Hit-test for hovered zone node. */
export function findHoveredZone(
  x: number,
  y: number,
  zones: RotationZone[],
  threshold = 0.035,
): RotationZone | null {
  let closest: RotationZone | null = null
  let minD2 = threshold * threshold
  for (const z of zones) {
    const d2 = (x - z.x) ** 2 + (y - z.y) ** 2
    if (d2 < minD2) {
      minD2 = d2
      closest = z
    }
  }
  return closest
}

/** Hit-test for hovered rotation transition curve. */
export function findHoveredTransition(
  x: number,
  y: number,
  transitions: RotationTransition[],
  zones: RotationZone[],
  threshold = 0.035,
): { transition: RotationTransition; midpoint: { x: number; y: number } } | null {
  const zoneMap = new Map<string, RotationZone>()
  for (const z of zones) {
    zoneMap.set(z.id, z)
  }

  let closest: RotationTransition | null = null
  let closestMid: { x: number; y: number } | null = null
  let minD2 = threshold * threshold

  for (const t of transitions) {
    const fz = zoneMap.get(t.from_zone)
    const tz = zoneMap.get(t.to_zone)
    if (!fz || !tz) continue

    const dx = tz.x - fz.x
    const dy = tz.y - fz.y
    const dist = Math.hypot(dx, dy)
    if (dist < 0.01) continue

    const nx = -dy / dist
    const ny = dx / dist
    const curvature = Math.min(0.06, dist * 0.16)
    const cpx = (fz.x + tz.x) / 2 + nx * curvature
    const cpy = (fz.y + tz.y) / 2 + ny * curvature

    // Midpoint of quadratic curve B(0.5)
    const mx = 0.25 * fz.x + 0.5 * cpx + 0.25 * tz.x
    const my = 0.25 * fz.y + 0.5 * cpy + 0.25 * tz.y

    const d2 = (x - mx) ** 2 + (y - my) ** 2
    if (d2 < minD2) {
      minD2 = d2
      closest = t
      closestMid = { x: mx, y: my }
    }
  }

  return closest && closestMid ? { transition: closest, midpoint: closestMid } : null
}
