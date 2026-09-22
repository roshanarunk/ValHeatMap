import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  drawDuelLines,
  renderDivergingHeatmap,
  renderHeatmap,
  type DuelLine,
  type RampName,
  type SideKey,
} from '../lib/heatmap'
import { drawSiteBadge, drawSpotPin } from '../lib/markers'
import type { KillPoint, MapInfo, PlantPoint, PlantSpot, Vec2 } from '../lib/types'

export type RenderMode = 'heatmap' | 'lines' | 'points'

export interface MapCanvasProps {
  map: MapInfo | null
  kills?: KillPoint[]
  plants?: PlantPoint[]
  spots?: PlantSpot[]
  mode: RenderMode
  ramp: RampName
  radius: number
  intensity: number
  anchor: 'victim' | 'killer'
  /**
   * A puuid. When set and the heatmap holds both their kills and their
   * deaths, the field is drawn diverging -- green where they win, red
   * where they lose -- instead of as one undifferentiated density.
   */
  player?: string
  percentile?: number
  /** Map rotation in degrees, so a user can orient to their own side. */
  rotation?: number
  showCallouts?: boolean
  showSpots?: boolean
  highlightTraded?: boolean
  loading?: boolean
  onSelectSpot?: (id: number | null) => void
  selectedSpot?: number | null
  /** Drag-to-select a rectangular zone, in normalised map space. */
  zoneMode?: boolean
  zone?: Zone | null
  onZoneChange?: (zone: Zone | null) => void
}

/** A box in normalised map space, always stored lo->hi. */
export interface Zone {
  x0: number
  y0: number
  x1: number
  y1: number
}

interface HoverTarget {
  x: number
  y: number
  title: string
  lines: string[]
}

/** Rotate a normalised point about the map centre. */
function rotatePoint(p: Vec2, radians: number): Vec2 {
  if (!radians) return p
  const dx = p.x - 0.5
  const dy = p.y - 0.5
  const cos = Math.cos(radians)
  const sin = Math.sin(radians)
  return { x: 0.5 + dx * cos - dy * sin, y: 0.5 + dx * sin + dy * cos }
}

