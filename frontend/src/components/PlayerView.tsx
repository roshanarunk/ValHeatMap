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
import { Empty, Panel, StatTile } from './Controls'
import { DivergingLegend, HeatLegend } from './HeatLegend'
import { TimeSlider } from './TimeSlider'
import {
  api,
  ApiError,
  type MatchDetail,
  type PlayerMatch,
  type PlayerSummary,
} from '../lib/api'
import type { Facets, KillPoint, KillsResponseV2 } from '../lib/types'

const ROUND_MAX_MS = 120_000
/** Matches the main view's default: tight splats keep spots distinct. */
const PLAYER_RADIUS = 5
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
  const [facets, setFacets] = useState<Facets | null>(null)
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

  useEffect(() => {
    if (!player || !aggMap) return
    let cancelled = false
    setAggBusy(true)
    // Which end the filters apply to is the mirror of the player's own:
    // showing your kills, the agent/weapon filters describe your victim;
    // showing your deaths, they describe your killer. "Both" cannot mean
    // one fixed end, so it filters the killer side, which is who beat you
    // or who you beat depending on the row.
    const opponentIsVictim = aggRole === 'killer'
    api
      .killsV2({
        map_name: aggMap,
        player: player.puuid,
        player_role: aggRole,
        time_start: aggTime[0],
        time_end: aggTime[1],
        sides,
        ...(opponentIsVictim
          ? { victim_agents: agents, victim_roles: roles }
          : { agents, roles }),
        weapons,
      })
      .then((res) => !cancelled && setAgg(res))
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setAggBusy(false))
    return () => {
      cancelled = true
    }
  }, [player, aggMap, aggRole, aggTime, agents, roles, weapons, sides])

  useEffect(() => {
    if (!selected) {
      setDetail(null)
      return
    }
    let cancelled = false
    setDetailBusy(true)
    api
      .match(selected)
      .then((d) => {
        if (!cancelled) {
          setDetail(d)
          setRoundFilter('')
          setTimeRange([0, ROUND_MAX_MS])
        }
      })
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : String(err)))
      .finally(() => !cancelled && setDetailBusy(false))
    return () => {
      cancelled = true
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
              <div className="statgrid">
                <StatTile label="Kills" value={num(player.kills)} />
                <StatTile label="Deaths" value={num(player.deaths)} />
                <StatTile label="K/D" value={player.kd.toFixed(2)} />
                <StatTile label="First bloods" value={num(player.first_bloods)} />
                <StatTile label="First deaths" value={num(player.first_deaths)} />
                <StatTile
                  label="Deaths traded"
                  value={`${Math.round(player.trade_rate * 100)}%`}
                  sub="a team-mate answered"
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
                  {(agents.length > 0 ||
                    roles.length > 0 ||
                    weapons.length > 0 ||
                    sides.length > 0) && (
                    <ControlGroup label=" ">
                      <button
                        type="button"
                        className="linkbtn"
                        onClick={() => {
                          setAgents([])
                          setRoles([])
                          setWeapons([])
                          setSides([])
                        }}
                      >
                        Clear filters
                      </button>
                    </ControlGroup>
                  )}
                </ControlBar>

                <div className="aggmap__canvas">
                  <MapCanvas
                    map={agg?.map ?? null}
                    kills={agg?.points ?? []}
                    mode={aggMode}
                    // Green for kills, red for deaths, so the colour means
                    // the same thing here as in the combined view.
                    ramp={aggRole === 'killer' ? 'toxic' : 'duel'}
                    radius={PLAYER_RADIUS}
                    intensity={1}
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
                        ramp={aggRole === 'killer' ? 'toxic' : 'duel'}
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
