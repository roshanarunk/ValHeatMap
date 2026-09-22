import type {
  AgentInfo,
  Facets,
  MapInfo,
  InsightsResponseV2,
  KillsResponseV2,
  UtilityResponseV2,
  InsightsResponse,
  KillsResponse,
  MapRow,
  MatchSummary,
  PlantsResponse,
  UtilityResponse,
  WeaponInfo,
} from './types'

// Same-origin in dev (Vite proxies /api) and in production (FastAPI serves the SPA).
const BASE = ''

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

async function get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const url = new URL(`${BASE}${path}`, window.location.origin)
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      if (value.length === 0) continue
      url.searchParams.set(key, value.join(','))
    } else if (typeof value === 'boolean') {
      if (value) url.searchParams.set(key, '1')
    } else {
      url.searchParams.set(key, String(value))
    }
  }
  const resp = await fetch(url.toString())
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`
    try {
      const body = await resp.json()
      if (body?.detail) detail = String(body.detail)
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(detail, resp.status)
  }
  return (await resp.json()) as T
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`
    try {
      const parsed = await resp.json()
      if (parsed?.detail) detail = String(parsed.detail)
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(detail, resp.status)
  }
  return (await resp.json()) as T
}

/** Filters shared by the kill, utility and insight endpoints. */
export interface QueryFilters {
  map_name?: string
  acts?: string[]
  patches?: string[]
  ranks?: string[]
  agents?: string[]
  victim_agents?: string[]
  /** Agent roles (Duelist, Sentinel, …), expanded to agents server-side. */
  roles?: string[]
  victim_roles?: string[]
  players?: string[]
  sides?: string[]
  rounds?: number[]
  weapons?: string[]
  abilities?: string[]
  /** Zone box as "x0,y0,x1,y1" in normalised minimap space. */
  zone?: string
  /** Which end of the duel the zone constrains. */
  zone_anchor?: 'victim' | 'killer'
  /** A puuid, for personal stats. */
  player?: string
  /** Which end of the duel the player is: their kills, deaths or both. */
  player_role?: 'killer' | 'victim' | 'either'
  /** Restrict to a single match, for review. */
  match_id?: string
  time_start?: number
  time_end?: number
  traded_only?: boolean
  untraded_only?: boolean
  first_blood_only?: boolean
  post_plant_only?: boolean
  pre_plant_only?: boolean
  include_teamkills?: boolean
  anchor?: 'victim' | 'killer'
  trade_window?: number
  trade_radius?: number
}

export interface PlayerMapRow {
  map_name: string
  kills: number
  deaths: number
  matches: number
}

export interface PlayerSummary {
  puuid: string
  name: string
  tag: string
  riot_id: string
  region: string | null
  /** Maps they have played, most-played first. */
  maps: PlayerMapRow[]
  /** Unix seconds; null until the crawler has fetched their history. */
  crawled_at: number | null
  requested_at: number | null
  kills: number
  deaths: number
  kd: number
  matches: number
  traded_deaths: number
  trade_rate: number
  first_bloods: number
  first_deaths: number
  untraded_deaths: number
  /** First bloods + first deaths: the duels that opened a round. */
  opening_duels: number
  opening_win_rate: number
  /** Kills that avenged a team-mate. */
  trade_kills: number
  rounds_won_with_kill: number
  /** Of rounds you got a kill in, the share your team won. */
  kill_round_win_rate: number
  post_plant_kills: number
  post_plant_deaths: number
  /** Rounds with 2+ kills, and the best single round. */
  multi_kill_rounds: number
  best_round: number
  tracked: boolean
}

export interface PlayerMatch {
  match_id: string
  map_name: string
  mode: string
  queue: string | null
  started_at: number | null
  rounds: number | null
  agent: string
  kills: number
  deaths: number
  kd: number
  first_bloods: number
}

export interface MatchKill {
  round: number
  t_ms: number
  side: string
  killer_agent: string
  victim_agent: string
  killer: string
  victim: string
  weapon: string
  ability: string
  damage_type: string
  vx: number
  vy: number
  kx: number | null
  ky: number | null
  traded: boolean
  first_blood: boolean
  post_plant: boolean
  round_won: boolean
}

export interface MatchDetail {
  match_id: string
  map_name: string
  /** Calibration and callouts, so the client can draw it without a second call. */
  map: MapInfo | null
  mode: string
  queue: string | null
  started_at: number | null
  rounds: number | null
  avg_tier: number | null
  kills: MatchKill[]
  plants: {
    round: number
    t_ms: number
    site: string
    x: number
    y: number
    won: boolean
    defused: boolean
  }[]
  scoreboard: {
    puuid: string
    agent: string
    kills: number
    deaths: number
    first_bloods: number
    kd: number
  }[]
}

