import type { ReactNode } from 'react'

export function Panel({
  title,
  subtitle,
  children,
  actions,
}: {
  title: string
  subtitle?: string
  children: ReactNode
  actions?: ReactNode
}) {
  return (
    <section className="panel">
      <header className="panel__head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {actions}
      </header>
      <div className="panel__body">{children}</div>
    </section>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field__label">
        {label}
        {hint && <em title={hint}>?</em>}
      </span>
      {children}
    </label>
  )
}

export function ChipGroup<T extends string>({
  options,
  selected,
  onToggle,
  onClear,
  icons,
}: {
  options: { value: T; label: string }[]
  selected: T[]
  onToggle: (value: T) => void
  onClear?: () => void
  icons?: Record<string, string>
}) {
  return (
    <div className="chips">
      {onClear && (
        <button
          type="button"
          className={`chip chip--all${selected.length === 0 ? ' is-active' : ''}`}
          onClick={onClear}
        >
          All
        </button>
      )}
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          className={`chip${selected.includes(opt.value) ? ' is-active' : ''}`}
          onClick={() => onToggle(opt.value)}
          title={opt.label}
        >
          {icons?.[opt.value] && <img src={icons[opt.value]} alt="" loading="lazy" />}
          <span>{opt.label}</span>
        </button>
      ))}
    </div>
  )
}

export function Toggle({
  label,
  checked,
  onChange,
  hint,
}: {
  label: string
  checked: boolean
  onChange: (v: boolean) => void
  hint?: string
}) {
  return (
    <button
      type="button"
      className={`toggle${checked ? ' is-on' : ''}`}
      onClick={() => onChange(!checked)}
      title={hint}
      aria-pressed={checked}
    >
      <span className="toggle__dot" />
      <span>{label}</span>
    </button>
  )
}

export function Slider({
  label,
  min,
  max,
  step = 1,
  value,
  onChange,
  format,
}: {
  label: string
  min: number
  max: number
  step?: number
  value: number
  onChange: (v: number) => void
  format?: (v: number) => string
}) {
  return (
    <label className="slider">
      <span className="slider__head">
        <span>{label}</span>
        <em>{format ? format(value) : value}</em>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  )
}

export function StatTile({
  label,
  value,
  sub,
  tone = 'neutral',
}: {
  label: string
  value: string
  sub?: string
  tone?: 'neutral' | 'good' | 'warn' | 'hot'
}) {
  return (
    <div className={`stat stat--${tone}`}>
      <span className="stat__label">{label}</span>
      <strong className="stat__value">{value}</strong>
      {sub && <span className="stat__sub">{sub}</span>}
    </div>
  )
}

export function Bar({ value, max, tone }: { value: number; max: number; tone?: string }) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0
  return (
    <div className="bar">
      <span style={{ width: `${pct}%`, background: tone }} />
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}
