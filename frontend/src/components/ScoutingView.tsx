import { useState, useEffect, useRef } from 'react'
import { api } from '../lib/api'
import type { ScoutReport, ScoutResponse } from '../lib/types'

interface ScoutingViewProps {
  maps: { map_name: string; minimap: string }[]
  initialMap?: string
  agents: { name: string; icon: string; role: string }[]
  onNavigateToRotations: (params: { player: string; agent?: string; mapName: string }) => void
}

interface OpponentInput {
  riot_id: string
  agent: string
}

const DEFAULT_MAP = 'Ascent'

export function ScoutingView({
  maps,
  initialMap = DEFAULT_MAP,
  agents,
  onNavigateToRotations,
}: ScoutingViewProps) {
  const [selectedMap, setSelectedMap] = useState<string>(initialMap || DEFAULT_MAP)
  const [opponents, setOpponents] = useState<OpponentInput[]>([
    { riot_id: '', agent: '' },
    { riot_id: '', agent: '' },
    { riot_id: '', agent: '' },
    { riot_id: '', agent: '' },
    { riot_id: '', agent: '' },
  ])
  const [isQuickPasteOpen, setIsQuickPasteOpen] = useState(false)
  const [quickPasteText, setQuickPasteText] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [scoutData, setScoutData] = useState<ScoutResponse | null>(null)
  const [expandedHotspots, setExpandedHotspots] = useState<Record<string, boolean>>({})

  useEffect(() => {
    if (initialMap) {
      setSelectedMap(initialMap)
    }
  }, [initialMap])

  const handleOpponentChange = (index: number, field: 'riot_id' | 'agent', value: string) => {
    setOpponents((prev) => {
      const next = [...prev]
      next[index] = { ...next[index], [field]: value }
      return next
    })
  }

  const handleQuickPasteApply = () => {
    const lines = quickPasteText
      .split(/[\n,;]+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
      .slice(0, 5)

    const next: OpponentInput[] = [
      { riot_id: '', agent: '' },
      { riot_id: '', agent: '' },
      { riot_id: '', agent: '' },
      { riot_id: '', agent: '' },
      { riot_id: '', agent: '' },
    ]

    lines.forEach((line, idx) => {
      next[idx] = { riot_id: line, agent: '' }
    })

    setOpponents(next)
    setIsQuickPasteOpen(false)
    setQuickPasteText('')
  }

  const handleLoadSample = () => {
    setOpponents([
      { riot_id: 'TenZ#0001', agent: 'Jett' },
      { riot_id: 'Chronicle#1234', agent: 'Sova' },
      { riot_id: 'Boaster#FNC', agent: 'Omen' },
      { riot_id: 'Derke#9999', agent: '' },
      { riot_id: 'Alfajer#TR1', agent: 'Killjoy' },
    ])
  }

  const handleClearAll = () => {
    setOpponents([
      { riot_id: '', agent: '' },
      { riot_id: '', agent: '' },
      { riot_id: '', agent: '' },
      { riot_id: '', agent: '' },
      { riot_id: '', agent: '' },
    ])
    setScoutData(null)
    setError(null)
  }

  const handleScout = async () => {
    const filled = opponents.filter((o) => o.riot_id.trim().length > 0)
    if (filled.length === 0) {
      setError('Enter at least 1 opponent Riot ID (e.g. Name#TAG).')
      return
    }

    setLoading(true)
    setError(null)

    try {
      const res = await api.scoutLobby(selectedMap, filled)
      setScoutData(res)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Scouting request failed.'
      setError(msg)
    } finally {
      setLoading(false)
    }
  }

  const toggleHotspot = (puuid: string) => {
    setExpandedHotspots((prev) => ({ ...prev, [puuid]: !prev[puuid] }))
  }

  const activeMapObj = maps.find((m) => m.map_name.toLowerCase() === selectedMap.toLowerCase())

  return (
    <div className="scout-view">
      {/* --- Top Sub-header & Notice --- */}
      <div className="scout-header">
        <div className="scout-header__info">
          <h2>Pre-Match Opponent Scouting</h2>
          <p>
            Tactical profiling & counter-play intelligence. 100% ban-proof — manual input only,
            never touches game files or Vanguard memory.
          </p>
        </div>
        <div className="scout-header__actions">
          <button
            type="button"
            className="scout-btn scout-btn--secondary"
            onClick={() => setIsQuickPasteOpen(true)}
          >
            📋 Quick Paste Lobby
          </button>
          <button
            type="button"
            className="scout-btn scout-btn--secondary"
            onClick={handleLoadSample}
          >
            ✨ Sample Lobby
          </button>
          <button
            type="button"
            className="scout-btn scout-btn--secondary"
            onClick={handleClearAll}
          >
            🗑️ Clear
          </button>
        </div>
      </div>

      {/* --- Map Selector Strip --- */}
      <div className="scout-map-bar">
        <span className="scout-map-bar__label">Match Map:</span>
        <div className="scout-map-chips">
          {maps.map((m) => (
            <button
              key={m.map_name}
              type="button"
              className={`scout-map-chip${selectedMap === m.map_name ? ' is-active' : ''}`}
              onClick={() => setSelectedMap(m.map_name)}
            >
              {m.minimap && <img src={m.minimap} alt="" loading="lazy" />}
              <span>{m.map_name}</span>
            </button>
          ))}
        </div>
      </div>

      {/* --- Opponents Input Strip --- */}
      <div className="scout-input-panel">
        <div className="scout-input-grid">
          {opponents.map((opp, idx) => (
            <div key={idx} className="scout-input-row">
              <span className="scout-slot-badge">#{idx + 1}</span>
              <input
                type="text"
                placeholder="Riot ID (e.g. TenZ#NA1)"
                className="scout-text-input"
                value={opp.riot_id}
                onChange={(e) => handleOpponentChange(idx, 'riot_id', e.target.value)}
              />
              <select
                className="scout-select"
                value={opp.agent}
                onChange={(e) => handleOpponentChange(idx, 'agent', e.target.value)}
              >
                <option value="">Agent (Auto)</option>
                {agents.map((ag) => (
                  <option key={ag.name} value={ag.name}>
                    {ag.name}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>

        <div className="scout-submit-row">
          <button
            type="button"
            className="scout-btn scout-btn--primary"
            onClick={handleScout}
            disabled={loading}
          >
            {loading ? 'Analyzing Opponents...' : `🎯 Scout Opponents on ${selectedMap}`}
          </button>
        </div>
      </div>

      {error && <div className="banner banner--error">{error}</div>}

      {/* --- Quick Paste Modal --- */}
      {isQuickPasteOpen && (
        <div className="scout-modal-backdrop" onClick={() => setIsQuickPasteOpen(false)}>
          <div className="scout-modal" onClick={(e) => e.stopPropagation()}>
            <h3>Quick Paste Opponent Lobby</h3>
            <p>Paste up to 5 opponent Riot IDs, separated by newlines or commas:</p>
            <textarea
              className="scout-textarea"
              rows={6}
              placeholder="TenZ#NA1&#10;Chronicle#1234&#10;Boaster#FNC"
              value={quickPasteText}
              onChange={(e) => setQuickPasteText(e.target.value)}
            />
            <div className="scout-modal__footer">
              <button
                type="button"
                className="scout-btn scout-btn--secondary"
                onClick={() => setIsQuickPasteOpen(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="scout-btn scout-btn--primary"
                onClick={handleQuickPasteApply}
              >
                Populate Slots
              </button>
            </div>
          </div>
        </div>
      )}

      {/* --- Results Section --- */}
      {scoutData && (
        <div className="scout-results">
          {/* Executive Briefing */}
          {scoutData.lobby_summary && (
            <div className="scout-briefing">
              <h3 className="scout-briefing__title">⚡ Lobby Threat Assessment</h3>
              <div className="scout-briefing__cards">
                {scoutData.lobby_summary.top_threat && (
                  <div className="scout-threat-card scout-threat-card--danger">
                    <div className="scout-threat-card__badge">Top Opening Threat</div>
                    <div className="scout-threat-card__body">
                      {scoutData.lobby_summary.top_threat.agent_icon && (
                        <img
                          src={scoutData.lobby_summary.top_threat.agent_icon}
                          alt=""
                          className="scout-threat-avatar"
                        />
                      )}
                      <div>
                        <strong>{scoutData.lobby_summary.top_threat.riot_id}</strong>
                        <em>
                          {scoutData.lobby_summary.top_threat.agent} ·{' '}
                          {Math.round(scoutData.lobby_summary.top_threat.opening_win_rate * 100)}%
                          Opening Duel WR · K/D: {scoutData.lobby_summary.top_threat.kd}
                        </em>
                        <p>{scoutData.lobby_summary.top_threat.reason}</p>
                      </div>
                    </div>
                  </div>
                )}

                {scoutData.lobby_summary.weak_link && (
                  <div className="scout-threat-card scout-threat-card--warn">
                    <div className="scout-threat-card__badge">Exploitable Target</div>
                    <div className="scout-threat-card__body">
                      {scoutData.lobby_summary.weak_link.agent_icon && (
                        <img
                          src={scoutData.lobby_summary.weak_link.agent_icon}
                          alt=""
                          className="scout-threat-avatar"
                        />
                      )}
                      <div>
                        <strong>{scoutData.lobby_summary.weak_link.riot_id}</strong>
                        <em>
                          {scoutData.lobby_summary.weak_link.agent} · Trade Rate:{' '}
                          {Math.round(scoutData.lobby_summary.weak_link.trade_rate * 100)}%
                        </em>
                        <p>{scoutData.lobby_summary.weak_link.reason}</p>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {scoutData.lobby_summary.playstyle_notes?.length > 0 && (
                <ul className="scout-briefing__notes">
                  {scoutData.lobby_summary.playstyle_notes.map((note, i) => (
                    <li key={i}>{note}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {/* Opponent Tactical Dossiers */}
          <div className="scout-dossiers">
            {scoutData.reports.map((report) => (
              <OpponentCard
                key={report.riot_id}
                report={report}
                mapName={selectedMap}
                minimapUrl={activeMapObj?.minimap}
                isHotspotExpanded={Boolean(expandedHotspots[report.puuid || report.riot_id])}
                onToggleHotspot={() => toggleHotspot(report.puuid || report.riot_id)}
                onNavigateToRotations={() => {
                  onNavigateToRotations({
                    player: report.puuid || report.riot_id,
                    agent: report.agent,
                    mapName: selectedMap,
                  })
                }}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function OpponentCard({
  report,
  mapName,
  minimapUrl,
  isHotspotExpanded,
  onToggleHotspot,
  onNavigateToRotations,
}: {
  report: ScoutReport
  mapName: string
  minimapUrl?: string
  isHotspotExpanded: boolean
  onToggleHotspot: () => void
  onNavigateToRotations: () => void
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  // Draw first-blood points when hotspot map is open
  useEffect(() => {
    if (!isHotspotExpanded || !canvasRef.current || !report.first_blood_points) return
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = window.devicePixelRatio || 1
    const size = 300
    canvas.width = size * dpr
    canvas.height = size * dpr
    canvas.style.width = `${size}px`
    canvas.style.height = `${size}px`
    ctx.scale(dpr, dpr)

    ctx.clearRect(0, 0, size, size)

    // Draw background minimap if available
    if (minimapUrl) {
      const img = new Image()
      img.crossOrigin = 'anonymous'
      img.src = minimapUrl
      img.onload = () => {
        ctx.drawImage(img, 0, 0, size, size)
        drawPoints()
      }
    } else {
      ctx.fillStyle = '#0f172a'
      ctx.fillRect(0, 0, size, size)
      drawPoints()
    }

    function drawPoints() {
      if (!ctx) return
      report.first_blood_points.forEach((pt) => {
        const x = pt.x * size
        const y = pt.y * size
        ctx.beginPath()
        ctx.arc(x, y, 4.5, 0, Math.PI * 2)
        ctx.fillStyle = pt.is_killer ? '#22c55e' : '#ef4444' // Green FB kill, Red first death
        ctx.strokeStyle = '#ffffff'
        ctx.lineWidth = 1.2
        ctx.fill()
        ctx.stroke()
      })
    }
  }, [isHotspotExpanded, report.first_blood_points, minimapUrl])

  return (
    <div className={`scout-card${!report.has_data ? ' scout-card--empty' : ''}`}>
      {/* Header */}
      <div className="scout-card__header">
        <div className="scout-card__identity">
          {report.agent_icon ? (
            <img src={report.agent_icon} alt="" className="scout-agent-icon" />
          ) : (
            <div className="scout-agent-placeholder">?</div>
          )}
          <div>
            <div className="scout-card__name-row">
              <h4 className="scout-card__name">{report.riot_id}</h4>
              {report.agent && <span className="scout-agent-pill">{report.agent}</span>}
              <span className="scout-matches-tag">
                {report.matches_on_map} matches on {mapName}
              </span>
            </div>
            <div className="scout-tags-row">
              {report.tactical_tags?.map((t, idx) => (
                <span key={idx} className={`scout-tag scout-tag--${t.type}`} title={t.desc}>
                  {t.tag}
                </span>
              ))}
            </div>
          </div>
        </div>

        <div className="scout-card__actions">
          {report.has_data && (
            <button
              type="button"
              className="scout-btn-small"
              onClick={onNavigateToRotations}
              title="Open full rotation flow for this player"
            >
              🧭 Rotations
            </button>
          )}
        </div>
      </div>

      {!report.has_data ? (
        <div className="scout-card__no-data">
          <span>⚠️ {report.error || `No match history on ${mapName} in database yet.`}</span>
        </div>
      ) : (
        <>
          {/* Key Metrics Grid */}
          <div className="scout-stats-grid">
            <div className="scout-stat">
              <span className="scout-stat__label">Map K/D</span>
              <strong className="scout-stat__value">{report.kd.toFixed(2)}</strong>
              <em className="scout-stat__sub">
                {report.kills}K / {report.deaths}D
              </em>
            </div>
            <div className="scout-stat">
              <span className="scout-stat__label">Opening Duel WR</span>
              <strong
                className={`scout-stat__value ${
                  report.opening_win_rate >= 0.55
                    ? 'is-good'
                    : report.opening_win_rate <= 0.4
                    ? 'is-danger'
                    : ''
                }`}
              >
                {Math.round(report.opening_win_rate * 100)}%
              </strong>
              <em className="scout-stat__sub">
                {report.first_bloods} FB / {report.first_deaths} FD
              </em>
            </div>
            <div className="scout-stat">
              <span className="scout-stat__label">Trade Rate</span>
              <strong className="scout-stat__value">
                {Math.round(report.trade_rate * 100)}%
              </strong>
              <em className="scout-stat__sub">deaths avenged</em>
            </div>
            <div className="scout-stat">
              <span className="scout-stat__label">Clutch WR</span>
              <strong className="scout-stat__value">
                {Math.round(report.clutch_win_rate * 100)}%
              </strong>
              <em className="scout-stat__sub">
                {report.clutches_won}/{report.clutches_faced} 1vX
              </em>
            </div>
            <div className="scout-stat">
              <span className="scout-stat__label">Advantage Throw</span>
              <strong
                className={`scout-stat__value ${
                  report.advantage_throw_rate >= 0.35 ? 'is-danger' : ''
                }`}
              >
                {Math.round(report.advantage_throw_rate * 100)}%
              </strong>
              <em className="scout-stat__sub">
                {report.advantage_rounds_thrown} thrown
              </em>
            </div>
          </div>

          {/* Counter-Play Tips */}
          {report.counter_tips?.length > 0 && (
            <div className="scout-playbook">
              <h5>🎯 Tactical Counter-Play:</h5>
              <ul>
                {report.counter_tips.map((tip, i) => (
                  <li key={i}>{tip}</li>
                ))}
              </ul>
            </div>
          )}

          {/* Corridors & Agent Profile Grid */}
          <div className="scout-details-row">
            {/* Rotation Habits */}
            <div className="scout-corridors-box">
              <h5>Corridor & Rotation Habits</h5>
              {report.defense_rotations?.length === 0 && report.attack_rotations?.length === 0 ? (
                <div className="scout-empty-text">No recorded rotations yet.</div>
              ) : (
                <div className="scout-corridors-list">
                  {report.defense_rotations?.map((r, i) => (
                    <div key={`def-${i}`} className="scout-corridor-item">
                      <span className="scout-side-tag scout-side-tag--def">DEF</span>
                      <span className="scout-corridor-path">
                        {r.from_zone} ➔ {r.to_zone}
                      </span>
                      <span className="scout-corridor-meta">
                        {Math.round(r.win_rate * 100)}% WR · {r.avg_duration_s}s ({r.count}x)
                      </span>
                    </div>
                  ))}
                  {report.attack_rotations?.map((r, i) => (
                    <div key={`atk-${i}`} className="scout-corridor-item">
                      <span className="scout-side-tag scout-side-tag--atk">ATK</span>
                      <span className="scout-corridor-path">
                        {r.from_zone} ➔ {r.to_zone}
                      </span>
                      <span className="scout-corridor-meta">
                        {Math.round(r.win_rate * 100)}% WR · {r.avg_duration_s}s ({r.count}x)
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Most Played Agents */}
            <div className="scout-agents-box">
              <h5>Played Agents on {mapName}</h5>
              {report.top_agents?.length === 0 ? (
                <div className="scout-empty-text">No agent breakdown available.</div>
              ) : (
                <div className="scout-agents-list">
                  {report.top_agents?.slice(0, 4).map((ag) => (
                    <div key={ag.agent} className="scout-agent-row">
                      {ag.icon ? (
                        <img src={ag.icon} alt="" className="scout-mini-icon" />
                      ) : (
                        <div className="scout-mini-placeholder" />
                      )}
                      <span className="scout-agent-name">{ag.agent}</span>
                      <span className="scout-agent-stat">
                        {ag.matches}G · K/D: {ag.kd} · {ag.first_bloods} FB
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* First Blood Hotspot Map Preview Toggle */}
          {report.first_blood_points && report.first_blood_points.length > 0 && (
            <div className="scout-hotspots-section">
              <button
                type="button"
                className="scout-hotspot-toggle"
                onClick={onToggleHotspot}
              >
                {isHotspotExpanded ? 'Hide' : 'Show'} First Contact Hotspots (
                {report.first_blood_points.length} duels)
              </button>

              {isHotspotExpanded && (
                <div className="scout-hotspots-canvas-wrap">
                  <canvas ref={canvasRef} className="scout-hotspots-canvas" />
                  <div className="scout-hotspots-legend">
                    <span>
                      <em className="dot dot--green" /> First Blood Pick
                    </span>
                    <span>
                      <em className="dot dot--red" /> First Blood Death
                    </span>
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
