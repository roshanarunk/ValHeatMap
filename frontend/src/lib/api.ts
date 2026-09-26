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
  RotationsResponse,
  ScoutReport,
  ScoutResponse,
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

const apiCache = new Map<string, { data: unknown; timestamp: number }>()
const CACHE_TTL_MS = 120_000 // 2 minutes
const CACHE_MAX_ENTRIES = 50

export function clearApiCache(): void {
  apiCache.clear()
}

async function get<T>(
  path: string,
  params?: Record<string, unknown>,
  options?: { signal?: AbortSignal },
): Promise<T> {
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
  const cacheKey = url.toString()
  const cached = apiCache.get(cacheKey)
  if (cached && Date.now() - cached.timestamp < CACHE_TTL_MS) {
    return cached.data as T
  }

  const resp = await fetch(cacheKey, { signal: options?.signal })
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
  const data = (await resp.json()) as T
  if (apiCache.size >= CACHE_MAX_ENTRIES) {
    const oldestKey = apiCache.keys().next().value
    if (oldestKey) apiCache.delete(oldestKey)
  }
  apiCache.set(cacheKey, { data, timestamp: Date.now() })
  return data
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
  victim_weapons?: string[]
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
  // Tactical spacing & micro-positioning
  supported_deaths: number
  isolated_deaths: number
  support_rate: number
  crossfire_kills: number
  // Discipline & Man-advantage
  advantage_deaths: number
  advantage_rounds_thrown: number
  advantage_throw_rate: number
  // Clutches (1vX)
  clutch_kills: number
  clutches_faced: number
  clutches_won: number
  clutch_win_rate: number
  // Impact vs Low Impact / Exit
  low_impact_kills: number
  impact_kills: number
  impact_kill_rate: number
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
    role?: string
    icon?: string
    team?: string
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
  facets: (options?: { signal?: AbortSignal }) => get<Facets>('/api/facets', undefined, options),
  killsV2: (f: QueryFilters, options?: { signal?: AbortSignal }) =>
    get<KillsResponseV2>('/api/kills', f as Record<string, unknown>, options),
  utilityV2: (f: QueryFilters, options?: { signal?: AbortSignal }) =>
    get<UtilityResponseV2>('/api/utility', f as Record<string, unknown>, options),
  insightsV2: (f: QueryFilters, options?: { signal?: AbortSignal }) =>
    get<InsightsResponseV2>('/api/insights', f as Record<string, unknown>, options),
  dataset: (options?: { signal?: AbortSignal }) => get<DatasetStats>('/api/dataset', undefined, options),
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
  player: (riotId: string, f: QueryFilters = {}, options?: { signal?: AbortSignal }) =>
    get<PlayerSummary>(
      `/api/player/${encodeURIComponent(riotId)}`,
      f as Record<string, unknown>,
      options,
    ),
  playerMatches: (
    riotId: string,
    limit = 20,
    f: QueryFilters = {},
    options?: { signal?: AbortSignal },
  ) =>
    get<{ player: PlayerSummary; matches: PlayerMatch[] }>(
      `/api/player/${encodeURIComponent(riotId)}/matches`,
      { ...(f as Record<string, unknown>), limit },
      options,
    ),
  refreshPlayer: (riotId: string) =>
    post<{ player: PlayerSummary; stored: number; new_matches: number }>(
      `/api/player/${encodeURIComponent(riotId)}/refresh`,
    ),
  match: (matchId: string, options?: { signal?: AbortSignal }) =>
    get<MatchDetail>(`/api/match/${encodeURIComponent(matchId)}`, undefined, options),
  crawl: (matches: number, region?: string, seed?: string) => {
    const params = new URLSearchParams({ matches: String(matches) })
    if (region) params.set('region', region)
    if (seed) params.set('seed', seed)
    return post<CrawlResult>(`/api/crawl?${params.toString()}`)
  },
  health: (options?: { signal?: AbortSignal }) =>
    get<{ status: string; matches: number; live_sources: Record<string, boolean> }>(
      '/api/health',
      undefined,
      options,
    ),
  maps: (options?: { signal?: AbortSignal }) => get<{ maps: MapRow[] }>('/api/maps', undefined, options),
  matches: (options?: { signal?: AbortSignal }) =>
    get<{ matches: MatchSummary[] }>('/api/matches', undefined, options),
  reference: (options?: { signal?: AbortSignal }) =>
    get<{ agents: AgentInfo[]; weapons: WeaponInfo[] }>('/api/reference', undefined, options),
  kills: (f: QueryFilters, options?: { signal?: AbortSignal }) =>
    get<KillsResponse>('/api/kills', f as Record<string, unknown>, options),
  utility: (f: QueryFilters, options?: { signal?: AbortSignal }) =>
    get<UtilityResponse>('/api/utility', f as Record<string, unknown>, options),
  plants: (
    f: QueryFilters & { cluster_radius?: number; min_sample?: number; sites?: string[] },
    options?: { signal?: AbortSignal },
  ) =>
    get<PlantsResponse>('/api/plants', f as Record<string, unknown>, options),
  rotations: (
    params: {
      map_name?: string
      side?: 'defense' | 'attack' | 'all'
      player?: string
      agent?: string
      match_id?: string
      team?: string
      round_num?: number
      min_count?: number
      focus_zone?: string
    },
    options?: { signal?: AbortSignal },
  ) =>
    get<RotationsResponse>('/api/rotations', params as Record<string, unknown>, options),
  insights: (f: QueryFilters & { grid?: number }, options?: { signal?: AbortSignal }) =>
    get<InsightsResponse>('/api/insights', f as Record<string, unknown>, options),
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
  scoutLobby: (mapName: string, opponents: { riot_id: string; agent?: string }[]) =>
    post<ScoutResponse>('/api/scout', { map_name: mapName, opponents }),
  scoutPlayer: (riotId: string, mapName: string, agent?: string, options?: { signal?: AbortSignal }) =>
    get<ScoutReport>(
      '/api/scout/player',
      { riot_id: riotId, map_name: mapName, agent },
      options,
    ),
}
