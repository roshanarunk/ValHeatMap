import { useMemo } from 'react'
import type { RotationsResponse, RotationTransition } from '../lib/types'
import { StatTile } from './Controls'

export type RotationsScope = 'global' | 'player' | 'match'

interface RotationsPanelProps {
  data: RotationsResponse | null
  loading?: boolean
  scope: RotationsScope
  onScopeChange: (scope: RotationsScope) => void
  side: 'defense' | 'attack' | 'all'
  onSideChange: (side: 'defense' | 'attack' | 'all') => void
  player: string
  onPlayerChange: (player: string) => void
  agent: string
  onAgentChange: (agent: string) => void
  availableAgents?: string[]
  matchId: string
  onMatchIdChange: (matchId: string) => void
  team: string
  onTeamChange: (team: string) => void
  roundNum: number | ''
  onRoundNumChange: (roundNum: number | '') => void
  focusZone: string | null
  onFocusZoneChange: (zone: string | null) => void
  minCount: number
  onMinCountChange: (count: number) => void
  selectedRoute: { from: string; to: string } | null
  onSelectRoute: (route: { from: string; to: string } | null) => void
  selectedZone: string | null
  onSelectZone: (zone: string | null) => void
}

export function RotationsPanel({
  data,
  loading = false,
  scope,
  onScopeChange,
  side,
  onSideChange,
  player,
  onPlayerChange,
  agent,
  onAgentChange,
  availableAgents = [],
  matchId,
  onMatchIdChange,
  team,
  onTeamChange,
  roundNum,
  onRoundNumChange,
  focusZone,
  onFocusZoneChange,
  minCount,
  onMinCountChange,
  selectedRoute,
  onSelectRoute,
  selectedZone,
  onSelectZone,
}: RotationsPanelProps) {
  const transitions = data?.transitions ?? []
  const zones = data?.zones ?? []

  // Top corridor
  const topRoute = transitions[0] ?? null

  // Overall average duration
  const avgDuration = useMemo(() => {
    if (!transitions.length) return 0
    const weightedSum = transitions.reduce(
      (acc, t) => acc + t.avg_duration_s * t.count,
      0,
    )
    const totalCount = transitions.reduce((acc, t) => acc + t.count, 0)
    return totalCount ? +(weightedSum / totalCount).toFixed(1) : 0
  }, [transitions])

  // Site Entry / Retake Splits
  const siteSplits = useMemo(() => {
    const targets = ['A Site', 'B Site', 'C Site']
    const results: { site: string; routes: RotationTransition[]; total: number }[] = []

    for (const target of targets) {
      const incoming = transitions.filter((t) => t.to_zone === target)
      if (incoming.length > 0) {
        const total = incoming.reduce((acc, t) => acc + t.count, 0)
        results.push({
          site: target,
          routes: incoming.slice(0, 4),
          total,
        })
      }
    }
    return results
  }, [transitions])

  const bluePlayers = useMemo(
    () => (data?.match_players ?? []).filter((p) => p.team.toLowerCase() === 'blue'),
    [data?.match_players],
  )
  const redPlayers = useMemo(
    () => (data?.match_players ?? []).filter((p) => p.team.toLowerCase() === 'red'),
    [data?.match_players],
  )
  const selectedPlayer = useMemo(() => {
    if (!player || !data?.match_players) return null
    return data.match_players.find((p) => p.puuid === player || p.name === player) ?? null
  }, [player, data?.match_players])

  return (
    <aside className="rotations-panel">
      {/* Controls Bar */}
      <div className="rotations-controls">
        {/* Scope Selector */}
        <div className="rotations-controls__group">
          <label className="rotations-label">Scope</label>
          <div className="rotations-toggle-group">
            <button
              type="button"
              className={`rotations-toggle-btn ${scope === 'global' ? 'active' : ''}`}
              onClick={() => onScopeChange('global')}
            >
              Global Map
            </button>
            <button
              type="button"
              className={`rotations-toggle-btn ${scope === 'player' ? 'active' : ''}`}
              onClick={() => onScopeChange('player')}
            >
              Player Habits
            </button>
            <button
              type="button"
              className={`rotations-toggle-btn ${scope === 'match' ? 'active' : ''}`}
              onClick={() => onScopeChange('match')}
            >
              Match Analysis
            </button>
          </div>
        </div>

        {/* Player Scope Options */}
        {scope === 'player' && (
          <>
            <div className="rotations-controls__group">
              <label className="rotations-label">Player</label>
              <input
                type="text"
                className="rotations-input"
                placeholder="Name#TAG or PUUID"
                value={player}
                onChange={(e) => onPlayerChange(e.target.value)}
                style={{ width: '150px' }}
              />
            </div>
            <div className="rotations-controls__group">
              <label className="rotations-label">Agent</label>
              <select
                className="rotations-select"
                value={agent}
                onChange={(e) => onAgentChange(e.target.value)}
              >
                <option value="">All Agents</option>
                {availableAgents.map((ag) => (
                  <option key={ag} value={ag}>
                    {ag}
                  </option>
                ))}
              </select>
            </div>
          </>
        )}

        {/* Match Scope Options */}
        {scope === 'match' && (
          <>
            <div className="rotations-controls__group">
              <label className="rotations-label">Match ID</label>
              <input
                type="text"
                className="rotations-input"
                placeholder="Riot Match UUID"
                value={matchId}
                onChange={(e) => onMatchIdChange(e.target.value)}
                style={{ width: '140px' }}
              />
            </div>
            <div className="rotations-controls__group">
              <label className="rotations-label">Team</label>
              <div className="rotations-toggle-group">
                <button
                  type="button"
                  className={`rotations-toggle-btn ${team === '' ? 'active' : ''}`}
                  onClick={() => onTeamChange('')}
                >
                  Both
                </button>
                <button
                  type="button"
                  className={`rotations-toggle-btn ${team.toLowerCase() === 'blue' ? 'active' : ''}`}
                  onClick={() => onTeamChange('Blue')}
                >
                  Blue
                </button>
                <button
                  type="button"
                  className={`rotations-toggle-btn ${team.toLowerCase() === 'red' ? 'active' : ''}`}
                  onClick={() => onTeamChange('Red')}
                >
                  Red
                </button>
              </div>
            </div>
            <div className="rotations-controls__group">
              <label className="rotations-label">Round</label>
              <input
                type="number"
                min={1}
                max={30}
                className="rotations-input"
                placeholder="All"
                value={roundNum}
                onChange={(e) => onRoundNumChange(e.target.value ? Number(e.target.value) : '')}
                style={{ width: '55px' }}
              />
            </div>
          </>
        )}

        <div className="rotations-controls__group">
          <label className="rotations-label">Side</label>
          <div className="rotations-toggle-group">
            <button
              type="button"
              className={`rotations-toggle-btn ${side === 'defense' ? 'active' : ''}`}
              onClick={() => onSideChange('defense')}
            >
              Defense
            </button>
            <button
              type="button"
              className={`rotations-toggle-btn ${side === 'attack' ? 'active' : ''}`}
              onClick={() => onSideChange('attack')}
            >
              Attack
            </button>
            <button
              type="button"
              className={`rotations-toggle-btn ${side === 'all' ? 'active' : ''}`}
              onClick={() => onSideChange('all')}
            >
              All
            </button>
          </div>
        </div>

        <div className="rotations-controls__group">
          <label className="rotations-label">Focus Zone</label>
          <select
            className="rotations-select"
            value={focusZone ?? ''}
            onChange={(e) => onFocusZoneChange(e.target.value ? e.target.value : null)}
          >
            <option value="">All Zones ({zones.length})</option>
            {zones.map((z) => (
              <option key={z.id} value={z.id}>
                {z.name} ({z.total_traffic ?? 0})
              </option>
            ))}
          </select>
        </div>

        <div className="rotations-controls__group">
          <label className="rotations-label">Min Volume</label>
          <select
            className="rotations-select"
            value={minCount}
            onChange={(e) => onMinCountChange(Number(e.target.value))}
          >
            <option value={1}>≥ 1 transition</option>
            <option value={2}>≥ 2 transitions</option>
            <option value={3}>≥ 3 transitions</option>
            <option value={5}>≥ 5 transitions</option>
            <option value={10}>≥ 10 transitions</option>
            <option value={25}>≥ 25 transitions</option>
          </select>
        </div>
      </div>

      {/* Match Lineups & Team Rosters (Match Scope) */}
      {scope === 'match' && (data?.match_players?.length ?? 0) > 0 && (
        <div className="rotations-roster-section">
          <div className="rotations-roster-header">
            <div className="rotations-roster-title">
              <span>Match Lineups & Roles</span>
              <span className="rotations-roster-badge">{data?.match_players?.length} Players</span>
            </div>
            {player ? (
              <div className="rotations-player-active-filter">
                <span>
                  Filtering: <strong>{selectedPlayer?.agent || 'Player'}</strong>{' '}
                  {selectedPlayer?.name ? `(${selectedPlayer.name})` : ''}
                </span>
                <button
                  type="button"
                  className="rotations-clear-btn"
                  onClick={() => onPlayerChange('')}
                >
                  Clear (Show Team Flow)
                </button>
              </div>
            ) : (
              <div className="rotations-player-active-filter">
                <span className="rotations-subtle">
                  Showing average {team ? `${team} Team` : 'Match'} flow. Select a player to isolate habits:
                </span>
              </div>
            )}
          </div>

          <div className="rotations-roster">
            {/* Blue Team */}
            <div
              className={`rotations-team-card rotations-team-card--blue ${
                team.toLowerCase() === 'blue' && !player ? 'active' : ''
              }`}
            >
              <div className="rotations-team-header">
                <span className="rotations-team-title rotations-team-title--blue">
                  <span className="team-dot team-dot--blue" />
                  Blue Team ({bluePlayers.length})
                </span>
                <button
                  type="button"
                  className={`rotations-team-btn ${team.toLowerCase() === 'blue' && !player ? 'active' : ''}`}
                  onClick={() => {
                    onTeamChange('Blue')
                    onPlayerChange('')
                  }}
                  title="View Blue Team aggregate flow"
                >
                  All Blue
                </button>
              </div>

              <div className="rotations-team-players">
                {bluePlayers.map((p) => {
                  const isSelected = player === p.puuid || player === p.name
                  return (
                    <button
                      key={p.puuid || p.agent}
                      type="button"
                      className={`rotations-player-item rotations-player-item--blue ${
                        isSelected ? 'selected' : ''
                      }`}
                      onClick={() => {
                        if (isSelected) {
                          onPlayerChange('')
                        } else {
                          onPlayerChange(p.puuid)
                          if (team.toLowerCase() !== 'blue') {
                            onTeamChange('Blue')
                          }
                        }
                      }}
                      title={`Filter to ${p.name || p.agent}'s rotations`}
                    >
                      {p.icon ? (
                        <img src={p.icon} alt={p.agent} className="rotations-player-avatar" />
                      ) : (
                        <div className="rotations-player-avatar-fallback rotations-player-avatar-fallback--blue">
                          {p.agent[0]}
                        </div>
                      )}
                      <div className="rotations-player-info">
                        <div className="rotations-player-agent">
                          <span>{p.agent}</span>
                          {p.role && <span className="rotations-role-tag">{p.role}</span>}
                        </div>
                        <div className="rotations-player-name">{p.name || 'Anonymous'}</div>
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>

            {/* Red Team */}
            <div
              className={`rotations-team-card rotations-team-card--red ${
                team.toLowerCase() === 'red' && !player ? 'active' : ''
              }`}
            >
              <div className="rotations-team-header">
                <span className="rotations-team-title rotations-team-title--red">
                  <span className="team-dot team-dot--red" />
                  Red Team ({redPlayers.length})
                </span>
                <button
                  type="button"
                  className={`rotations-team-btn ${team.toLowerCase() === 'red' && !player ? 'active' : ''}`}
                  onClick={() => {
                    onTeamChange('Red')
                    onPlayerChange('')
                  }}
                  title="View Red Team aggregate flow"
                >
                  All Red
                </button>
              </div>

              <div className="rotations-team-players">
                {redPlayers.map((p) => {
                  const isSelected = player === p.puuid || player === p.name
                  return (
                    <button
                      key={p.puuid || p.agent}
                      type="button"
                      className={`rotations-player-item rotations-player-item--red ${
                        isSelected ? 'selected' : ''
                      }`}
                      onClick={() => {
                        if (isSelected) {
                          onPlayerChange('')
                        } else {
                          onPlayerChange(p.puuid)
                          if (team.toLowerCase() !== 'red') {
                            onTeamChange('Red')
                          }
                        }
                      }}
                      title={`Filter to ${p.name || p.agent}'s rotations`}
                    >
                      {p.icon ? (
                        <img src={p.icon} alt={p.agent} className="rotations-player-avatar" />
                      ) : (
                        <div className="rotations-player-avatar-fallback rotations-player-avatar-fallback--red">
                          {p.agent[0]}
                        </div>
                      )}
                      <div className="rotations-player-info">
                        <div className="rotations-player-agent">
                          <span>{p.agent}</span>
                          {p.role && <span className="rotations-role-tag">{p.role}</span>}
                        </div>
                        <div className="rotations-player-name">{p.name || 'Anonymous'}</div>
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Headline Metric Tiles */}
      <div className="rotations-stats-grid">
        <StatTile
          label="Total Rotations"
          value={loading ? '...' : (data?.total_transitions ?? 0).toLocaleString()}
          sub={
            scope === 'match'
              ? player && selectedPlayer
                ? `${selectedPlayer.agent} (${selectedPlayer.name || 'Player'}) • ${selectedPlayer.team}`
                : `In match ${matchId ? matchId.slice(0, 8) + '...' : ''} ${team ? `(${team})` : ''}`
              : scope === 'player'
                ? player
                  ? `${player}${agent ? ` (${agent})` : ''}`
                  : 'Enter a player Riot ID'
                : `Across ${zones.length} tactical zones`
          }
        />
        <StatTile
          label="Active Corridors"
          value={loading ? '...' : transitions.length.toString()}
          sub={focusZone ? `Filtered by ${focusZone}` : 'Major map corridors'}
        />
        <StatTile
          label="Top Route"
          value={
            loading || !topRoute
              ? '—'
              : `${topRoute.from_zone} ➔ ${topRoute.to_zone}`
          }
          sub={topRoute ? `${topRoute.count} times (${Math.round(topRoute.outgoing_share * 100)}% share)` : 'No route'}
        />
        <StatTile
          label="Avg Transit Speed"
          value={loading ? '...' : `${avgDuration}s`}
          sub="Elapsed time between snapshots"
        />
      </div>

      {/* Selected Route / Zone Banner */}
      {(selectedRoute || selectedZone) && (
        <div className="rotations-banner">
          <div className="rotations-banner__text">
            {selectedRoute ? (
              <span>
                Highlighted Corridor:{' '}
                <strong>
                  {selectedRoute.from} ➔ {selectedRoute.to}
                </strong>
              </span>
            ) : (
              <span>
                Focused on Zone: <strong>{selectedZone}</strong>
              </span>
            )}
          </div>
          <button
            type="button"
            className="rotations-banner__clear"
            onClick={() => {
              onSelectRoute(null)
              onSelectZone(null)
            }}
          >
            Clear Selection
          </button>
        </div>
      )}

      {/* Site Retake Splits */}
      {siteSplits.length > 0 && (
        <div className="rotations-splits-section">
          <h4 className="rotations-section-title">Site Entry & Retake Corridors</h4>
          <div className="rotations-splits-grid">
            {siteSplits.map((split) => (
              <div key={split.site} className="rotations-split-card">
                <div className="rotations-split-card__header">
                  <span className="rotations-split-card__title">{split.site} Entries</span>
                  <span className="rotations-split-card__count">{split.total} rotations</span>
                </div>
                <div className="rotations-split-bars">
                  {split.routes.map((r) => {
                    const share = Math.round((r.count / split.total) * 100)
                    const isSelected =
                      selectedRoute?.from === r.from_zone &&
                      selectedRoute?.to === r.to_zone
                    return (
                      <div
                        key={r.from_zone}
                        className={`rotations-split-row ${isSelected ? 'selected' : ''}`}
                        onClick={() =>
                          onSelectRoute(
                            isSelected ? null : { from: r.from_zone, to: r.to_zone },
                          )
                        }
                      >
                        <div className="rotations-split-row__info">
                          <span className="rotations-split-row__from">
                            Via {r.from_zone}
                          </span>
                          <span className="rotations-split-row__pct">{share}%</span>
                        </div>
                        <div className="rotations-split-row__bar-bg">
                          <div
                            className="rotations-split-row__bar-fill"
                            style={{ width: `${share}%` }}
                          />
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Dominant Rotation Routes Leaderboard Table */}
      <div className="rotations-table-section">
        <h4 className="rotations-section-title">Dominant Rotation Corridors</h4>
        <div className="rotations-table-wrapper">
          <table className="rotations-table">
            <thead>
              <tr>
                <th>Corridor Route</th>
                <th className="align-right">Volume</th>
                <th className="align-right">Departure Share</th>
                <th className="align-right">Avg Speed</th>
                <th className="align-right">Round Win %</th>
              </tr>
            </thead>
            <tbody>
              {transitions.length === 0 ? (
                <tr>
                  <td colSpan={5} className="rotations-empty-cell">
                    {loading ? 'Loading rotation telemetry...' : 'No rotation transitions meet the current criteria.'}
                  </td>
                </tr>
              ) : (
                transitions.map((t, idx) => {
                  const isSelected =
                    selectedRoute?.from === t.from_zone &&
                    selectedRoute?.to === t.to_zone
                  const isWinPositive = t.win_rate >= 0.5
                  return (
                    <tr
                      key={`${t.from_zone}->${t.to_zone}`}
                      className={`rotations-tr ${isSelected ? 'selected' : ''}`}
                      onClick={() =>
                        onSelectRoute(
                          isSelected ? null : { from: t.from_zone, to: t.to_zone },
                        )
                      }
                    >
                      <td className="rotations-td-route">
                        <span className="rotations-rank">#{idx + 1}</span>
                        <span className="rotations-from">{t.from_zone}</span>
                        <span className="rotations-arrow">➔</span>
                        <span className="rotations-to">{t.to_zone}</span>
                      </td>
                      <td className="align-right font-mono font-bold">
                        {t.count.toLocaleString()}
                      </td>
                      <td className="align-right font-mono">
                        {Math.round(t.outgoing_share * 100)}%
                      </td>
                      <td className="align-right font-mono text-muted">
                        {t.avg_duration_s}s
                      </td>
                      <td className="align-right">
                        <span
                          className={`rotations-win-badge ${
                            isWinPositive ? 'positive' : 'negative'
                          }`}
                        >
                          {Math.round(t.win_rate * 100)}%
                        </span>
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </aside>
  )
}
