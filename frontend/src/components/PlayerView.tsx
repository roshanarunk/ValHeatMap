import { useCallback, useEffect, useMemo, useState } from 'react'
import { MapCanvas, type RenderMode } from './MapCanvas'
import {
  ControlBar,
  ControlGroup,
  MultiSelect,
  RotateControl,
  SegmentedControl,
  Select,
} from './MapControls'
import { Empty, Panel, Slider, StatTile, Toggle } from './Controls'
import { DivergingLegend, HeatLegend } from './HeatLegend'
import { TimeSlider } from './TimeSlider'
import {
  api,
  ApiError,
  type MatchDetail,
  type PlayerMatch,
  type PlayerSummary,
} from '../lib/api'
import type { QueryFilters } from '../lib/api'
import type { RampName } from '../lib/heatmap'
import type { Facets, KillPoint, KillsResponseV2 } from '../lib/types'

const ROUND_MAX_MS = 120_000
/** Matches the main view's default: tight splats keep spots distinct. */
const PLAYER_RADIUS = 5
/** "Balanced" on the hotspot-focus scale, matching the global views. */
const DEFAULT_PERCENTILE = 0.985
const STORAGE_KEY = 'valheatmap.riot_id'

type Role = 'killer' | 'victim' | 'either'

const ROLES: { id: Role; label: string }[] = [
  { id: 'killer', label: 'My kills' },
  { id: 'victim', label: 'My deaths' },
  { id: 'either', label: 'Both' },
]

const num = (v: number) => v.toLocaleString()

// Valorant's own shop categories, matching the global filters.
const WEAPON_GROUPS: { label: string; values: string[] }[] = [
  { label: 'Rifles', values: ['Vandal', 'Phantom', 'Bulldog', 'Guardian'] },
  { label: 'Snipers', values: ['Operator', 'Marshal', 'Outlaw'] },
  { label: 'SMGs', values: ['Spectre', 'Stinger'] },
  { label: 'Pistols', values: ['Classic', 'Shorty', 'Frenzy', 'Ghost', 'Sheriff'] },
  { label: 'Shotguns', values: ['Bucky', 'Judge'] },
  { label: 'Heavy', values: ['Ares', 'Odin'] },
]

function when(ts: number | null): string {
  if (!ts) return ''
  // started_at is milliseconds for matches, seconds for registrations;
  // both land in a sane range once normalised.
  const ms = ts > 1e12 ? ts : ts * 1000
  const days = (Date.now() - ms) / 86_400_000
  if (days < 1) return 'today'
  if (days < 2) return 'yesterday'
  if (days < 30) return `${Math.floor(days)}d ago`
  return new Date(ms).toLocaleDateString()
}

/**
 * A match's kills, in the shape MapCanvas already knows how to draw.
 *
 * `mine` is set here rather than left to the canvas: match review carries
 * real puuids, while the aggregate endpoint sends the flag directly, and
 * having one field means the canvas never has to know the difference.
 */
function toKillPoints(detail: MatchDetail, puuid?: string): KillPoint[] {
  return detail.kills.map((k) => ({
    ...(puuid ? { mine: k.killer === puuid } : {}),
    round: k.round,
    t: k.t_ms,
    killer: k.killer,
    victim: k.victim,
    killer_agent: k.killer_agent,
    victim_agent: k.victim_agent,
    killer_team: '',
    side: (k.side as KillPoint['side']) ?? 'none',
    weapon: k.weapon,
    ability: k.ability,
    type: (k.damage_type as KillPoint['type']) ?? 'unknown',
    traded: k.traded,
    trade_kill: false,
    first_blood: k.first_blood,
    post_plant: k.post_plant,
    round_won: k.round_won,
    victim_pos: { x: k.vx, y: k.vy },
    killer_pos: k.kx != null && k.ky != null ? { x: k.kx, y: k.ky } : undefined,
  }))
}

