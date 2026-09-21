import { useCallback, useEffect, useMemo, useState } from 'react'
import { MapCanvas, type RenderMode } from './components/MapCanvas'
import { TimeSlider } from './components/TimeSlider'
import { Bar, ChipGroup, Empty, Field, Panel, Slider, StatTile, Toggle } from './components/Controls'
import { ImportPanel } from './components/ImportPanel'
import { HeatLegend } from './components/HeatLegend'
import { InsightsView } from './components/InsightsView'
import { api, ApiError, type QueryFilters } from './lib/api'
import type { RampName } from './lib/heatmap'
import type {
  AgentInfo,
  InsightsResponse,
  KillsResponse,
  MapRow,
  MatchSummary,
  PlantsResponse,
  UtilityResponse,
} from './lib/types'

type View = 'kills' | 'utility' | 'plants' | 'insights'

const ROUND_MAX_MS = 120_000
const VIEWS: { id: View; label: string; blurb: string }[] = [
  { id: 'kills', label: 'Kill heatmap', blurb: 'Where duels are won and lost' },
  { id: 'utility', label: 'Utility damage', blurb: 'Kills from damaging abilities' },
  { id: 'plants', label: 'Plants & win rate', blurb: 'Which spike positions actually win' },
  { id: 'insights', label: 'Advanced stats', blurb: 'Trades, openings, ranges, zones' },
]

const pct = (v: number) => `${Math.round(v * 100)}%`

