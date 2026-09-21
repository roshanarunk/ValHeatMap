import type { ReactNode } from 'react'
import { sideGradient } from '../lib/heatmap'

/**
 * The control strip that sits directly above and below the map.
 *
 * Filters that change what you are looking at belong next to the thing
 * they change -- putting them in a far sidebar meant a constant round trip
 * between the corner of the screen and the map.
 */

export function ControlBar({ children }: { children: ReactNode }) {
  return <div className="controlbar">{children}</div>
}

export function ControlGroup({
  label,
  children,
  grow,
}: {
  label?: string
  children: ReactNode
  grow?: boolean
}) {
  return (
    <div className={`controlgroup${grow ? ' controlgroup--grow' : ''}`}>
      {label && <span className="controlgroup__label">{label}</span>}
      <div className="controlgroup__body">{children}</div>
    </div>
  )
}

export function SegmentedControl<T extends string | number>({
  options,
  value,
  onChange,
  size = 'md',
}: {
  options: { value: T; label: string; title?: string; icon?: string }[]
  value: T | T[]
  onChange: (v: T) => void
  size?: 'sm' | 'md'
}) {
  const active = Array.isArray(value) ? value : [value]
  return (
    <div className={`segmented segmented--${size}`}>
      {options.map((opt) => (
        <button
          key={String(opt.value)}
          type="button"
          className={active.includes(opt.value) ? 'is-active' : ''}
          onClick={() => onChange(opt.value)}
          title={opt.title ?? opt.label}
        >
          {opt.icon && <img src={opt.icon} alt="" loading="lazy" />}
          <span>{opt.label}</span>
        </button>
      ))}
    </div>
  )
}

/** Dropdown for lists too long to sit inline, like acts or agents. */
export function Select<T extends string>({
  value,
  options,
  onChange,
  placeholder,
}: {
  value: T | ''
  options: { value: T; label: string }[]
  onChange: (v: T | '') => void
  placeholder: string
}) {
  return (
    <select
      className="minselect"
      value={value}
      onChange={(e) => onChange(e.target.value as T | '')}
    >
      <option value="">{placeholder}</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

export function RotateControl({
  rotation,
  onChange,
}: {
  rotation: number
  onChange: (deg: number) => void
}) {
  return (
    <div className="rotate">
      <button
        type="button"
        onClick={() => onChange((rotation - 90 + 360) % 360)}
        title="Rotate counter-clockwise"
        aria-label="Rotate counter-clockwise"
      >
        ⟲
      </button>
      <span title="Map rotation">{rotation}°</span>
      <button
        type="button"
        onClick={() => onChange((rotation + 90) % 360)}
        title="Rotate clockwise"
        aria-label="Rotate clockwise"
      >
        ⟳
      </button>
      {rotation !== 0 && (
        <button type="button" className="rotate__reset" onClick={() => onChange(0)}>
          Reset
        </button>
      )}
    </div>
  )
}

/** Legend for the directional duel lines. */
export function DuelLegend() {
  return (
    <div className="duellegend">
      <span className="duellegend__item">
        <i style={{ background: sideGradient('attack') }} />
        Attacker kills
      </span>
      <span className="duellegend__item">
        <i style={{ background: sideGradient('defense') }} />
        Defender kills
      </span>
      <em>gradient runs shooter → victim</em>
    </div>
  )
}