export function PlayerView() {
  const [riotId, setRiotId] = useState(() => localStorage.getItem(STORAGE_KEY) ?? '')
  const [input, setInput] = useState(riotId)
  const [player, setPlayer] = useState<PlayerSummary | null>(null)
  const [matches, setMatches] = useState<PlayerMatch[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // Aggregate view: every kill/death across their whole history on one map.
  const [aggMap, setAggMap] = useState('')
  const [aggRole, setAggRole] = useState<Role>('either')
  const [aggMode, setAggMode] = useState<RenderMode>('heatmap')
  const [agg, setAgg] = useState<KillsResponseV2 | null>(null)
  const [aggBusy, setAggBusy] = useState(false)
  const [aggTime, setAggTime] = useState<[number, number]>([0, ROUND_MAX_MS])

  // Filters. These narrow the *opponent* in each duel: on your kills that
  // is who you killed, on your deaths who killed you -- which is the
  // question worth asking ("how do I do against Jett?").
  const [agents, setAgents] = useState<string[]>([])
  const [roles, setRoles] = useState<string[]>([])
  const [weapons, setWeapons] = useState<string[]>([])
  const [sides, setSides] = useState<string[]>([])
  // Outcome filters, matching the global views. Traded/untraded are
  // mutually exclusive, so selecting one clears the other.
  const [tradedOnly, setTradedOnly] = useState(false)
  const [untradedOnly, setUntradedOnly] = useState(false)
  const [firstBloodOnly, setFirstBloodOnly] = useState(false)
  const [postPlantOnly, setPostPlantOnly] = useState(false)
  const [facets, setFacets] = useState<Facets | null>(null)

  // Rendering, same controls as the global views.
  const [ramp, setRamp] = useState<RampName>('inferno')
  const [radius, setRadius] = useState(PLAYER_RADIUS)
  const [percentile, setPercentile] = useState(DEFAULT_PERCENTILE)
  const [intensity, setIntensity] = useState(0.95)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshNote, setRefreshNote] = useState('')

  // Review state: one selected match, loaded on demand.
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<MatchDetail | null>(null)
  const [detailBusy, setDetailBusy] = useState(false)
  const [role, setRole] = useState<Role>('either')
  const [mode, setMode] = useState<RenderMode>('lines')
  const [roundFilter, setRoundFilter] = useState<number | ''>('')
  const [timeRange, setTimeRange] = useState<[number, number]>([0, ROUND_MAX_MS])
  const [rotation, setRotation] = useState(0)

  const load = useCallback(async (id: string) => {
    setBusy(true)
    setError('')
    try {
      const res = await api.playerMatches(id, 25)
      setPlayer(res.player)
      setMatches(res.matches)
      setRiotId(id)
      localStorage.setItem(STORAGE_KEY, id)
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Not tracked yet: register, then the crawler picks them up.
        try {
          const reg = await api.registerPlayer(id)
          setPlayer(reg.player)
          setMatches([])
          setRiotId(id)
          localStorage.setItem(STORAGE_KEY, id)
        } catch (regErr) {
          setError(regErr instanceof Error ? regErr.message : String(regErr))
        }
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy(false)
    }
  }, [])

  // Restore the last Riot ID on mount, so the tab opens on your own stats.
  useEffect(() => {
    if (riotId) void load(riotId)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Default to the map they play most, so the tab opens on real data.
  useEffect(() => {
    if (!aggMap && player?.maps?.length) setAggMap(player.maps[0].map_name)
  }, [player, aggMap])

  // Agent, weapon and role lists for the filter controls.
  useEffect(() => {
    api.facets().then(setFacets).catch(() => setFacets(null))
  }, [])

  const refresh = useCallback(async () => {
    if (!player) return
    setRefreshing(true)
    setRefreshNote('')
    try {
      const res = await api.refreshPlayer(player.riot_id)
      setPlayer(res.player)
      setRefreshNote(
        res.new_matches > 0
          ? `Added ${res.new_matches} match${res.new_matches === 1 ? '' : 'es'}.`
          : 'Already up to date.',
      )
      const list = await api.playerMatches(player.riot_id, 25)
      setMatches(list.matches)
    } catch (err) {
      setRefreshNote(err instanceof Error ? err.message : String(err))
    } finally {
      setRefreshing(false)
    }
  }, [player])

  // Everything narrowing the selection, in one place so the heatmap, the
  // stat tiles and the match list cannot drift out of step.
  const filters: QueryFilters = useMemo(() => {
    // Which end the agent/role filters apply to is the mirror of the
    // player's own: showing your kills they describe your victim, showing
    // your deaths they describe your killer.
    const opponentIsVictim = aggRole === 'killer'
    return {
      map_name: aggMap || undefined,
      time_start: aggTime[0] > 0 ? aggTime[0] : undefined,
      time_end: aggTime[1] < ROUND_MAX_MS ? aggTime[1] : undefined,
      sides,
      ...(opponentIsVictim
        ? { victim_agents: agents, victim_roles: roles }
        : { agents, roles }),
      weapons,
      traded_only: tradedOnly,
      untraded_only: untradedOnly,
      first_blood_only: firstBloodOnly,
      post_plant_only: postPlantOnly,
    }
  }, [
    aggMap, aggRole, aggTime, sides, agents, roles, weapons,
    tradedOnly, untradedOnly, firstBloodOnly, postPlantOnly,
  ])

  // Re-read the headline numbers whenever the selection changes, so the
  // tiles describe what is on screen. Keyed on riot_id rather than the
  // whole player object, which this effect itself replaces.
  const riotIdKey = player?.riot_id ?? ''
  useEffect(() => {
    if (!riotIdKey || !aggMap) return
    const controller = new AbortController()
    api
      .player(riotIdKey, filters, { signal: controller.signal })
      .then((p) => {
        if (!controller.signal.aborted) {
          setPlayer((cur) => (cur ? { ...cur, ...p } : p))
        }
      })
      .catch(() => {
        /* the heatmap request surfaces any error */
      })
    return () => {
      controller.abort()
    }
  }, [riotIdKey, aggMap, filters])

  const playerPuuid = player?.puuid
  useEffect(() => {
    if (!playerPuuid || !aggMap) return
    const controller = new AbortController()
    setAggBusy(true)
    api
      .killsV2(
        {
          ...filters,
          player: playerPuuid,
          player_role: aggRole,
        },
        { signal: controller.signal },
      )
      .then((res) => {
        if (!controller.signal.aborted) setAgg(res)
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!controller.signal.aborted) setAggBusy(false)
      })
    return () => {
      controller.abort()
    }
  }, [playerPuuid, aggMap, aggRole, filters])

  useEffect(() => {
    if (!selected) {
      setDetail(null)
      return
    }
    const controller = new AbortController()
    setDetailBusy(true)
    api
      .match(selected, { signal: controller.signal })
      .then((d) => {
        if (!controller.signal.aborted) {
          setDetail(d)
          setRoundFilter('')
          setTimeRange([0, ROUND_MAX_MS])
        }
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailBusy(false)
      })
    return () => {
      controller.abort()
    }
  }, [selected])

  // The kills drawn on the map, after the review filters.
  const points = useMemo(() => {
    if (!detail) return []
    let rows = toKillPoints(detail, player?.puuid)
    if (player) {
      if (role === 'killer') rows = rows.filter((k) => k.killer === player.puuid)
      else if (role === 'victim') rows = rows.filter((k) => k.victim === player.puuid)
      else rows = rows.filter((k) => k.killer === player.puuid || k.victim === player.puuid)
    }
    if (roundFilter !== '') rows = rows.filter((k) => k.round === roundFilter)
    return rows.filter((k) => k.t >= timeRange[0] && k.t <= timeRange[1])
  }, [detail, player, role, roundFilter, timeRange])

  const activeFilters =
    agents.length + roles.length + weapons.length + sides.length +
    [tradedOnly, untradedOnly, firstBloodOnly, postPlantOnly].filter(Boolean).length +
    (aggTime[0] > 0 || aggTime[1] < ROUND_MAX_MS ? 1 : 0)

  const clearFilters = useCallback(() => {
    setAgents([])
    setRoles([])
    setWeapons([])
    setSides([])
    setTradedOnly(false)
    setUntradedOnly(false)
    setFirstBloodOnly(false)
    setPostPlantOnly(false)
    setAggTime([0, ROUND_MAX_MS])
  }, [])

  // The ramp follows the view -- green for kills, red for deaths -- until
  // the user picks one, after which their choice sticks.
  const [rampTouched, setRampTouched] = useState(false)
  useEffect(() => {
    if (rampTouched) return
    setRamp(aggRole === 'killer' ? 'toxic' : aggRole === 'victim' ? 'duel' : 'inferno')
  }, [aggRole, rampTouched])

  // The agent/role filters describe the other player in the duel, and
  // which one that is flips with the view.
  const opponentLabel =
    aggRole === 'killer' ? 'Victim role' : aggRole === 'victim' ? 'Killer role' : 'Role'

  const rounds = useMemo(
    () => (detail ? Array.from(new Set(detail.kills.map((k) => k.round))).sort((a, b) => a - b) : []),
    [detail],
  )

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = input.trim()
    if (trimmed) void load(trimmed)
  }

  return (
    <div className="playerview">
      <form className="playerform" onSubmit={submit}>
        <input
          className="playerform__input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="YourName#TAG"
          aria-label="Riot ID"
          spellCheck={false}
        />
        <button className="playerform__go" type="submit" disabled={busy || !input.trim()}>
          {busy ? 'Loading…' : 'Look up'}
        </button>
      </form>

      {error && <p className="playerview__error">{error}</p>}

      {player && (
        <>
          <header className="playerhead">
            <div>
              <h2 className="playerhead__name">{player.riot_id}</h2>
              <p className="playerhead__meta">
                {player.region?.toUpperCase()} · {num(player.matches)} matches
                {player.crawled_at ? ` · updated ${when(player.crawled_at)}` : ''}
                {refreshNote ? ` · ${refreshNote}` : ''}
              </p>
            </div>
            <button
              type="button"
              className="playerhead__refresh"
              onClick={() => void refresh()}
              disabled={refreshing}
              title="Fetch any matches played since the last update"
            >
              {refreshing ? 'Refreshing…' : 'Refresh'}
            </button>
          </header>

          {player.matches === 0 ? (
            <Empty>
              <strong>Waiting for your matches.</strong> You are in the queue — tracked
              players are fetched first, so this usually takes a minute or two.
            </Empty>
          ) : (
            <>
              {activeFilters > 0 && (
                <p className="statgrid__scope">
                  Showing {aggMap || 'all maps'} · {activeFilters} filter
                  {activeFilters === 1 ? '' : 's'} applied
                </p>
              )}
              <div className="statgrid statgrid--wide">
                <StatTile
                  label="Kills"
                  value={num(player.kills)}
                  sub={`${num(player.matches)} matches`}
                />
                <StatTile label="Deaths" value={num(player.deaths)} />
                <StatTile
                  label="K/D"
                  value={player.kd.toFixed(2)}
                  tone={player.kd >= 1 ? 'good' : 'neutral'}
                />
                <StatTile
                  label="Opening duels"
                  value={`${Math.round(player.opening_win_rate * 100)}%`}
                  sub={`${num(player.first_bloods)} won · ${num(player.first_deaths)} lost`}
                  tone={player.opening_win_rate >= 0.5 ? 'good' : 'warn'}
                />
                <StatTile
                  label="Deaths traded"
                  value={`${Math.round(player.trade_rate * 100)}%`}
                  sub={`${num(player.untraded_deaths)} went unanswered`}
                  tone={player.trade_rate >= 0.5 ? 'good' : 'neutral'}
                />
                <StatTile
                  label="Trade kills"
                  value={num(player.trade_kills)}
                  sub="you avenged a team-mate"
                />
                <StatTile
                  label="Rounds won with a kill"
                  value={`${Math.round(player.kill_round_win_rate * 100)}%`}
                  sub={`${num(player.rounds_won_with_kill)} of ${num(player.kills)} kills`}
                  tone={player.kill_round_win_rate >= 0.5 ? 'good' : 'neutral'}
                />
                <StatTile
                  label="Multi-kill rounds"
                  value={num(player.multi_kill_rounds)}
                  sub={player.best_round >= 2 ? `best: ${player.best_round}K` : ''}
                  tone={player.best_round >= 5 ? 'hot' : 'neutral'}
                />
                <StatTile
                  label="Post-plant"
                  value={`${num(player.post_plant_kills)} / ${num(player.post_plant_deaths)}`}
                  sub="kills / deaths after the spike"
                />
              </div>

              {/* Career heatmap: every duel across their whole history on
                  one map, which is the view the match list cannot give. */}
              <section className="aggmap">
                <h3 className="matchlist__title">Your heatmap</h3>
                <p className="matchlist__hint">
                  Every duel you have played on this map, not just one game.
                </p>

                <div className="mapselect mapselect--player">
                  {player.maps.map((m) => (
                    <button
                      key={m.map_name}
                      type="button"
                      className={`mapchip${m.map_name === aggMap ? ' is-active' : ''}`}
                      onClick={() => setAggMap(m.map_name)}
                      title={`${num(m.kills)} kills · ${num(m.deaths)} deaths · ${num(
                        m.matches,
                      )} matches`}
                    >
                      <span>{m.map_name}</span>
                      <em className="mapchip__count">
                        {num(m.kills)}/{num(m.deaths)}
                      </em>
                    </button>
                  ))}
                </div>

                <ControlBar>
                  <ControlGroup label="Show">
                    <SegmentedControl
                      value={aggRole}
                      options={ROLES.map((r) => ({ value: r.id, label: r.label }))}
                      onChange={(v) => setAggRole(v as Role)}
                    />
                  </ControlGroup>
                  <ControlGroup label="Draw">
                    <SegmentedControl
                      value={aggMode}
                      options={[
                        { value: 'heatmap', label: 'Heat' },
                        { value: 'points', label: 'Points' },
                        { value: 'lines', label: 'Duels' },
                      ]}
                      onChange={(v) => setAggMode(v as RenderMode)}
                    />
                  </ControlGroup>
                  <ControlGroup label="Side">
                    <SegmentedControl
                      size="sm"
                      value={sides.length === 1 ? sides[0] : ''}
                      options={[
                        { value: '', label: 'Both' },
                        { value: 'attack', label: 'Attack' },
                        { value: 'defense', label: 'Defend' },
                      ]}
                      onChange={(v) => setSides(v ? [v] : [])}
                    />
                  </ControlGroup>
                  <ControlGroup label="Orient">
                    <RotateControl rotation={rotation} onChange={setRotation} />
                  </ControlGroup>
                </ControlBar>

                <ControlBar>
                  <ControlGroup label={opponentLabel}>
                    <MultiSelect
                      values={roles}
                      placeholder="Any role"
                      options={(facets?.roles ?? []).map((r) => ({
                        value: r.role,
                        label: r.role,
                      }))}
                      onChange={setRoles}
                    />
                  </ControlGroup>
                  <ControlGroup label="Agent">
                    <MultiSelect
                      values={agents}
                      placeholder="Any agent"
                      options={(facets?.agents ?? []).map((a) => ({
                        value: a.agent,
                        label: a.agent,
                        hint: a.role,
                      }))}
                      onChange={setAgents}
                    />
                  </ControlGroup>
                  <ControlGroup label="Weapon">
                    <MultiSelect
                      values={weapons}
                      placeholder="Any weapon"
                      options={(facets?.weapons ?? []).map((w) => ({
                        value: w.weapon,
                        label: w.weapon,
                      }))}
                      groups={WEAPON_GROUPS}
                      onChange={setWeapons}
                    />
                  </ControlGroup>
                  <ControlGroup label="Filter">
                    <div className="togglerow">
                      <Toggle
                        label="Traded"
                        checked={tradedOnly}
                        onChange={(v) => {
                          setTradedOnly(v)
                          if (v) setUntradedOnly(false)
                        }}
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
                  {activeFilters > 0 && (
                    <ControlGroup label=" ">
                      <button type="button" className="linkbtn" onClick={clearFilters}>
                        Clear {activeFilters} filter{activeFilters === 1 ? '' : 's'}
                      </button>
                    </ControlGroup>
                  )}
                </ControlBar>

                <div className="aggmap__layout">
                  <div className="aggmap__canvas">
                  <MapCanvas
                    map={agg?.map ?? null}
                    kills={agg?.points ?? []}
                    mode={aggMode}
                    // Green for kills, red for deaths, so the colour means
                    // the same thing here as in the combined view.
                    ramp={ramp}
                    radius={radius}
                    intensity={intensity}
                    percentile={percentile}
                    // Plot where the player was: their own position is the
                    // kill end when they got the kill, the death end when
                    // they died.
                    anchor={aggRole === 'killer' ? 'killer' : 'victim'}
                    // In "Both", this splits the field into wins and losses
                    // so a spot reads green or red rather than merely busy.
                    player={aggRole === 'either' ? player.puuid : undefined}
                    rotation={rotation}
                    loading={aggBusy}
                  />
                  <TimeSlider
                    value={aggTime}
                    max={ROUND_MAX_MS}
                    histogram={agg?.histogram}
                    onChange={setAggTime}
                  />
                  {aggMode === 'heatmap' &&
                    (aggRole === 'either' ? (
                      <DivergingLegend />
                    ) : (
                      <HeatLegend
                        ramp={ramp}
                        label={aggRole === 'killer' ? 'Your kills' : 'Your deaths'}
                      />
                    ))}
                  {agg && (
                    <p className="review__count">
                      {num(agg.total)} duels
                      {agg.sampled ? ` · showing ${num(agg.points.length)}` : ''}
                    </p>
                  )}
                  </div>

                  <Panel title="Rendering">
                    {/* The diverging view uses its own fixed scale, so a
                        ramp picker there would do nothing. */}
                    {aggRole !== 'either' && (
                      <SegmentedControl
                        size="sm"
                        value={ramp}
                        options={(['inferno', 'ice', 'toxic', 'duel'] as RampName[]).map(
                          (r) => ({
                            value: r,
                            label:
                              r === 'duel' ? 'Blood' : r[0].toUpperCase() + r.slice(1),
                          }),
                        )}
                        onChange={(v) => {
                          setRamp(v as RampName)
                          setRampTouched(true)
                        }}
                      />
                    )}
                    <Slider
                      label="Spot size"
                      min={5}
                      max={40}
                      value={radius}
                      onChange={setRadius}
                      format={(v) => `${v}px`}
                    />
                    <Slider
                      label="Hotspot focus"
                      min={0.9}
                      max={1}
                      step={0.005}
                      value={percentile}
                      onChange={setPercentile}
                      format={(v) =>
                        v >= 0.999 ? 'Peaks' : v >= 0.985 ? 'Balanced' : 'Broad'
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
                    {(radius !== PLAYER_RADIUS ||
                      percentile !== DEFAULT_PERCENTILE ||
                      intensity !== 0.95) && (
                      <button
                        type="button"
                        className="linkbtn"
                        onClick={() => {
                          setRadius(PLAYER_RADIUS)
                          setPercentile(DEFAULT_PERCENTILE)
                          setIntensity(0.95)
                        }}
                      >
                        Reset to defaults
                      </button>
                    )}
                  </Panel>
                </div>
              </section>

              <section className="matchlist">
                <h3 className="matchlist__title">Recent matches</h3>
                <p className="matchlist__hint">Pick one to review its duels on the map.</p>
                <ul className="matchlist__rows">
                  {matches.map((m) => (
                    <li key={m.match_id}>
                      <button
                        className={
                          m.match_id === selected
                            ? 'matchrow matchrow--active'
                            : 'matchrow'
                        }
                        onClick={() => setSelected(m.match_id === selected ? null : m.match_id)}
                      >
                        <span className="matchrow__map">{m.map_name}</span>
                        <span className="matchrow__agent">{m.agent}</span>
                        <span className="matchrow__kd">
                          {m.kills}/{m.deaths}
                        </span>
                        <span
                          className={
                            m.kd >= 1 ? 'matchrow__ratio is-good' : 'matchrow__ratio is-bad'
                          }
                        >
                          {m.kd.toFixed(2)}
                        </span>
                        <span className="matchrow__when">{when(m.started_at)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            </>
          )}
        </>
      )}

      {selected && (
        <section className="review">
          <ControlBar>
            <ControlGroup label="Show">
              <SegmentedControl
                value={role}
                options={ROLES.map((r) => ({ value: r.id, label: r.label }))}
                onChange={(v) => setRole(v as Role)}
              />
            </ControlGroup>
            <ControlGroup label="Draw">
              <SegmentedControl
                value={mode}
                options={[
                  { value: 'lines', label: 'Duels' },
                  { value: 'points', label: 'Points' },
                  { value: 'heatmap', label: 'Heat' },
                ]}
                onChange={(v) => setMode(v as RenderMode)}
              />
            </ControlGroup>
            <ControlGroup label="Round">
              <Select
                value={roundFilter === '' ? '' : String(roundFilter)}
                onChange={(v) => setRoundFilter(v === '' ? '' : Number(v))}
                placeholder="All rounds"
                options={rounds.map((r) => ({
                  value: String(r),
                  label: `Round ${r + 1}`,
                }))}
              />
            </ControlGroup>
            <ControlGroup label="Orient">
              <RotateControl rotation={rotation} onChange={setRotation} />
            </ControlGroup>
          </ControlBar>

          <div className="review__body">
            <div className="review__map">
              <MapCanvas
                map={detail?.map ?? null}
                kills={points}
                mode={mode}
                // Same convention as the career view above.
                ramp={role === 'killer' ? 'toxic' : 'duel'}
                radius={PLAYER_RADIUS}
                intensity={1}
                anchor={role === 'killer' ? 'killer' : 'victim'}
                player={role === 'either' ? player?.puuid : undefined}
                rotation={rotation}
                loading={detailBusy}
              />
              <TimeSlider
                value={timeRange}
                max={ROUND_MAX_MS}
                onChange={setTimeRange}
              />
            </div>

            {detail && (
              <Panel title="Scoreboard">
                <ul className="scoreboard">
                  {detail.scoreboard.map((p) => (
                    <li
                      key={p.puuid}
                      className={
                        p.puuid === player?.puuid ? 'scorerow scorerow--me' : 'scorerow'
                      }
                    >
                      <span className="scorerow__agent">{p.agent}</span>
                      <span className="scorerow__kd">
                        {p.kills}/{p.deaths}
                      </span>
                      <span className="scorerow__ratio">{p.kd.toFixed(2)}</span>
                    </li>
                  ))}
                </ul>
                <p className="review__count">
                  {num(points.length)} of {num(detail.kills.length)} duels shown
                </p>
              </Panel>
            )}
          </div>
        </section>
      )}

      {!player && !busy && !error && (
        <Empty>
          <strong>See your own heatmaps.</strong> Enter your Riot ID to track your
          matches and review recent games.
        </Empty>
      )}
    </div>
  )
}
