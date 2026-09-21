import { useMemo } from 'react'
import { rampStops, type RampName } from '../lib/heatmap'

/**
 * Density scale for the heatmap, matching the ramp currently in use.
 * The scale is relative -- it shows where activity concentrates, not an
 * absolute kill count -- so it is labelled Low/High rather than numerically.
 */
export function HeatLegend({ ramp, label }: { ramp: RampName; label: string }) {
  const gradient = useMemo(
    () => `linear-gradient(90deg, ${rampStops(ramp, 16).join(', ')})`,
    [ramp],
  )
  return (
    <div className="heatlegend">
      <span className="heatlegend__label">{label}</span>
      <div className="heatlegend__scale">
        <span>Low</span>
        <i style={{ background: gradient }} />
        <span>High</span>
      </div>
    </div>
  )
}
