import { useMemo } from 'react'
import { divergingStops, rampStops, type RampName } from '../lib/heatmap'

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

/**
 * Scale for the combined kills-and-deaths view.
 *
 * Unlike the density legend above, colour here is not "how much" but "who
 * won": the ends are the two outcomes and the middle means the duels at
 * that spot are evenly split, not that nothing happened there. Opacity
 * still carries volume, which is why a contested spot looks dim.
 */
export function DivergingLegend() {
  const gradient = useMemo(
    () => `linear-gradient(90deg, ${divergingStops(11).join(', ')})`,
    [],
  )
  return (
    <div className="heatlegend">
      <span className="heatlegend__label">Duels won here</span>
      <div className="heatlegend__scale">
        <span>Died more</span>
        <i style={{ background: gradient }} />
        <span>Killed more</span>
      </div>
    </div>
  )
}
