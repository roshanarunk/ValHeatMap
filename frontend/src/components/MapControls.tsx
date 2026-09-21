import { useEffect, useRef, useState, type ReactNode } from 'react'
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


/**
 * Multi-select dropdown for lists too long to sit inline as chips.
 *
 * Weapons are the motivating case: twenty of them, and comparing two or
 * three (rifles, or every pistol) is the common question, which a
 * single-select cannot express.
 */
export function MultiSelect<T extends string>({
  values,
  options,
  onChange,
  placeholder,
  groups,
}: {
  values: T[]
  options: { value: T; label: string; hint?: string }[]
  onChange: (values: T[]) => void
  placeholder: string
  /** Optional one-click presets, e.g. "Rifles". */
  groups?: { label: string; values: T[] }[]
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  const toggle = (value: T) =>
    onChange(values.includes(value) ? values.filter((v) => v !== value) : [...values, value])

  const label =
    values.length === 0
      ? placeholder
      : values.length === 1
        ? (options.find((o) => o.value === values[0])?.label ?? values[0])
        : `${values.length} selected`

  return (
    <div className="multiselect" ref={ref}>
      <button
        type="button"
        className={`multiselect__button${values.length ? ' is-active' : ''}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span>{label}</span>
        <i aria-hidden="true">▾</i>
      </button>
      {open && (
        <div className="multiselect__menu">
          {groups && groups.length > 0 && (
            <div className="multiselect__groups">
              {groups.map((g) => (
                <button
                  key={g.label}
                  type="button"
                  onClick={() => {
                    // Toggle the whole group: select it if any member is
                    // missing, otherwise clear it.
                    const all = g.values.every((v) => values.includes(v))
                    onChange(
                      all
                        ? values.filter((v) => !g.values.includes(v))
                        : [...new Set([...values, ...g.values])],
                    )
                  }}
                >
                  {g.label}
                </button>
              ))}
              {values.length > 0 && (
                <button type="button" className="is-clear" onClick={() => onChange([])}>
                  Clear
                </button>
              )}
            </div>
          )}
          <div className="multiselect__list">
            {options.map((o) => (
              <label key={o.value} className={values.includes(o.value) ? 'is-on' : ''}>
                <input
                  type="checkbox"
                  checked={values.includes(o.value)}
                  onChange={() => toggle(o.value)}
                />
                <span>{o.label}</span>
                {o.hint && <em>{o.hint}</em>}
              </label>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
