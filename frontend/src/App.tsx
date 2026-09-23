import { useCallback, useEffect, useMemo, useState } from 'react'
import { MapCanvas, type RenderMode, type Zone } from './components/MapCanvas'
import {
  ControlBar,
  ControlGroup,
  DuelLegend,
  RotateControl,
  SegmentedControl,
  MultiSelect,
  Select,
} from './components/MapControls'
import { PlayerView } from './components/PlayerView'
import { TimeSlider } from './components/TimeSlider'
import { Empty, Panel, Slider, StatTile, Toggle } from './components/Controls'
import { HeatLegend } from './components/HeatLegend'
import { api, ApiError, type QueryFilters } from './lib/api'
import type { RampName } from './lib/heatmap'
import { winRateCss } from './lib/markers'
import type {
  Facets,
  InsightsResponseV2,
  KillsResponseV2,
  PlantsResponse,
  UtilityResponseV2,
} from './lib/types'

type View = 'kills' | 'utility' | 'plants' | 'insights' | 'player'

const ROUND_MAX_MS = 120_000
/** Splat radius in px. Tight by default so hotspots stay distinct. */
const DEFAULT_RADIUS = 5
/** "Balanced" on the hotspot-focus scale. */
const DEFAULT_PERCENTILE = 0.985
const VIEWS: { id: View; label: string }[] = [
  { id: 'kills', label: 'Kills' },
  { id: 'utility', label: 'Utility' },
  { id: 'plants', label: 'Plants' },
  { id: 'insights', label: 'Breakdown' },
  { id: 'player', label: 'My stats' },
]

// Valorant's own shop categories, so "Rifles" selects what a player means
// by it rather than an arbitrary grouping.
const WEAPON_GROUPS: { label: string; values: string[] }[] = [
  { label: 'Rifles', values: ['Vandal', 'Phantom', 'Bulldog', 'Guardian'] },
  { label: 'Snipers', values: ['Operator', 'Marshal', 'Outlaw'] },
  { label: 'SMGs', values: ['Spectre', 'Stinger'] },
  { label: 'Pistols', values: ['Classic', 'Shorty', 'Frenzy', 'Ghost', 'Sheriff'] },
  { label: 'Shotguns', values: ['Bucky', 'Judge'] },
  { label: 'Heavy', values: ['Ares', 'Odin'] },
]

const pct = (v: number) => `${Math.round(v * 100)}%`
const num = (v: number) => v.toLocaleString()

