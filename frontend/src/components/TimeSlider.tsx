import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

export interface TimeSliderProps {
  /** Full round-time domain in ms. */
  max: number
  value: [number, number]
  onChange: (range: [number, number]) => void
  /** Kill counts per bucket, drawn as a density strip behind the track. */
  histogram?: { t: number; count: number }[]
  disabled?: boolean
}

const fmt = (ms: number) => {
  const s = Math.max(0, ms) / 1000
  const m = Math.floor(s / 60)
  const rest = s - m * 60
  return m > 0 ? `${m}:${rest.toFixed(0).padStart(2, '0')}` : `${rest.toFixed(1)}s`
}

/**
 * Dual-handle range slider over round time.
 *
 * Built from pointer events rather than two stacked <input type=range>
 * elements so the handles can cross-clamp and the density strip can sit
 * inside the track.
 */
export function TimeSlider({ max, value, onChange, histogram = [], disabled }: TimeSliderProps) {
  const trackRef = useRef<HTMLDivElement | null>(null)
  const [dragging, setDragging] = useState<'start' | 'end' | null>(null)
  const [start, end] = value

  const peak = useMemo(
    () => histogram.reduce((acc, b) => Math.max(acc, b.count), 0),
    [histogram],
  )

  const posFromEvent = useCallback(
    (clientX: number) => {
      const el = trackRef.current
      if (!el) return 0
      const rect = el.getBoundingClientRect()
      const ratio = (clientX - rect.left) / rect.width
      return Math.round(Math.max(0, Math.min(1, ratio)) * max)
    },
    [max],
  )

  useEffect(() => {
    if (!dragging) return
    const move = (e: PointerEvent) => {
      const t = posFromEvent(e.clientX)
      if (dragging === 'start') onChange([Math.min(t, end), end])
      else onChange([start, Math.max(t, start)])
    }
    const up = () => setDragging(null)
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
  }, [dragging, posFromEvent, onChange, start, end])

  const startPct = max > 0 ? (start / max) * 100 : 0
  const endPct = max > 0 ? (end / max) * 100 : 100

  const grab = (handle: 'start' | 'end') => (e: React.PointerEvent) => {
    if (disabled) return
    e.preventDefault()
    setDragging(handle)
  }

  // Click anywhere on the track to move the nearer handle there.
  const onTrackDown = (e: React.PointerEvent) => {
    if (disabled) return
    const t = posFromEvent(e.clientX)
    if (Math.abs(t - start) <= Math.abs(t - end)) {
      onChange([Math.min(t, end), end])
      setDragging('start')
    } else {
      onChange([start, Math.max(t, start)])
      setDragging('end')
    }
  }

  const nudge = (handle: 'start' | 'end', delta: number) => {
    if (handle === 'start') onChange([Math.max(0, Math.min(start + delta, end)), end])
    else onChange([start, Math.min(max, Math.max(end + delta, start))])
  }

  return (
    <div className={`time-slider${disabled ? ' is-disabled' : ''}`}>
      <div className="time-slider__head">
        <span className="time-slider__label">Round time</span>
        <span className="time-slider__readout">
          {fmt(start)} – {fmt(end)}
        </span>
      </div>

      <div className="time-slider__track" ref={trackRef} onPointerDown={onTrackDown}>
        <div className="time-slider__hist" aria-hidden="true">
          {histogram.map((b) => (
            <span
              key={b.t}
              style={{
                left: `${(b.t / Math.max(1, max)) * 100}%`,
                height: `${peak > 0 ? Math.max(8, (b.count / peak) * 100) : 0}%`,
                opacity: b.t >= start && b.t <= end ? 0.85 : 0.22,
              }}
            />
          ))}
        </div>
        <div className="time-slider__rail" />
        <div
          className="time-slider__range"
          style={{ left: `${startPct}%`, width: `${Math.max(0, endPct - startPct)}%` }}
        />
        <button
          type="button"
          className="time-slider__handle"
          style={{ left: `${startPct}%` }}
          onPointerDown={grab('start')}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft') nudge('start', -1000)
            if (e.key === 'ArrowRight') nudge('start', 1000)
          }}
          aria-label="Range start"
          aria-valuenow={start}
          aria-valuemin={0}
          aria-valuemax={max}
          role="slider"
          disabled={disabled}
        />
        <button
          type="button"
          className="time-slider__handle"
          style={{ left: `${endPct}%` }}
          onPointerDown={grab('end')}
          onKeyDown={(e) => {
            if (e.key === 'ArrowLeft') nudge('end', -1000)
            if (e.key === 'ArrowRight') nudge('end', 1000)
          }}
          aria-label="Range end"
          aria-valuenow={end}
          aria-valuemin={0}
          aria-valuemax={max}
          role="slider"
          disabled={disabled}
        />
      </div>

      <div className="time-slider__presets">
        <button type="button" onClick={() => onChange([0, max])} disabled={disabled}>
          Full round
        </button>
        <button type="button" onClick={() => onChange([0, 20000])} disabled={disabled}>
          Early (0–20s)
        </button>
        <button type="button" onClick={() => onChange([20000, 45000])} disabled={disabled}>
          Mid
        </button>
        <button type="button" onClick={() => onChange([45000, max])} disabled={disabled}>
          Late
        </button>
      </div>
    </div>
  )
}
