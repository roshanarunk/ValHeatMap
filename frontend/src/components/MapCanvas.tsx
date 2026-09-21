import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { drawDot, drawDuelLine, renderHeatmap, winRateColor, type RampName } from '../lib/heatmap'
import type { KillPoint, MapInfo, PlantPoint, PlantSpot, Vec2 } from '../lib/types'

export type RenderMode = 'heatmap' | 'points' | 'duels'

export interface MapCanvasProps {
  map: MapInfo | null
  kills?: KillPoint[]
  plants?: PlantPoint[]
  spots?: PlantSpot[]
  mode: RenderMode
  ramp: RampName
  radius: number
  intensity: number
  /** Which end of the duel to plot. */
  anchor: 'victim' | 'killer'
  highlightTraded?: boolean
  showCallouts?: boolean
  showSpots?: boolean
  loading?: boolean
  /** Density percentile treated as full heat; lower = more of the map hot. */
  percentile?: number
}

interface HoverTarget {
  x: number
  y: number
  title: string
  lines: string[]
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
  highlightTraded = false,
  showCallouts = false,
  showSpots = false,
  loading = false,
  percentile = 0.995,
}: MapCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const imageRef = useRef<HTMLImageElement | null>(null)
  const [size, setSize] = useState(0)
  const [imageReady, setImageReady] = useState(false)
  const [hover, setHover] = useState<HoverTarget | null>(null)

  // The minimap is square; track the container and render at device pixel
  // ratio so the heatmap stays crisp on high-DPI displays.
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0
      setSize(Math.max(0, Math.floor(w)))
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
      imageRef.current = null
      // Render the data layer anyway -- an unreachable CDN shouldn't
      // blank the whole visualisation.
      setImageReady(true)
    }
    img.src = map.minimap
    setImageReady(false)
    return () => {
      cancelled = true
    }
  }, [map?.minimap])

  const points: Vec2[] = useMemo(() => {
    if (plants.length > 0 && kills.length === 0) return plants.map((p) => p.position)
    return kills
      .map((k) => (anchor === 'killer' ? k.killer_pos : k.victim_pos))
      .filter((p): p is Vec2 => !!p)
  }, [kills, plants, anchor])

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

    // Base map.
    const img = imageRef.current
    ctx.save()
    if (img) {
      ctx.globalAlpha = 0.85
      ctx.drawImage(img, 0, 0, px, px)
    } else {
      ctx.fillStyle = '#0d1117'
      ctx.fillRect(0, 0, px, px)
    }
    ctx.restore()

    // Darken the map so the data layer reads clearly on top of it.
    ctx.save()
    ctx.fillStyle = 'rgba(6, 10, 20, 0.35)'
    ctx.fillRect(0, 0, px, px)
    ctx.restore()

    if (mode === 'heatmap' && points.length > 0) {
      renderHeatmap(ctx, px, px, {
        points,
        radius: radius * dpr,
        intensity,
        ramp,
        percentile,
      })
    }

    if (mode === 'duels') {
      for (const k of kills) {
        if (!k.killer_pos) continue
        const from = { x: k.killer_pos.x * px, y: k.killer_pos.y * px }
        const to = { x: k.victim_pos.x * px, y: k.victim_pos.y * px }
        const color =
          highlightTraded && k.traded ? 'rgba(96, 224, 168, 0.85)' : 'rgba(150, 170, 210, 0.45)'
        drawDuelLine(ctx, from, to, color, highlightTraded && k.traded ? 0.85 : 0.35)
      }
    }

    if (mode === 'points' || mode === 'duels') {
      for (const k of kills) {
        const p = anchor === 'killer' ? k.killer_pos : k.victim_pos
        if (!p) continue
        const traded = highlightTraded && k.traded
        drawDot(ctx, p.x * px, p.y * px, {
          fill: traded
            ? 'rgba(96, 224, 168, 0.95)'
            : k.side === 'attack'
              ? 'rgba(255, 110, 110, 0.85)'
              : 'rgba(110, 180, 255, 0.85)',
          stroke: traded ? 'rgba(20, 60, 44, 0.9)' : 'rgba(8, 12, 22, 0.75)',
          radius: Math.max(2, 3.2 * dpr),
          alpha: 0.95,
        })
      }
      // Plant markers, when a plant layer is active.
      for (const p of plants) {
        drawDot(ctx, p.position.x * px, p.position.y * px, {
          fill: p.won ? 'rgba(96, 224, 168, 0.9)' : 'rgba(255, 128, 128, 0.9)',
          stroke: 'rgba(8,12,22,0.8)',
          radius: Math.max(3, 4 * dpr),
        })
      }
    }

    // Plant spots: circles sized by sample, coloured by win rate.
    if (showSpots) {
      for (const spot of spots) {
        const cx = spot.position.x * px
        const cy = spot.position.y * px
        const r = Math.max(10 * dpr, Math.min(46 * dpr, 9 * dpr + spot.plants * 2.6 * dpr))
        ctx.save()
        ctx.beginPath()
        ctx.arc(cx, cy, r, 0, Math.PI * 2)
        ctx.fillStyle = winRateColor(spot.win_rate, spot.reliable ? 0.34 : 0.16)
        ctx.fill()
        ctx.lineWidth = 2 * dpr
        ctx.strokeStyle = winRateColor(spot.win_rate, spot.reliable ? 0.95 : 0.4)
        ctx.setLineDash(spot.reliable ? [] : [4 * dpr, 4 * dpr])
        ctx.stroke()
        ctx.setLineDash([])

        const label = `${Math.round(spot.win_rate * 100)}%`
        ctx.font = `600 ${Math.max(11, 12 * dpr)}px ui-sans-serif, system-ui, sans-serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillStyle = 'rgba(4, 8, 16, 0.85)'
        ctx.fillText(label, cx + 1, cy + 1)
        ctx.fillStyle = spot.reliable ? '#ffffff' : 'rgba(255,255,255,0.65)'
        ctx.fillText(label, cx, cy)
        ctx.font = `500 ${Math.max(9, 10 * dpr)}px ui-sans-serif, system-ui, sans-serif`
        ctx.fillStyle = 'rgba(226, 236, 255, 0.72)'
        ctx.fillText(`${spot.plants}×`, cx, cy + r + 8 * dpr)
        ctx.restore()
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
        const text = c.region.toUpperCase()
        ctx.fillStyle = 'rgba(4, 8, 16, 0.75)'
        ctx.fillText(text, cx + 1, cy + 1)
        ctx.fillStyle = 'rgba(190, 206, 236, 0.62)'
        ctx.fillText(text, cx, cy)
      }
      ctx.restore()
    }
  }, [
    size, points, mode, ramp, radius, intensity, percentile, kills, plants,
    spots, anchor, highlightTraded, showCallouts, showSpots, map, imageReady,
  ])

  useEffect(() => {
    draw()
  }, [draw])

  // Hover: find the nearest plotted feature to the cursor.
  const onMove = useCallback(
    (event: React.MouseEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current
      if (!canvas || size <= 0) return
      const rect = canvas.getBoundingClientRect()
      const nx = (event.clientX - rect.left) / rect.width
      const ny = (event.clientY - rect.top) / rect.height

      const near = <T,>(items: T[], pos: (i: T) => Vec2, limit: number) => {
        let best: { item: T; dist: number } | null = null
        for (const item of items) {
          const p = pos(item)
          const d = Math.hypot(p.x - nx, p.y - ny)
          if (d < limit && (!best || d < best.dist)) best = { item, dist: d }
        }
        return best?.item ?? null
      }

      if (showSpots && spots.length) {
        const spot = near(spots, (s) => s.position, 0.045)
        if (spot) {
          setHover({
            x: spot.position.x,
            y: spot.position.y,
            title: `Site ${spot.site} — ${Math.round(spot.win_rate * 100)}% round win`,
            lines: [
              `${spot.plants} plants · ${spot.wins}W ${spot.losses}L`,
              `${spot.defused} defused`,
              `avg plant ${(spot.avg_plant_time_ms / 1000).toFixed(1)}s`,
              spot.reliable ? '' : 'Low sample — treat with caution',
            ].filter(Boolean),
          })
          return
        }
      }

      if (plants.length) {
        const plant = near(plants, (p) => p.position, 0.03)
        if (plant) {
          setHover({
            x: plant.position.x,
            y: plant.position.y,
            title: `Round ${plant.round + 1} — site ${plant.site}`,
            lines: [
              plant.won ? 'Round won' : 'Round lost',
              plant.defused ? 'Defused' : '',
              `planted ${(plant.t / 1000).toFixed(1)}s`,
            ].filter(Boolean),
          })
          return
        }
      }

      if (kills.length && mode !== 'heatmap') {
        const kill = near(
          kills,
          (k) => (anchor === 'killer' ? (k.killer_pos ?? k.victim_pos) : k.victim_pos),
          0.028,
        )
        if (kill) {
          const p = anchor === 'killer' ? (kill.killer_pos ?? kill.victim_pos) : kill.victim_pos
          setHover({
            x: p.x,
            y: p.y,
            title: `${kill.killer} → ${kill.victim}`,
            lines: [
              `${kill.killer_agent} vs ${kill.victim_agent}`,
              kill.ability || kill.weapon || kill.type,
              `round ${kill.round + 1} · ${(kill.t / 1000).toFixed(1)}s`,
              kill.traded ? 'Traded' : '',
              kill.first_blood ? 'Opening kill' : '',
            ].filter(Boolean),
          })
          return
        }
      }
      setHover(null)
    },
    [kills, plants, spots, size, showSpots, anchor, mode],
  )

  const empty = !loading && points.length === 0 && spots.length === 0

  return (
    <div className="map-canvas" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        style={{ width: size, height: size }}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      />
      {!map && <div className="map-overlay">Select a map to begin</div>}
      {loading && <div className="map-overlay map-overlay--soft">Loading…</div>}
      {empty && map && (
        <div className="map-overlay map-overlay--soft">
          No events match these filters
        </div>
      )}
      {hover && (
        <div
          className="map-tooltip"
          style={{
            left: `${hover.x * 100}%`,
            top: `${hover.y * 100}%`,
            // Flip the tooltip when it would overflow the right/bottom edge.
            transform: `translate(${hover.x > 0.7 ? '-105%' : '12px'}, ${
              hover.y > 0.8 ? '-105%' : '12px'
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