export default function App() {
  const [facets, setFacets] = useState<Facets | null>(null)
  const [view, setView] = useState<View>('kills')
  const [mapName, setMapName] = useState('')

  // filters
  const [act, setAct] = useState('')
  const [rank, setRank] = useState('')
  const [agent, setAgent] = useState('')
  const [roles, setRoles] = useState<string[]>([])
  const [ability, setAbility] = useState('')
  const [weapons, setWeapons] = useState<string[]>([])
  // Zone cross-filter: the box constrains one end of each duel and the map
  // plots the other, so "kills by people here" and "deaths caused from
  // here" are two views of the same selection.
  const [zoneMode, setZoneMode] = useState(false)
  const [zone, setZone] = useState<Zone | null>(null)
  const [sides, setSides] = useState<string[]>([])
  const [timeRange, setTimeRange] = useState<[number, number]>([0, ROUND_MAX_MS])
  const [tradedOnly, setTradedOnly] = useState(false)
  const [untradedOnly, setUntradedOnly] = useState(false)
  const [firstBloodOnly, setFirstBloodOnly] = useState(false)
  const [postPlantOnly, setPostPlantOnly] = useState(false)

  // display
  const [renderMode, setRenderMode] = useState<RenderMode>('heatmap')
  const [anchor, setAnchor] = useState<'victim' | 'killer'>('victim')
  const [ramp, setRamp] = useState<RampName>('inferno')
  const [radiusOverride, setRadiusOverride] = useState<number | null>(null)
  const [intensity, setIntensity] = useState(0.95)
  const [percentile, setPercentile] = useState(DEFAULT_PERCENTILE)
  const [rotation, setRotation] = useState(0)
  const [showCallouts, setShowCallouts] = useState(false)
  const [clusterRadius, setClusterRadius] = useState(800)
  const [selectedSpot, setSelectedSpot] = useState<number | null>(null)

  // data
  const [kills, setKills] = useState<KillsResponseV2 | null>(null)
  const [utility, setUtility] = useState<UtilityResponseV2 | null>(null)
  const [plants, setPlants] = useState<PlantsResponse | null>(null)
  const [insights, setInsights] = useState<InsightsResponseV2 | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .facets()
      .then((f) => {
        setFacets(f)
        setMapName((cur) => cur || f.maps[0]?.map_name || '')
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  const filters: QueryFilters = useMemo(
    () => ({
      map_name: mapName || undefined,
      acts: act ? [act] : [],
      ranks: rank ? [rank] : [],
      agents: agent ? [agent] : [],
      roles,
      abilities: ability ? [ability] : [],
      weapons,
      sides,
      zone: zone ? `${zone.x0},${zone.y0},${zone.x1},${zone.y1}` : undefined,
      // The zone constrains the opposite end from the one being plotted:
      // selecting where killers stood shows where their victims fell.
      zone_anchor: zone ? (anchor === 'victim' ? 'killer' : 'victim') : undefined,
      time_start: timeRange[0] > 0 ? timeRange[0] : undefined,
      time_end: timeRange[1] < ROUND_MAX_MS ? timeRange[1] : undefined,
      traded_only: tradedOnly,
      untraded_only: untradedOnly,
      first_blood_only: firstBloodOnly,
      post_plant_only: postPlantOnly,
    }),
    [
      mapName, act, rank, agent, roles, ability, weapons, sides, timeRange, zone,
      anchor, tradedOnly, untradedOnly, firstBloodOnly, postPlantOnly,
    ],
  )

  useEffect(() => {
    // The player tab fetches its own data and has no map selected, so it
    // must not fall through to the insights request below.
    if (!mapName || view === 'player') return
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    const run = async () => {
      try {
        if (view === 'kills') {
          const res = await api.killsV2(filters, { signal: controller.signal })
          setKills(res)
        } else if (view === 'utility') {
          const res = await api.utilityV2(filters, { signal: controller.signal })
          setUtility(res)
        } else if (view === 'plants') {
          const res = await api.plants(
            { ...filters, cluster_radius: clusterRadius },
            { signal: controller.signal },
          )
          setPlants(res)
        } else {
          const res = await api.insightsV2(filters, { signal: controller.signal })
          setInsights(res)
        }
      } catch (e) {
        if (controller.signal.aborted) return
        setError(
          e instanceof ApiError && e.status === 404
            ? 'No data for this selection.'
            : e instanceof Error
              ? e.message
              : String(e),
        )
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    run()
    return () => {
      controller.abort()
    }
  }, [view, filters, mapName, clusterRadius])

  const toggle = (list: string[], value: string) =>
    list.includes(value) ? list.filter((v) => v !== value) : [...list, value]

  const shownKills =
    view === 'utility' ? (utility?.points ?? []) : view === 'kills' ? (kills?.points ?? []) : []
  const stats = view === 'utility' ? utility?.stats : kills?.stats
  const total = view === 'utility' ? utility?.total : kills?.total
  const sampled = view === 'utility' ? utility?.sampled : kills?.sampled
  const activeMap =
    view === 'plants' ? plants?.map : view === 'utility' ? utility?.map : kills?.map

  // Fixed 5px rather than scaling with point count: a tight splat keeps
  // individual positions readable instead of blurring neighbouring spots
  // into one mass, which is what the auto sizing did on busy maps.
  const radius = radiusOverride ?? DEFAULT_RADIUS

  const resetFilters = useCallback(() => {
    setAct('')
    setRank('')
    setAgent('')
    setRoles([])
    setAbility('')
    setWeapons([])
    setSides([])
    setZone(null)
    setTimeRange([0, ROUND_MAX_MS])
    setTradedOnly(false)
    setUntradedOnly(false)
    setFirstBloodOnly(false)
    setPostPlantOnly(false)
  }, [])

  const activeFilters =
    (act ? 1 : 0) + (rank ? 1 : 0) + (agent ? 1 : 0) + roles.length +
    (ability ? 1 : 0) + (weapons.length ? 1 : 0) +
    (zone ? 1 : 0) + sides.length +
    (timeRange[0] > 0 || timeRange[1] < ROUND_MAX_MS ? 1 : 0) +
    [tradedOnly, untradedOnly, firstBloodOnly, postPlantOnly].filter(Boolean).length

  const isPlants = view === 'plants'
  const isInsights = view === 'insights'
  const isPlayer = view === 'player'

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand__mark" aria-hidden="true" />
          <div>
            <h1>ValHeatMap</h1>
            <p>Spatial Valorant analytics</p>
          </div>
        </div>

        <nav className="viewnav viewnav--compact">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              type="button"
              className={view === v.id ? 'is-active' : ''}
              onClick={() => setView(v.id)}
            >
              {v.label}
            </button>
          ))}
        </nav>

        {facets && (
          <div className="datasetline">
            <strong>{num(facets.stats.matches)}</strong> matches ·{' '}
            <strong>{num(facets.stats.kills)}</strong> kills
          </div>
        )}
      </header>

      {/* The player tab is its own thing: no map picker and none of the
          global filters apply to it, so it replaces the stage entirely. */}
      {isPlayer ? (
        <PlayerView />
      ) : (
      <>
      <div className="mapselect">
        {facets?.maps.map((m) => (
          <button
            key={m.map_name}
            type="button"
            className={`mapchip${mapName === m.map_name ? ' is-active' : ''}`}
            onClick={() => {
              setMapName(m.map_name)
              setSelectedSpot(null)
            }}
            title={`${num(m.matches)} matches · ${num(m.kills)} kills`}
          >
            {m.minimap && <img src={m.minimap} alt="" loading="lazy" />}
            <span>{m.map_name}</span>
          </button>
        ))}
      </div>

      {error && <div className="banner banner--error">{error}</div>}

      <main className="stage2">
        <div className="stage2__main">
          {/* --- controls above the map --- */}
          <ControlBar>
            <ControlGroup label="Act">
              <Select
                value={act}
                placeholder="All acts"
                options={(facets?.acts ?? []).map((a) => ({
                  value: a.act,
                  label: `${a.act} (${num(a.matches)})`,
                }))}
                onChange={setAct}
              />
            </ControlGroup>

            <ControlGroup label="Rank">
              <SegmentedControl
                size="sm"
                options={[
                  { value: '', label: 'All' },
                  ...(facets?.ranks ?? []).map((r) => ({ value: r.id, label: r.name })),
                ]}
                value={rank}
                onChange={(v) => setRank(v === rank ? '' : v)}
              />
            </ControlGroup>

            {!isPlants && (
              <ControlGroup label="Side">
                <SegmentedControl
                  size="sm"
                  options={[
                    { value: 'attack', label: 'Attack' },
                    { value: 'defense', label: 'Defense' },
                  ]}
                  value={sides}
                  onChange={(v) => setSides((cur) => toggle(cur, v))}
                />
              </ControlGroup>
            )}

            {view === 'utility' && (
              <ControlGroup label="Ability">
                <Select
                  value={ability}
                  placeholder="All abilities"
                  options={(facets?.abilities ?? []).map((a) => ({
                    value: a.ability,
                    label: `${a.ability} · ${a.agent}`,
                  }))}
                  onChange={setAbility}
                />
              </ControlGroup>
            )}

            {!isPlants && !isInsights && (
              <ControlGroup label="Weapon">
                <MultiSelect
                  values={weapons}
                  placeholder="All weapons"
                  groups={WEAPON_GROUPS}
                  options={(facets?.weapons ?? []).map((w) => ({
                    value: w.weapon,
                    label: w.weapon,
                    hint: w.kills.toLocaleString(),
                  }))}
                  onChange={setWeapons}
                />
              </ControlGroup>
            )}

            {!isPlants && (
              <ControlGroup label="Agent">
                <Select
                  value={agent}
                  placeholder="All agents"
                  options={(facets?.agents ?? []).map((a) => ({
                    value: a.agent,
                    label: a.agent,
                  }))}
                  onChange={setAgent}
                />
              </ControlGroup>
            )}

            {!isPlants && (
              <ControlGroup label="Role">
                <MultiSelect
                  values={roles}
                  placeholder="All roles"
                  options={(facets?.roles ?? []).map((r) => ({
                    value: r.role,
                    label: r.role,
                    hint: num(r.kills),
                  }))}
                  onChange={setRoles}
                />
              </ControlGroup>
            )}

            {!isPlants && (
              <ControlGroup label="View">
                <SegmentedControl
                  size="sm"
                  options={[
                    { value: 'heatmap' as RenderMode, label: 'Heatmap' },
                    { value: 'lines' as RenderMode, label: 'Duel lines' },
                    { value: 'points' as RenderMode, label: 'Points' },
                  ]}
                  value={renderMode}
                  onChange={setRenderMode}
                />
              </ControlGroup>
            )}

            {!isPlants && !isInsights && (
              <ControlGroup label="Zone">
                <Toggle
                  label={zoneMode ? 'Drawing' : 'Select area'}
                  checked={zoneMode}
                  onChange={(v) => {
                    setZoneMode(v)
                    if (!v) setZone(null)
                  }}
                  hint="Drag a box on the map to focus one area"
                />
                {zone && (
                  <button type="button" className="linkbtn" onClick={() => setZone(null)}>
                    Clear
                  </button>
                )}
              </ControlGroup>
            )}

            <ControlGroup label="Map">
              <RotateControl rotation={rotation} onChange={setRotation} />
            </ControlGroup>

            {activeFilters > 0 && (
              <button type="button" className="linkbtn resetall" onClick={resetFilters}>
                Reset {activeFilters}
              </button>
            )}
          </ControlBar>

          {/* --- the map --- */}
          {!isInsights && (
            <div className="stage2__map">
              <MapCanvas
                map={activeMap ?? null}
                kills={shownKills}
                plants={isPlants ? (plants?.points ?? []) : []}
                spots={isPlants ? (plants?.spots ?? []) : []}
                mode={isPlants ? 'points' : renderMode}
                ramp={view === 'utility' ? 'toxic' : ramp}
                radius={radius}
                intensity={intensity}
                anchor={anchor}
                percentile={percentile}
                rotation={rotation}
                showCallouts={showCallouts}
                showSpots={isPlants}
                highlightTraded
                loading={loading}
                selectedSpot={selectedSpot}
                onSelectSpot={setSelectedSpot}
                zoneMode={zoneMode && !isPlants}
                zone={zone}
                onZoneChange={setZone}
              />
            </div>
          )}

          {/* --- controls below the map --- */}
          {!isInsights && (
            <>
              {zone && !isPlants && (
                <div className="zonenote">
                  <strong>
                    {anchor === 'victim'
                      ? 'Deaths caused by players inside the box'
                      : 'Where the players who died inside the box were killed from'}
                  </strong>
                  <span>
                    The box holds one end of each duel; the map plots the other.
                    Switch <em>Plot</em> to flip which end.
                  </span>
                  <button type="button" className="linkbtn" onClick={() => setZone(null)}>
                    Clear zone
                  </button>
                </div>
              )}
              {zoneMode && !zone && !isPlants && (
                <div className="zonenote zonenote--hint">
                  <span>Drag a box on the map to focus an area.</span>
                </div>
              )}
              {renderMode === 'lines' && !isPlants && <DuelLegend />}
              {renderMode === 'heatmap' && !isPlants && (
                <HeatLegend
                  ramp={view === 'utility' ? 'toxic' : ramp}
                  label={anchor === 'killer' ? 'Kills from here' : 'Deaths here'}
                />
              )}

              {!isPlants && (
                <div className="timerow">
                  <TimeSlider
                    max={ROUND_MAX_MS}
                    value={timeRange}
                    onChange={setTimeRange}
                    histogram={kills?.histogram ?? []}
                  />
                </div>
              )}

              <ControlBar>
                {!isPlants && (
                  <ControlGroup label="Plot">
                    <SegmentedControl
                      size="sm"
                      options={[
                        { value: 'victim' as const, label: 'Deaths' },
                        { value: 'killer' as const, label: 'Killer spots' },
                      ]}
                      value={anchor}
                      onChange={setAnchor}
                    />
                  </ControlGroup>
                )}
                {!isPlants && (
                  <ControlGroup label="Filter" grow>
                    <div className="togglerow">
                      <Toggle
                        label="Traded"
                        checked={tradedOnly}
                        onChange={(v) => {
                          setTradedOnly(v)
                          if (v) setUntradedOnly(false)
                        }}
                        hint="Deaths a teammate avenged"
                      />
                      <Toggle
                        label="Untraded"
                        checked={untradedOnly}
                        onChange={(v) => {
                          setUntradedOnly(v)
                          if (v) setTradedOnly(false)
                        }}
                      />
                      <Toggle
                        label="Openings"
                        checked={firstBloodOnly}
                        onChange={setFirstBloodOnly}
                      />
                      <Toggle
                        label="Post-plant"
                        checked={postPlantOnly}
                        onChange={setPostPlantOnly}
                      />
                    </div>
                  </ControlGroup>
                )}
                {isPlants && (
                  <ControlGroup label="Spot grouping" grow>
                    <Slider
                      label=""
                      min={200}
                      max={2500}
                      step={100}
                      value={clusterRadius}
                      onChange={setClusterRadius}
                      format={(v) => `${(v / 100).toFixed(0)}m`}
                    />
                  </ControlGroup>
                )}
                <ControlGroup label="Display">
                  <div className="togglerow">
                    <Toggle label="Callouts" checked={showCallouts} onChange={setShowCallouts} />
                  </div>
                </ControlGroup>
              </ControlBar>
            </>
          )}

          {isInsights && <InsightsPanel data={insights} loading={loading} />}
        </div>

        {/* --- side rail --- */}
        <aside className="stage2__side">
          {!isPlants && !isInsights && (
            <div className="statgrid">
              <StatTile
                label={view === 'utility' ? 'Utility kills' : 'Kills'}
                value={num(total ?? 0)}
                sub={
                  sampled
                    ? `showing ${num(shownKills.length)} sampled`
                    : `${num(stats?.matches ?? 0)} matches`
                }
              />
              <StatTile
                label="Traded"
                value={stats ? pct(stats.trade_rate) : '—'}
                sub="of deaths avenged"
                tone="good"
              />
              <StatTile label="Openings" value={num(stats?.first_bloods ?? 0)} sub="first duels" />
              <StatTile
                label="Post-plant"
                value={num(stats?.post_plant ?? 0)}
                sub="after the spike"
                tone="warn"
              />
            </div>
          )}

          {isPlants && plants && (
            <PlantSide data={plants} selected={selectedSpot} onSelect={setSelectedSpot} />
          )}

          {view === 'utility' && utility && (
            <Panel title="Abilities" subtitle="Damaging util that finished a kill">
              {utility.abilities.length === 0 ? (
                <Empty>No utility kills here.</Empty>
              ) : (
                <ul className="ranklist">
                  {utility.abilities.slice(0, 14).map((a) => (
                    <li key={`${a.agent}-${a.ability}`}>
                      <span className="ranklist__label">
                        <strong>{a.ability}</strong>
                        <em>{a.agent}</em>
                      </span>
                      <div className="bar">
                        <span
                          style={{
                            width: `${(a.kills / utility.abilities[0].kills) * 100}%`,
                            background: 'linear-gradient(90deg,#63d471,#b6f09c)',
                          }}
                        />
                      </div>
                      <span className="ranklist__value">{num(a.kills)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          )}

          {!isPlants && !isInsights && (
            <Panel title="Rendering">
              <SegmentedControl
                size="sm"
                options={[
                  { value: 'inferno' as RampName, label: 'Inferno' },
                  { value: 'ice' as RampName, label: 'Ice' },
                  { value: 'toxic' as RampName, label: 'Toxic' },
                  { value: 'duel' as RampName, label: 'Blood' },
                ]}
                value={ramp}
                onChange={setRamp}
              />
              <Slider
                label="Spot size"
                min={5}
                max={40}
                value={radius}
                onChange={setRadiusOverride}
                format={(v) => `${v}px`}
              />
              <Slider
                label="Hotspot focus"
                min={0.9}
                max={1}
                step={0.005}
                value={percentile}
                onChange={setPercentile}
                format={(v) => (v >= 0.999 ? 'Peaks' : v >= 0.985 ? 'Balanced' : 'Broad')}
              />
              <Slider
                label="Opacity"
                min={0.3}
                max={1}
                step={0.05}
                value={intensity}
                onChange={setIntensity}
                format={(v) => `${Math.round(v * 100)}%`}
              />
              {(radiusOverride !== null || percentile !== DEFAULT_PERCENTILE) && (
                <button
                  type="button"
                  className="linkbtn"
                  onClick={() => {
                    setRadiusOverride(null)
                    setPercentile(DEFAULT_PERCENTILE)
                  }}
                >
                  Reset to defaults
                </button>
              )}
            </Panel>
          )}
        </aside>
      </main>
      </>
      )}
    </div>
  )
}

function PlantSide({
  data,
  selected,
  onSelect,
}: {
  data: PlantsResponse
  selected: number | null
  onSelect: (id: number | null) => void
}) {
  if (data.summary.planted_rounds === 0) return <Empty>No plants in this selection.</Empty>
  return (
    <>
      <div className="statgrid">
        <StatTile label="Plants" value={num(data.summary.planted_rounds)} sub="rounds" />
        <StatTile
          label="Post-plant win"
          value={pct(data.summary.plant_win_rate)}
          sub="attacker rounds won"
          tone="good"
        />
        <StatTile label="Defused" value={num(data.summary.defused ?? 0)} sub="spikes" tone="warn" />
        <StatTile label="Spots" value={num(data.summary.spots ?? 0)} sub="positions" />
      </div>

      <Panel title="Plant spots" subtitle="Click a row to isolate it on the map">
        <ul className="spotrows">
          {data.spots.map((s, i) => (
            <li key={s.id}>
              <button
                type="button"
                className={`spotrow${selected === s.id ? ' is-active' : ''}${
                  s.reliable ? '' : ' is-thin'
                }`}
                onClick={() => onSelect(selected === s.id ? null : s.id)}
              >
                <span
                  className="spotrow__pin"
                  style={{ background: winRateCss(s.win_rate, s.reliable) }}
                >
                  {i + 1}
                </span>
                <span className="spotrow__meta">
                  <strong>
                    {pct(s.win_rate)} <em>site {s.site}</em>
                  </strong>
                  <em>
                    {num(s.plants)} plants · {num(s.wins)}W {num(s.losses)}L
                    {s.reliable ? '' : ' · low sample'}
                  </em>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel title="By site">
        <ul className="ranklist">
          {data.sites.map((s) => (
            <li key={s.site}>
              <span className="ranklist__label">
                <strong>Site {s.site}</strong>
                <em>avg {(s.avg_plant_time_ms / 1000).toFixed(1)}s</em>
              </span>
              <div className="bar">
                <span
                  style={{ width: `${s.win_rate * 100}%`, background: winRateCss(s.win_rate) }}
                />
              </div>
              <span className="ranklist__value">
                {pct(s.win_rate)}
                <em>{num(s.plants)}</em>
              </span>
            </li>
          ))}
        </ul>
      </Panel>
    </>
  )
}

function InsightsPanel({ data, loading }: { data: InsightsResponseV2 | null; loading: boolean }) {
  if (loading && !data) return <Empty>Crunching…</Empty>
  if (!data) return <Empty>No data.</Empty>
  const maxAgent = data.agents[0]?.kills ?? 1
  const maxWeapon = data.weapons[0]?.kills ?? 1
  return (
    <div className="insights__grid">
      <Panel title="Agents" subtitle="Kills, and how often those deaths were traded">
        <ul className="ranklist">
          {data.agents.slice(0, 16).map((a) => (
            <li key={a.agent}>
              <span className="ranklist__label">
                <strong>{a.agent}</strong>
                <em>{pct(a.traded / Math.max(1, a.kills))} traded</em>
              </span>
              <div className="bar">
                <span
                  style={{
                    width: `${(a.kills / maxAgent) * 100}%`,
                    background: 'linear-gradient(90deg,#ff4655,#ff9d8a)',
                  }}
                />
              </div>
              <span className="ranklist__value">{num(a.kills)}</span>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel title="Weapons">
        <ul className="ranklist">
          {data.weapons.slice(0, 16).map((w) => (
            <li key={w.weapon}>
              <span className="ranklist__label">
                <strong>{w.weapon}</strong>
              </span>
              <div className="bar">
                <span
                  style={{
                    width: `${(w.kills / maxWeapon) * 100}%`,
                    background: 'linear-gradient(90deg,#b28dff,#ddd0ff)',
                  }}
                />
              </div>
              <span className="ranklist__value">{num(w.kills)}</span>
            </li>
          ))}
        </ul>
      </Panel>

      <Panel title="Utility kills" subtitle="Abilities that finished the job">
        {data.abilities.length === 0 ? (
          <Empty>None in this selection.</Empty>
        ) : (
          <ul className="ranklist">
            {data.abilities.slice(0, 16).map((a) => (
              <li key={`${a.agent}-${a.ability}`}>
                <span className="ranklist__label">
                  <strong>{a.ability}</strong>
                  <em>{a.agent}</em>
                </span>
                <div className="bar">
                  <span
                    style={{
                      width: `${(a.kills / data.abilities[0].kills) * 100}%`,
                      background: 'linear-gradient(90deg,#63d471,#b6f09c)',
                    }}
                  />
                </div>
                <span className="ranklist__value">{num(a.kills)}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  )
}