export function MapCanvas({
  map,
  kills = [],
  plants = [],
  spots = [],
  mode,
  ramp,
  radius,
  intensity,
  anchor,
  player,
  percentile = 0.99,
  rotation = 0,
  showCallouts = false,
  showSpots = false,
  highlightTraded = false,
  loading = false,
  onSelectSpot,
  selectedSpot = null,
  zoneMode = false,
  zone = null,
  onZoneChange,
}: MapCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const imageRef = useRef<HTMLImageElement | null>(null)
  const [size, setSize] = useState(0)
  const [imageReady, setImageReady] = useState(false)
  const [hover, setHover] = useState<HoverTarget | null>(null)
  // Drag state lives here rather than in the parent so the rubber band can
  // redraw at pointer speed without a round trip through React state.
  const dragRef = useRef<{ x: number; y: number } | null>(null)
  const [draft, setDraft] = useState<Zone | null>(null)

  const radians = useMemo(() => (rotation * Math.PI) / 180, [rotation])

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      setSize(Math.max(0, Math.floor(entries[0]?.contentRect.width ?? 0)))
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!map?.minimap) {
      imageRef.current = null
      setImageReady(false)
      return
    }
    const img = new Image()
    img.crossOrigin = 'anonymous'
    let cancelled = false
    img.onload = () => {
      if (cancelled) return
      imageRef.current = img
      setImageReady(true)
    }
    img.onerror = () => {
      if (cancelled) return
      // An unreachable CDN shouldn't blank the data layer.
      imageRef.current = null
      setImageReady(true)
    }
    img.src = map.minimap
    setImageReady(false)
    return () => {
      cancelled = true
    }
  }, [map?.minimap])

  const heatPoints: Vec2[] = useMemo(() => {
    if (plants.length > 0 && kills.length === 0) return plants.map((p) => p.position)
    return kills
      .map((k) => (anchor === 'killer' ? k.killer_pos : k.victim_pos))
      .filter((p): p is Vec2 => !!p)
  }, [kills, plants, anchor])

  /**
   * The player's own position in each duel, split by outcome.
   *
   * Their position is the *killer* end when they got the kill and the
   * *victim* end when they died, so this cannot use a single anchor --
   * that is exactly what makes the combined view worth drawing.
   */
  const divergingPoints = useMemo(() => {
    if (!player) return null
    const wins: Vec2[] = []
    const losses: Vec2[] = []
    for (const k of kills) {
      // `mine` is set by the server, which knows the subject's id. Matching
      // on puuid here does not work: the kill points carry agent names, not
      // player ids, so every duel fell through to "loss" and the whole map
      // drew red.
      const won = k.mine ?? (k.killer !== '' && k.killer === player)
      if (won) {
        const p = k.killer_pos ?? k.victim_pos
        if (p) wins.push(p)
      } else if (k.victim_pos) {
        losses.push(k.victim_pos)
      }
    }
    // Only worth diverging when both outcomes are present; one-sided data
    // is clearer as an ordinary single-hue heatmap.
    if (wins.length === 0 || losses.length === 0) return null
    return { wins, losses }
  }, [kills, player])

  const duelLines: DuelLine[] = useMemo(() => {
    if (mode !== 'lines') return []
    const out: DuelLine[] = []
    for (const k of kills) {
      if (!k.killer_pos) continue
      const side: SideKey =
        k.side === 'attack' || k.side === 'defense' ? (k.side as SideKey) : 'none'
      out.push({ from: k.killer_pos, to: k.victim_pos, side })
    }
    return out
  }, [kills, mode])

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas || size <= 0) return
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const px = Math.floor(size * dpr)
    if (canvas.width !== px || canvas.height !== px) {
      canvas.width = px
      canvas.height = px
    }
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.clearRect(0, 0, px, px)

    // Everything inside this transform is drawn in map space, so the base
    // image and every data layer rotate together.
    ctx.save()
    if (radians) {
      ctx.translate(px / 2, px / 2)
      ctx.rotate(radians)
      ctx.translate(-px / 2, -px / 2)
    }

    const img = imageRef.current
    if (img) {
      ctx.save()
      ctx.globalAlpha = 0.85
      ctx.drawImage(img, 0, 0, px, px)
      ctx.restore()
    } else {
      ctx.fillStyle = '#0d1117'
      ctx.fillRect(0, 0, px, px)
    }
    ctx.fillStyle = 'rgba(6, 10, 20, 0.35)'
    ctx.fillRect(0, 0, px, px)

    if (mode === 'heatmap' && divergingPoints) {
      renderDivergingHeatmap(ctx, px, px, {
        wins: divergingPoints.wins,
        losses: divergingPoints.losses,
        radius: radius * dpr,
        intensity,
        percentile,
      })
    } else if (mode === 'heatmap' && heatPoints.length > 0) {
      renderHeatmap(ctx, px, px, {
        points: heatPoints,
        radius: radius * dpr,
        intensity,
        ramp,
        percentile,
      })
    }

    if (mode === 'lines' && duelLines.length > 0) {
      drawDuelLines(ctx, px, duelLines, { intensity })
    }

    if (mode === 'points') {
      for (const k of kills) {
        // With a player set, plot where *they* stood: the killer end when
        // they got the kill, the victim end when they died.
        const won = k.mine ?? false
        const p = player
          ? won
            ? (k.killer_pos ?? k.victim_pos)
            : k.victim_pos
          : anchor === 'killer'
            ? k.killer_pos
            : k.victim_pos
        if (!p) continue
        const traded = highlightTraded && k.traded
        ctx.beginPath()
        ctx.arc(p.x * px, p.y * px, Math.max(1.5, 2.6 * dpr), 0, Math.PI * 2)
        // In the player view, colour is outcome (green won, red lost) to
        // match the heatmap. Elsewhere it stays attacker/defender.
        ctx.fillStyle = traded
          ? 'rgba(96, 224, 168, 0.9)'
          : player
            ? won
              ? 'rgba(64, 220, 130, 0.85)'
              : 'rgba(255, 72, 88, 0.85)'
            : k.side === 'attack'
              ? 'rgba(255, 90, 100, 0.8)'
              : 'rgba(90, 170, 255, 0.8)'
        ctx.fill()
      }
    }

    // Individual plants sit under the badges as a light scatter.
    if (showSpots && plants.length) {
      for (const p of plants) {
        ctx.beginPath()
        ctx.arc(p.position.x * px, p.position.y * px, 1.6 * dpr, 0, Math.PI * 2)
        ctx.fillStyle = p.won ? 'rgba(96, 224, 168, 0.3)' : 'rgba(255, 110, 110, 0.3)'
        ctx.fill()
      }
    }

    if (showCallouts && map?.callouts?.length) {
      ctx.save()
      ctx.font = `600 ${Math.max(8, 9 * dpr)}px ui-sans-serif, system-ui, sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      for (const c of map.callouts) {
        const cx = c.position.x * px
        const cy = c.position.y * px
        if (cx < 0 || cy < 0 || cx > px || cy > px) continue
        ctx.save()
        ctx.translate(cx, cy)
        // Keep callout text upright however the map is turned.
        if (radians) ctx.rotate(-radians)
        ctx.fillStyle = 'rgba(4, 8, 16, 0.75)'
        ctx.fillText(c.region.toUpperCase(), 1, 1)
        ctx.fillStyle = 'rgba(190, 206, 236, 0.6)'
        ctx.fillText(c.region.toUpperCase(), 0, 0)
        ctx.restore()
      }
      ctx.restore()
    }

    // Site badges and numbered spot pins go last so nothing covers them.
    if (showSpots && spots.length) {
      const bySite = new Map<string, { x: number; y: number; n: number }>()
      for (const s of spots) {
        const acc = bySite.get(s.site) ?? { x: 0, y: 0, n: 0 }
        acc.x += s.position.x * s.plants
        acc.y += s.position.y * s.plants
        acc.n += s.plants
        bySite.set(s.site, acc)
      }
      for (const [site, acc] of bySite) {
        if (acc.n === 0) continue
        const bx = (acc.x / acc.n) * px
        const by = (acc.y / acc.n) * px
        // The site centroid usually lands on top of the busiest spot pin,
        // which hid the badge entirely. Nudge it clear of the nearest pin.
        let offset = 0
        for (const s of spots) {
          if (s.site !== site) continue
          const d = Math.hypot(s.position.x * px - bx, s.position.y * px - by)
          if (d < 26 * dpr) offset = Math.max(offset, 26 * dpr - d + 16 * dpr)
        }
        drawSiteBadge(ctx, bx, by - offset, site, { dpr, rotation: radians })
      }
      spots.forEach((s, i) => {
        if (selectedSpot !== null && s.id !== selectedSpot) return
        drawSpotPin(
          ctx,
          {
            x: s.position.x,
            y: s.position.y,
            index: i + 1,
            winRate: s.win_rate,
            plants: s.plants,
            reliable: s.reliable,
          },
          px,
          { dpr, rotation: radians },
        )
      })
    }

    // Zone overlay: dim everything outside the box so the selection reads
    // as a focus rather than just an outline.
    const box = draft ?? zone
    if (box) {
      const x = box.x0 * px
      const y = box.y0 * px
      const w = (box.x1 - box.x0) * px
      const h = (box.y1 - box.y0) * px
      ctx.save()
      ctx.fillStyle = 'rgba(6, 10, 20, 0.55)'
      ctx.beginPath()
      ctx.rect(0, 0, px, px)
      ctx.rect(x, y, w, h)
      ctx.fill('evenodd')
      ctx.strokeStyle = 'rgba(255, 70, 85, 0.95)'
      ctx.lineWidth = 1.5 * dpr
      ctx.setLineDash([5 * dpr, 4 * dpr])
      ctx.strokeRect(x, y, w, h)
      ctx.setLineDash([])
      ctx.restore()
    }

    ctx.restore()
  }, [
    size, heatPoints, divergingPoints, duelLines, mode, ramp, radius, intensity,
    percentile, kills, plants, spots, anchor, showCallouts, showSpots,
    highlightTraded, map, imageReady, radians, selectedSpot, zone, draft,
  ])

  useEffect(() => {
    draw()
  }, [draw])

  const onMove = useCallback(
    (event: React.MouseEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current
      if (!canvas || size <= 0) return
      const rect = canvas.getBoundingClientRect()
      // Undo the rotation so hit-testing happens in map space.
      const raw = {
        x: (event.clientX - rect.left) / rect.width,
        y: (event.clientY - rect.top) / rect.height,
      }
      if (zoneMode && dragRef.current) return
      const pt = rotatePoint(raw, -radians)

      const near = <T,>(items: T[], pos: (i: T) => Vec2, limit: number) => {
        let best: { item: T; dist: number } | null = null
        for (const item of items) {
          const p = pos(item)
          const d = Math.hypot(p.x - pt.x, p.y - pt.y)
          if (d < limit && (!best || d < best.dist)) best = { item, dist: d }
        }
        return best?.item ?? null
      }

      if (showSpots && spots.length) {
        const spot = near(spots, (s) => s.position, 0.035)
        if (spot) {
          const idx = spots.findIndex((s) => s.id === spot.id) + 1
          setHover({
            x: spot.position.x,
            y: spot.position.y,
            title: `#${idx} · Site ${spot.site} — ${Math.round(spot.win_rate * 100)}% round win`,
            lines: [
              `${spot.plants.toLocaleString()} plants · ${spot.wins}W ${spot.losses}L`,
              `${spot.defused} defused`,
              `avg plant ${(spot.avg_plant_time_ms / 1000).toFixed(1)}s`,
              spot.reliable ? '' : 'Low sample',
            ].filter(Boolean),
          })
          return
        }
      }

      if (kills.length && mode === 'points') {
        const kill = near(
          kills,
          (k) => (anchor === 'killer' ? (k.killer_pos ?? k.victim_pos) : k.victim_pos),
          0.02,
        )
        if (kill) {
          const p = anchor === 'killer' ? (kill.killer_pos ?? kill.victim_pos) : kill.victim_pos
          setHover({
            x: p.x,
            y: p.y,
            title: `${kill.killer_agent} → ${kill.victim_agent}`,
            lines: [
              kill.ability || kill.weapon || kill.type,
              `round ${kill.round + 1} · ${(kill.t / 1000).toFixed(1)}s`,
              kill.traded ? 'Traded' : '',
            ].filter(Boolean),
          })
          return
        }
      }
      setHover(null)
    },
    [kills, spots, size, showSpots, anchor, mode, radians, zoneMode],
  )

  const toMapSpace = useCallback(
    (event: React.PointerEvent | React.MouseEvent) => {
      const canvas = canvasRef.current
      if (!canvas) return null
      const rect = canvas.getBoundingClientRect()
      return rotatePoint(
        {
          x: (event.clientX - rect.left) / rect.width,
          y: (event.clientY - rect.top) / rect.height,
        },
        -radians,
      )
    },
    [radians],
  )

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLCanvasElement>) => {
      if (!zoneMode) return
      const p = toMapSpace(event)
      if (!p) return
      event.currentTarget.setPointerCapture(event.pointerId)
      dragRef.current = p
      setDraft({ x0: p.x, y0: p.y, x1: p.x, y1: p.y })
    },
    [zoneMode, toMapSpace],
  )

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLCanvasElement>) => {
      const start = dragRef.current
      if (!zoneMode || !start) return
      const p = toMapSpace(event)
      if (!p) return
      setDraft({
        x0: Math.min(start.x, p.x),
        y0: Math.min(start.y, p.y),
        x1: Math.max(start.x, p.x),
        y1: Math.max(start.y, p.y),
      })
    },
    [zoneMode, toMapSpace],
  )

  const onPointerUp = useCallback(
    (event: React.PointerEvent<HTMLCanvasElement>) => {
      if (!zoneMode || !dragRef.current) return
      dragRef.current = null
      event.currentTarget.releasePointerCapture?.(event.pointerId)
      setDraft((current) => {
        // Ignore an accidental click: a box smaller than this selects
        // almost nothing and is nearly always a misclick.
        if (current && current.x1 - current.x0 > 0.01 && current.y1 - current.y0 > 0.01) {
          onZoneChange?.(current)
        } else {
          onZoneChange?.(null)
        }
        return null
      })
    },
    [zoneMode, onZoneChange],
  )

  const onClick = useCallback(() => {
    if (!onSelectSpot || !hover) return
    const match = spots.find(
      (s) => Math.abs(s.position.x - hover.x) < 1e-6 && Math.abs(s.position.y - hover.y) < 1e-6,
    )
    onSelectSpot(match ? (selectedSpot === match.id ? null : match.id) : null)
  }, [hover, spots, onSelectSpot, selectedSpot])

  // Hover coordinates are in map space; rotate them back for positioning.
  const hoverScreen = hover ? rotatePoint({ x: hover.x, y: hover.y }, radians) : null
  const empty = !loading && heatPoints.length === 0 && spots.length === 0

  return (
    <div className="map-canvas" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        style={{ width: size, height: size, cursor: zoneMode ? 'crosshair' : 'default' }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        onClick={onClick}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      />
      {!map && <div className="map-overlay">Select a map to begin</div>}
      {loading && <div className="map-overlay map-overlay--soft">Loading…</div>}
      {empty && map && !loading && (
        <div className="map-overlay map-overlay--soft">No events match these filters</div>
      )}
      {hover && hoverScreen && (
        <div
          className="map-tooltip"
          style={{
            left: `${hoverScreen.x * 100}%`,
            top: `${hoverScreen.y * 100}%`,
            transform: `translate(${hoverScreen.x > 0.7 ? '-105%' : '12px'}, ${
              hoverScreen.y > 0.8 ? '-105%' : '12px'
            })`,
          }}
        >
          <strong>{hover.title}</strong>
          {hover.lines.map((line) => (
            <span key={line}>{line}</span>
          ))}
        </div>
      )}
    </div>
  )
}
