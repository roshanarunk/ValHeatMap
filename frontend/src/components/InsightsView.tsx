import { Bar, Empty, Panel, StatTile } from './Controls'
import type { InsightsResponse } from '../lib/types'

const pct = (v: number) => `${Math.round(v * 100)}%`

export function InsightsView({
  data,
  loading,
}: {
  data: InsightsResponse | null
  loading: boolean
}) {
  if (loading && !data) return <Empty>Crunching the numbers…</Empty>
  if (!data) return <Empty>No data for this selection.</Empty>

  const { trades, opening_duels, weapons, distance, timing, multikills, opening_impact } = data
  const maxTiming = Math.max(1, ...timing.buckets.map((b) => b.total))
  const maxDist = Math.max(1, ...distance.buckets.map((b) => b.count))

  return (
    <div className="insights">
      <div className="statgrid statgrid--wide">
        <StatTile
          label="Opening kill → round win"
          value={pct(opening_impact.win_rate_after_opening_kill)}
          sub={`${opening_impact.sample} rounds`}
          tone="hot"
        />
        <StatTile
          label="As attackers"
          value={pct(opening_impact.attack_win_rate)}
          sub={`${opening_impact.attack_sample} opening kills`}
        />
        <StatTile
          label="As defenders"
          value={pct(opening_impact.defense_win_rate)}
          sub={`${opening_impact.defense_sample} opening kills`}
        />
        <StatTile
          label="Median duel range"
          value={`${distance.median_m}m`}
          sub={`${distance.sample} duels measured`}
          tone="good"
        />
      </div>

      <div className="insights__grid">
        <Panel
          title="Trade economy"
          subtitle="Who gets avenged, and who punishes"
        >
          {trades.players.length === 0 ? (
            <Empty>No kills in this selection.</Empty>
          ) : (
            <div className="table-wrap"><table className="table">
              <thead>
                <tr>
                  <th>Player</th>
                  <th title="Kills / deaths in this selection">K/D</th>
                  <th title="Share of this player's deaths that a teammate traded back">
                    Death traded
                  </th>
                  <th title="Kills this player made avenging a teammate">Trade kills</th>
                  <th title="Kills this player got that went unpunished">Unpunished</th>
                </tr>
              </thead>
              <tbody>
                {trades.players.map((p) => (
                  <tr key={p.puuid}>
                    <td>
                      <span className="who">
                        <strong>{p.name}</strong>
                        <em>{p.agent}</em>
                      </span>
                    </td>
                    <td>
                      {p.kills}/{p.deaths}
                    </td>
                    <td>
                      <span className="cellbar">
                        <Bar
                          value={p.traded_death_rate}
                          max={1}
                          tone="linear-gradient(90deg,#2ec27e,#7ee2b8)"
                        />
                        <em>{pct(p.traded_death_rate)}</em>
                      </span>
                    </td>
                    <td>{p.trade_kills}</td>
                    <td>{p.untraded_kills}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </Panel>

        <Panel title="Opening duels" subtitle="The first fight of the round decides most of them">
          {opening_duels.players.length === 0 ? (
            <Empty>No opening duels in this selection.</Empty>
          ) : (
            <div className="table-wrap"><table className="table">
              <thead>
                <tr>
                  <th>Player</th>
                  <th>Duels</th>
                  <th>Won</th>
                  <th title="Rounds won after this player took the opening kill">Converted</th>
                </tr>
              </thead>
              <tbody>
                {opening_duels.players.map((p) => (
                  <tr key={p.puuid}>
                    <td>
                      <span className="who">
                        <strong>{p.name}</strong>
                        <em>{p.agent}</em>
                      </span>
                    </td>
                    <td>{p.duels}</td>
                    <td>
                      <span className="cellbar">
                        <Bar
                          value={p.win_rate}
                          max={1}
                          tone="linear-gradient(90deg,#4c8dff,#8fb8ff)"
                        />
                        <em>{pct(p.win_rate)}</em>
                      </span>
                    </td>
                    <td>{p.opening_kills > 0 ? pct(p.round_conversion) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </Panel>

        <Panel title="Engagement range" subtitle="How far apart duels are fought">
          <ul className="ranklist">
            {distance.buckets.map((b) => (
              <li key={b.range}>
                <span className="ranklist__label">
                  <strong>{b.range}</strong>
                </span>
                <Bar value={b.count} max={maxDist} tone="linear-gradient(90deg,#ff7a45,#ffc58f)" />
                <span className="ranklist__value">{b.count}</span>
              </li>
            ))}
          </ul>
        </Panel>

        <Panel title="Round timing" subtitle="When kills happen, by side">
          {timing.buckets.length === 0 ? (
            <Empty>No timing data.</Empty>
          ) : (
            <div className="timing">
              {timing.buckets.map((b) => (
                <div key={b.t} className="timing__col" title={`${b.total} kills`}>
                  <div className="timing__stack">
                    <span
                      className="timing__atk"
                      style={{ height: `${(b.attack / maxTiming) * 100}%` }}
                    />
                    <span
                      className="timing__def"
                      style={{ height: `${(b.defense / maxTiming) * 100}%` }}
                    />
                  </div>
                  <em>{Math.round(b.t / 1000)}s</em>
                </div>
              ))}
            </div>
          )}
          <p className="legend">
            <span className="legend__swatch legend__swatch--atk" /> Attack
            <span className="legend__swatch legend__swatch--def" /> Defense
          </p>
        </Panel>

        <Panel title="Weapons" subtitle="Kills and the range they were taken at">
          {weapons.weapons.length === 0 ? (
            <Empty>No weapon kills in this selection.</Empty>
          ) : (
            <ul className="ranklist">
              {weapons.weapons.slice(0, 10).map((w) => (
                <li key={w.weapon}>
                  <span className="ranklist__label">
                    <strong>{w.weapon}</strong>
                    {w.avg_distance_m !== null && <em>avg {w.avg_distance_m}m</em>}
                  </span>
                  <Bar
                    value={w.kills}
                    max={weapons.weapons[0].kills}
                    tone="linear-gradient(90deg,#b28dff,#ddd0ff)"
                  />
                  <span className="ranklist__value">{w.kills}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Multikill rounds" subtitle="3+ kills by one player">
          {multikills.rounds.length === 0 ? (
            <Empty>No multikill rounds in this selection.</Empty>
          ) : (
            <ul className="ranklist ranklist--plain">
              {multikills.rounds.slice(0, 12).map((r) => (
                <li key={`${r.round}-${r.name}`}>
                  <span className="ranklist__label">
                    <strong>
                      {r.kills}K · {r.name}
                    </strong>
                    <em>
                      {r.agent} · round {r.round + 1} · {(r.span_ms / 1000).toFixed(1)}s
                    </em>
                  </span>
                  <span className={`pill${r.won_round ? ' pill--good' : ' pill--bad'}`}>
                    {r.won_round ? 'Won' : 'Lost'}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  )
}