export default function App() {
  const [maps, setMaps] = useState<MapRow[]>([])
  const [matches, setMatches] = useState<MatchSummary[]>([])
  const [agents, setAgents] = useState<AgentInfo[]>([])
  const [liveSources, setLiveSources] = useState<Record<string, boolean>>({})

  const [view, setView] = useState<View>('kills')
  const [mapName, setMapName] = useState<string>('')
  const [selectedMatches, setSelectedMatches] = useState<string[]>([])

  // filters
  const [agentFilter, setAgentFilter] = useState<string[]>([])
  const [sides, setSides] = useState<string[]>([])
  const [timeRange, setTimeRange] = useState<[number, number]>([0, ROUND_MAX_MS])
  const [tradedOnly, setTradedOnly] = useState(false)
  const [untradedOnly, setUntradedOnly] = useState(false)
  const [firstBloodOnly, setFirstBloodOnly] = useState(false)
  const [postPlantOnly, setPostPlantOnly] = useState(false)
  const [highlightTraded, setHighlightTraded] = useState(true)
  const [tradeWindow, setTradeWindow] = useState(3000)
  const [tradeRadius, setTradeRadius] = useState(3000)

  // render options
  const [renderMode, setRenderMode] = useState<RenderMode>('heatmap')
  const [anchor, setAnchor] = useState<'victim' | 'killer'>('victim')
  const [ramp, setRamp] = useState<RampName>('inferno')
  // null = follow the auto-scaled default; a number means the user has
  // taken manual control of the slider.
  const [radiusOverride, setRadiusOverride] = useState<number | null>(null)
  const [intensity, setIntensity] = useState(0.95)
  const [percentile, setPercentile] = useState(0.985)
  const [showCallouts, setShowCallouts] = useState(false)
  const [clusterRadius, setClusterRadius] = useState(800)

  // data
  const [kills, setKills] = useState<KillsResponse | null>(null)
  const [utility, setUtility] = useState<UtilityResponse | null>(null)
  const [plants, setPlants] = useState<PlantsResponse | null>(null)
  const [insights, setInsights] = useState<InsightsResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refreshCatalog = useCallback(async () => {
    const [mapsRes, matchesRes, health] = await Promise.all([
      api.maps(),
      api.matches(),
      api.health(),
    ])
    setMaps(mapsRes.maps)
    setMatches(matchesRes.matches)
    setLiveSources(health.live_sources)
    setMapName((current) => current || mapsRes.maps[0]?.map_name || '')
  }, [])

  useEffect(() => {
    refreshCatalog().catch((e) => setError(e instanceof Error ? e.message : String(e)))
    api
      .reference()
      .then((r) => setAgents(r.agents))
      .catch(() => setAgents([]))
  }, [refreshCatalog])

  // Matches available on the selected map, and the agents actually played
  // there -- filtering by an agent nobody picked is a dead end.
  const mapMatches = useMemo(
    () => matches.filter((m) => !mapName || m.map_name === mapName),
    [matches, mapName],
  )
  const activeMatchIds = useMemo(
    () => (selectedMatches.length ? selectedMatches : mapMatches.map((m) => m.match_id)),
    [selectedMatches, mapMatches],
  )
  const playedAgents = useMemo(() => {
    const names = new Set<string>()
    for (const m of mapMatches) {
      if (selectedMatches.length && !selectedMatches.includes(m.match_id)) continue
      for (const p of m.players) if (p.agent) names.add(p.agent)
    }
    return agents.filter((a) => names.has(a.name))
  }, [mapMatches, selectedMatches, agents])

  const roundBased = mapMatches.some((m) => m.is_round_based)

  const filters: QueryFilters = useMemo(
    () => ({
      match_ids: activeMatchIds,
      map_name: mapName || undefined,
      agents: agentFilter,
      sides,
      time_start: timeRange[0] > 0 ? timeRange[0] : undefined,
      time_end: timeRange[1] < ROUND_MAX_MS ? timeRange[1] : undefined,
      traded_only: tradedOnly,
      untraded_only: untradedOnly,
      first_blood_only: firstBloodOnly,
      post_plant_only: postPlantOnly,
      anchor,
      trade_window: tradeWindow,
      trade_radius: tradeRadius,
    }),
    [
      activeMatchIds, mapName, agentFilter, sides, timeRange, tradedOnly,
      untradedOnly, firstBloodOnly, postPlantOnly, anchor, tradeWindow, tradeRadius,
    ],
  )

  useEffect(() => {
    if (!mapName || activeMatchIds.length === 0) return
    let cancelled = false
    setLoading(true)
    setError(null)

    const run = async () => {
      try {
        if (view === 'kills') {
          const res = await api.kills(filters)
          if (!cancelled) setKills(res)
        } else if (view === 'utility') {
          const res = await api.utility(filters)
          if (!cancelled) setUtility(res)
        } else if (view === 'plants') {
          const res = await api.plants({
            match_ids: activeMatchIds,
            map_name: mapName,
            cluster_radius: clusterRadius,
          })
          if (!cancelled) setPlants(res)
        } else {
          const res = await api.insights(filters)
          if (!cancelled) setInsights(res)
        }
      } catch (e) {
        if (cancelled) return
        const message =
          e instanceof ApiError && e.status === 404
            ? 'No matches for this selection yet — import one to get started.'
            : e instanceof Error
              ? e.message
              : String(e)
        setError(message)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [view, filters, mapName, activeMatchIds, clusterRadius])

  const toggle = <T,>(list: T[], value: T): T[] =>
    list.includes(value) ? list.filter((v) => v !== value) : [...list, value]

  const activeMap =
    view === 'plants' ? (plants?.map ?? null)
    : view === 'utility' ? (utility?.map ?? null)
    : (kills?.map ?? plants?.map ?? utility?.map ?? null)

  const shownKills =
    view === 'utility' ? (utility?.points ?? []) : view === 'kills' ? (kills?.points ?? []) : []
  const stats = view === 'utility' ? utility?.stats : kills?.stats

  // The right blur depends on how much data is on screen: a handful of
  // points needs a wide splat to read at all, while thousands need a tight
  // one or every distinct angle merges into a single mass.
  const autoRadius = useMemo(() => {
    const n = shownKills.length
    if (n === 0) return 14
    if (n < 60) return 24
    if (n < 250) return 18
    if (n < 1200) return 14
    if (n < 5000) return 11
    return 9
  }, [shownKills.length])
  const radius = radiusOverride ?? autoRadius

  const resetFilters = () => {
    setAgentFilter([])
    setSides([])
    setTimeRange([0, ROUND_MAX_MS])
    setTradedOnly(false)
    setUntradedOnly(false)
    setFirstBloodOnly(false)
    setPostPlantOnly(false)
  }

  const activeFilterCount =
    agentFilter.length + sides.length +
    (timeRange[0] > 0 || timeRange[1] < ROUND_MAX_MS ? 1 : 0) +
    [tradedOnly, untradedOnly, firstBloodOnly, postPlantOnly].filter(Boolean).length

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

        <nav className="viewnav">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              type="button"
              className={view === v.id ? 'is-active' : ''}
              onClick={() => setView(v.id)}
            >
              <strong>{v.label}</strong>
              <span>{v.blurb}</span>
            </button>
          ))}
        </nav>
      </header>

      <div className="layout">
        <aside className="sidebar">
          <Panel title="Map" subtitle={`${maps.length} loaded`}>
            <div className="maplist">
              {maps.map((m) => (
                <button
                  key={m.map_name}
                  type="button"
                  className={`maplist__item${mapName === m.map_name ? ' is-active' : ''}`}
                  onClick={() => {
                    setMapName(m.map_name)
                    setSelectedMatches([])
                  }}
                >
                  {m.minimap && <img src={m.minimap} alt="" loading="lazy" />}
                  <span className="maplist__meta">
                    <strong>{m.map_name}</strong>
                    <em>
                      {m.matches} {m.matches === 1 ? 'match' : 'matches'} · {m.kills} kills
                    </em>
                  </span>
                </button>
              ))}
              {maps.length === 0 && <Empty>No matches loaded yet.</Empty>}
            </div>
          </Panel>

          {mapMatches.length > 1 && (
            <Panel title="Matches" subtitle="Combine several for a bigger sample">
              <div className="matchlist">
                <button
                  type="button"
                  className={`matchrow${selectedMatches.length === 0 ? ' is-active' : ''}`}
                  onClick={() => setSelectedMatches([])}
                >
                  All {mapMatches.length} matches
                </button>
                {mapMatches.map((m) => (
                  <button
                    key={m.match_id}
                    type="button"
                    className={`matchrow${selectedMatches.includes(m.match_id) ? ' is-active' : ''}`}
                    onClick={() => setSelectedMatches((cur) => toggle(cur, m.match_id))}
                  >
                    <span>{m.mode_raw || m.mode}</span>
                    <em>
                      {m.kills} kills · {m.rounds} rounds
                    </em>
                  </button>
                ))}
              </div>
            </Panel>
          )}

          <Panel
            title="Filters"
            subtitle={activeFilterCount ? `${activeFilterCount} active` : 'Showing everything'}
            actions={
              activeFilterCount > 0 ? (
                <button type="button" className="linkbtn" onClick={resetFilters}>
                  Reset
                </button>
              ) : undefined
            }
          >
            <Field label="Agent">
              <ChipGroup
                options={playedAgents.map((a) => ({ value: a.name, label: a.name }))}
                selected={agentFilter}
                onToggle={(v) => setAgentFilter((cur) => toggle(cur, v))}
                onClear={() => setAgentFilter([])}
                icons={Object.fromEntries(playedAgents.map((a) => [a.name, a.icon]))}
              />
            </Field>

            {roundBased && (
              <Field label="Side">
                <ChipGroup
                  options={[
                    { value: 'attack', label: 'Attack' },
                    { value: 'defense', label: 'Defense' },
                  ]}
                  selected={sides}
                  onToggle={(v) => setSides((cur) => toggle(cur, v))}
                  onClear={() => setSides([])}
                />
              </Field>
            )}

            <TimeSlider
              max={ROUND_MAX_MS}
              value={timeRange}
              onChange={setTimeRange}
              histogram={kills?.histogram ?? []}
              disabled={view === 'plants'}
            />

            <div className="togglegrid">
              <Toggle
                label="Traded only"
                checked={tradedOnly}
                onChange={(v) => {
                  setTradedOnly(v)
                  if (v) setUntradedOnly(false)
                }}
                hint="Deaths a teammate avenged within the trade window"
              />
              <Toggle
                label="Untraded only"
                checked={untradedOnly}
                onChange={(v) => {
                  setUntradedOnly(v)
                  if (v) setTradedOnly(false)
                }}
                hint="Deaths that went unpunished"
              />
              <Toggle label="Opening duels" checked={firstBloodOnly} onChange={setFirstBloodOnly} />
              {roundBased && (
                <Toggle label="Post-plant" checked={postPlantOnly} onChange={setPostPlantOnly} />
              )}
            </div>
          </Panel>

          <Panel title="Trade rule" subtitle="What counts as a trade">
            <Slider
              label="Window"
              min={500}
              max={8000}
              step={250}
              value={tradeWindow}
              onChange={setTradeWindow}
              format={(v) => `${(v / 1000).toFixed(2)}s`}
            />
            <Slider
              label="Max distance"
              min={0}
              max={8000}
              step={250}
              value={tradeRadius}
              onChange={setTradeRadius}
              format={(v) => (v === 0 ? 'Anywhere' : `${(v / 100).toFixed(0)}m`)}
            />
            <p className="hint">
              A death counts as traded when a teammate kills the killer within the window, near
              where the death happened.
            </p>
          </Panel>

          <ImportPanel liveSources={liveSources} onImported={refreshCatalog} />
        </aside>

        <main className="main">
          {error && <div className="banner banner--error">{error}</div>}

          {view === 'insights' ? (
            <InsightsView data={insights} loading={loading} />
          ) : (
            <>
              <div className="stage">
                <div className="stage__map">
                  <MapCanvas
                    map={activeMap}
                    kills={shownKills}
                    plants={view === 'plants' ? (plants?.points ?? []) : []}
                    spots={view === 'plants' ? (plants?.spots ?? []) : []}
                    mode={view === 'plants' ? 'points' : renderMode}
                    ramp={view === 'utility' ? 'toxic' : ramp}
                    radius={radius}
                    intensity={intensity}
                    anchor={anchor}
                    highlightTraded={highlightTraded}
                    showCallouts={showCallouts}
                    showSpots={view === 'plants'}
                    loading={loading}
                    percentile={percentile}
                  />
                  {view !== 'plants' && renderMode === 'heatmap' && (
                    <HeatLegend
                      ramp={view === 'utility' ? 'toxic' : ramp}
                      label={anchor === 'killer' ? 'Kills from here' : 'Deaths here'}
                    />
                  )}
                </div>

                <div className="stage__side">
                  {view === 'plants' ? (
                    <PlantSidebar
                      data={plants}
                      clusterRadius={clusterRadius}
                      onClusterRadius={setClusterRadius}
                    />
                  ) : (
                    <>
                      <div className="statgrid">
                        <StatTile
                          label={view === 'utility' ? 'Utility kills' : 'Kills shown'}
                          value={String(stats?.total ?? 0)}
                          sub={`${activeMatchIds.length} match${activeMatchIds.length === 1 ? '' : 'es'}`}
                        />
                        <StatTile
                          label="Traded"
                          value={stats ? pct(stats.trade_rate) : '—'}
                          sub={stats?.avg_trade_latency_ms ? `avg ${(stats.avg_trade_latency_ms / 1000).toFixed(2)}s` : 'of deaths avenged'}
                          tone="good"
                        />
                        <StatTile
                          label="Opening kills"
                          value={String(stats?.first_bloods ?? 0)}
                          sub="first duel of a round"
                        />
                        <StatTile
                          label="Round win rate"
                          value={stats ? pct(stats.round_win_rate) : '—'}
                          sub="for the killer's team"
                          tone="hot"
                        />
                      </div>

                      <Panel
                        title="Display"
                        actions={
                          radiusOverride !== null ? (
                            <button
                              type="button"
                              className="linkbtn"
                              onClick={() => setRadiusOverride(null)}
                            >
                              Auto size
                            </button>
                          ) : undefined
                        }
                      >
                        <Field label="Render">
                          <ChipGroup
                            options={[
                              { value: 'heatmap', label: 'Heatmap' },
                              { value: 'points', label: 'Points' },
                              { value: 'duels', label: 'Duel lines' },
                            ]}
                            selected={[renderMode]}
                            onToggle={(v) => setRenderMode(v as RenderMode)}
                          />
                        </Field>
                        <Field label="Plot position of" hint="Where the victim died, or where the killer stood">
                          <ChipGroup
                            options={[
                              { value: 'victim', label: 'Deaths' },
                              { value: 'killer', label: 'Killer spots' },
                            ]}
                            selected={[anchor]}
                            onToggle={(v) => setAnchor(v as 'victim' | 'killer')}
                          />
                        </Field>
                        {view !== 'utility' && (
                          <Field label="Palette">
                            <ChipGroup
                              options={[
                                { value: 'inferno', label: 'Inferno' },
                                { value: 'ice', label: 'Ice' },
                                { value: 'toxic', label: 'Toxic' },
                                { value: 'duel', label: 'Blood' },
                              ]}
                              selected={[ramp]}
                              onToggle={(v) => setRamp(v as RampName)}
                            />
                          </Field>
                        )}
                        <Slider
                          label="Spot size"
                          min={5}
                          max={40}
                          value={radius}
                          onChange={setRadiusOverride}
                          format={(v) =>
                            radiusOverride === null ? `${v}px · auto` : `${v}px`
                          }
                        />
                        <Slider
                          label="Hotspot focus"
                          min={0.9}
                          max={1}
                          step={0.005}
                          value={percentile}
                          onChange={setPercentile}
                          format={(v) =>
                            v >= 0.999 ? 'Peaks only' : v >= 0.985 ? 'Balanced' : 'Broad'
                          }
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
                        <div className="togglegrid">
                          <Toggle
                            label="Highlight trades"
                            checked={highlightTraded}
                            onChange={setHighlightTraded}
                            hint="Colour traded deaths green in point and duel views"
                          />
                          <Toggle label="Callouts" checked={showCallouts} onChange={setShowCallouts} />
                        </div>
                      </Panel>

                      {view === 'utility' && utility && (
                        <Panel title="Ability breakdown" subtitle="Damaging util that finished a kill">
                          {utility.report.abilities.length === 0 ? (
                            <Empty>No utility kills in this selection.</Empty>
                          ) : (
                            <ul className="ranklist">
                              {utility.report.abilities.map((a) => (
                                <li key={`${a.agent}-${a.ability}`}>
                                  {a.agent_icon && <img src={a.agent_icon} alt="" loading="lazy" />}
                                  <span className="ranklist__label">
                                    <strong>{a.ability}</strong>
                                    <em>{a.agent}</em>
                                  </span>
                                  <Bar
                                    value={a.kills}
                                    max={utility.report.abilities[0].kills}
                                    tone="linear-gradient(90deg,#63d471,#b6f09c)"
                                  />
                                  <span className="ranklist__value">{a.kills}</span>
                                </li>
                              ))}
                            </ul>
                          )}
                        </Panel>
                      )}
                    </>
                  )}
                </div>
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  )
}

function PlantSidebar({
  data,
  clusterRadius,
  onClusterRadius,
}: {
  data: PlantsResponse | null
  clusterRadius: number
  onClusterRadius: (v: number) => void
}) {
  if (!data) return <Empty>Loading plant data…</Empty>
  if (data.summary.planted_rounds === 0)
    return <Empty>No spike plants in this selection (no plants recorded).</Empty>

  const spots = [...data.spots].sort((a, b) => b.plants - a.plants)
  return (
    <>
      <div className="statgrid">
        <StatTile label="Plants" value={String(data.summary.planted_rounds)} sub="rounds with a plant" />
        <StatTile
          label="Post-plant win"
          value={pct(data.summary.plant_win_rate)}
          sub="attacker rounds won"
          tone="good"
        />
        <StatTile label="Defused" value={String(data.summary.defused ?? 0)} sub="spikes defused" tone="warn" />
        <StatTile label="Spots" value={String(data.summary.spots ?? spots.length)} sub="distinct positions" />
      </div>

      <Panel title="Plant spots by win rate" subtitle="Nearby plants grouped into one spot">
        <Slider
          label="Grouping radius"
          min={200}
          max={2500}
          step={100}
          value={clusterRadius}
          onChange={onClusterRadius}
          format={(v) => `${(v / 100).toFixed(0)}m`}
        />
        <ul className="spotlist">
          {spots.map((s) => (
            <li key={s.id} className={s.reliable ? '' : 'is-thin'}>
              <span className="spotlist__site">{s.site}</span>
              <span className="spotlist__meta">
                <strong>{pct(s.win_rate)} win</strong>
                <em>
                  {s.plants} plants · {s.wins}W {s.losses}L
                  {s.reliable ? '' : ' · low sample'}
                </em>
              </span>
              <Bar
                value={s.win_rate}
                max={1}
                tone={
                  s.win_rate >= 0.5
                    ? 'linear-gradient(90deg,#2ec27e,#7ee2b8)'
                    : 'linear-gradient(90deg,#c2453e,#f08c87)'
                }
              />
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
                <em>avg plant {(s.avg_plant_time_ms / 1000).toFixed(1)}s</em>
              </span>
              <Bar
                value={s.win_rate}
                max={1}
                tone="linear-gradient(90deg,#4c8dff,#8fb8ff)"
              />
              <span className="ranklist__value">
                {pct(s.win_rate)}
                <em>{s.plants}×</em>
              </span>
            </li>
          ))}
        </ul>
      </Panel>
    </>
  )
}
