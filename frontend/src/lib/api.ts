import type {
  AgentInfo,
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
  match_ids?: string[]
  map_name?: string
  mode?: string
  agents?: string[]
  victim_agents?: string[]
  players?: string[]
  sides?: string[]
  rounds?: number[]
  weapons?: string[]
  abilities?: string[]
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
  dataset: () => get<DatasetStats>('/api/dataset'),
  reloadDataset: () => post<{ loaded: number }>('/api/dataset/reload'),
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