export interface DatasetStats {
  matches: number
  kills: number
  plants: number
  by_map: { map_name: string; matches: number; kills: number }[]
  players_known: number
  players_pending: number
  queue: Record<string, number>
  loaded_in_memory: number
}

export interface CrawlResult {
  stored: number
  skipped: number
  errors: number
  requests: number
  elapsed_s: number
  rate_per_min: number
  dataset: DatasetStats
}

export const api = {
  facets: () => get<Facets>('/api/facets'),
  killsV2: (f: QueryFilters) => get<KillsResponseV2>('/api/kills', f as Record<string, unknown>),
  utilityV2: (f: QueryFilters) =>
    get<UtilityResponseV2>('/api/utility', f as Record<string, unknown>),
  insightsV2: (f: QueryFilters) =>
    get<InsightsResponseV2>('/api/insights', f as Record<string, unknown>),
  dataset: () => get<DatasetStats>('/api/dataset'),
  reloadDataset: () => post<{ loaded: number }>('/api/dataset/reload'),

  // --- players ---
  registerPlayer: (riotId: string) =>
    post<{ player: PlayerSummary; new: boolean }>('/api/player/register', {
      riot_id: riotId,
    }),
  // The Riot ID goes in the path, so the # must be encoded or it reads as
  // a URL fragment and never reaches the server.
  // Filters narrow the headline numbers to the current selection, so the
  // stat tiles describe what is on screen rather than always a career.
  player: (riotId: string, f: QueryFilters = {}) =>
    get<PlayerSummary>(
      `/api/player/${encodeURIComponent(riotId)}`,
      f as Record<string, unknown>,
    ),
  playerMatches: (riotId: string, limit = 20, f: QueryFilters = {}) =>
    get<{ player: PlayerSummary; matches: PlayerMatch[] }>(
      `/api/player/${encodeURIComponent(riotId)}/matches`,
      { ...(f as Record<string, unknown>), limit },
    ),
  refreshPlayer: (riotId: string) =>
    post<{ player: PlayerSummary; stored: number; new_matches: number }>(
      `/api/player/${encodeURIComponent(riotId)}/refresh`,
    ),
  match: (matchId: string) => get<MatchDetail>(`/api/match/${encodeURIComponent(matchId)}`),
  crawl: (matches: number, region?: string, seed?: string) => {
    const params = new URLSearchParams({ matches: String(matches) })
    if (region) params.set('region', region)
    if (seed) params.set('seed', seed)
    return post<CrawlResult>(`/api/crawl?${params.toString()}`)
  },
  health: () => get<{ status: string; matches: number; live_sources: Record<string, boolean> }>('/api/health'),
  maps: () => get<{ maps: MapRow[] }>('/api/maps'),
  matches: () => get<{ matches: MatchSummary[] }>('/api/matches'),
  reference: () => get<{ agents: AgentInfo[]; weapons: WeaponInfo[] }>('/api/reference'),
  kills: (f: QueryFilters) => get<KillsResponse>('/api/kills', f as Record<string, unknown>),
  utility: (f: QueryFilters) => get<UtilityResponse>('/api/utility', f as Record<string, unknown>),
  plants: (f: QueryFilters & { cluster_radius?: number; min_sample?: number; sites?: string[] }) =>
    get<PlantsResponse>('/api/plants', f as Record<string, unknown>),
  insights: (f: QueryFilters & { grid?: number }) =>
    get<InsightsResponse>('/api/insights', f as Record<string, unknown>),
  importUpload: (payload: unknown) =>
    post<{ imported: string; match: MatchSummary }>('/api/import/upload', payload),
  importHenrik: (matchId: string, region?: string) =>
    post<{ imported: string; match: MatchSummary }>(
      `/api/import/henrik/${encodeURIComponent(matchId)}${region ? `?region=${region}` : ''}`,
    ),
  importHenrikPlayer: (name: string, tag: string, region?: string, size = 5) =>
    post<{ imported: string[]; count: number }>(
      `/api/import/henrik/player/${encodeURIComponent(name)}/${encodeURIComponent(tag)}?size=${size}${
        region ? `&region=${region}` : ''
      }`,
    ),
  importRiot: (matchId: string, region?: string) =>
    post<{ imported: string; match: MatchSummary }>(
      `/api/import/riot/${encodeURIComponent(matchId)}${region ? `?region=${region}` : ''}`,
    ),
}
