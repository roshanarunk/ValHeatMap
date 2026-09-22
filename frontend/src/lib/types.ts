export interface Vec2 {
  x: number
  y: number
}

export interface MapCallout {
  region: string
  super_region: string
  position: Vec2
}

export interface MapInfo {
  uuid: string
  name: string
  map_url: string
  minimap: string
  splash: string
  has_calibration: boolean
  callouts: MapCallout[]
}

export interface KillPoint {
  round: number
  t: number
  killer: string
  victim: string
  killer_agent: string
  victim_agent: string
  killer_team: string
  side: 'attack' | 'defense' | 'none'
  weapon: string
  ability: string
  type: 'weapon' | 'ability' | 'bomb' | 'fall' | 'unknown'
  traded: boolean
  trade_kill: boolean
  first_blood: boolean
  post_plant: boolean
  round_won: boolean
  victim_pos: Vec2
  killer_pos?: Vec2
  /**
   * Set only when the query named a player: true if they got this kill,
   * false if they died in it. The server decides, since the point payload
   * carries agent names rather than player ids.
   */
  mine?: boolean
}

export interface KillStats {
  total: number
  traded: number
  trade_rate: number
  utility: number
  utility_rate: number
  first_bloods: number
  post_plant: number
  round_win_rate: number
  avg_trade_latency_ms: number | null
}

export interface KillsResponse {
  map: MapInfo
  points: KillPoint[]
  stats: KillStats
  histogram: { t: number; count: number }[]
  matches: string[]
  anchor: 'victim' | 'killer'
}

export interface PlantPoint {
  round: number
  site: string
  t: number
  team: string
  won: boolean
  defused: boolean
  position: Vec2
}

export interface PlantSpot {
  id: number
  site: string
  position: Vec2
  plants: number
  wins: number
  losses: number
  defused: number
  win_rate: number
  reliable: boolean
  spread: number
  rounds: number[]
  avg_plant_time_ms: number
}

export interface PlantsResponse {
  map: MapInfo
  points: PlantPoint[]
  spots: PlantSpot[]
  sites: {
    site: string
    plants: number
    wins: number
    win_rate: number
    defused: number
    avg_plant_time_ms: number
  }[]
  summary: {
    planted_rounds: number
    plant_win_rate: number
    defused?: number
    spots?: number
  }
}

export interface UtilityAbility {
  agent: string
  agent_icon: string
  ability: string
  slot: string
  kills: number
}

export interface UtilityResponse {
  map: MapInfo
  points: KillPoint[]
  report: {
    abilities: UtilityAbility[]
    total_utility_kills: number
    utility_share: number
  }
  stats: KillStats
}

export interface TradeRow {
  puuid: string
  name: string
  agent: string
  team: string
  kills: number
  deaths: number
  traded_deaths: number
  traded_death_rate: number
  trade_kills: number
  untraded_kills: number
  untraded_kill_rate: number
}

export interface OpeningRow {
  puuid: string
  name: string
  agent: string
  team: string
  opening_kills: number
  opening_deaths: number
  duels: number
  win_rate: number
  round_conversion: number
}

export interface ZoneCell {
  x: number
  y: number
  kills: number
  attack: number
  defense: number
  traded: number
  attack_share: number | null
  trade_rate: number
  cell_size: number
}

export interface InsightsResponse {
  stats: KillStats
  trades: { players: TradeRow[] }
  opening_duels: { players: OpeningRow[] }
  utility: { abilities: UtilityAbility[]; total_utility_kills: number; utility_share: number }
  weapons: { weapons: { weapon: string; kills: number; traded: number; avg_distance_m: number | null }[] }
  distance: { buckets: { range: string; count: number }[]; median_m: number; sample: number }
  zones: { grid: number; cells: ZoneCell[] }
  timing: { bucket_ms: number; buckets: { t: number; attack: number; defense: number; total: number }[] }
  multikills: {
    rounds: { round: number; name: string; agent: string; kills: number; won_round: boolean; span_ms: number }[]
  }
  opening_impact: {
    sample: number
    win_rate_after_opening_kill: number
    attack_sample: number
    attack_win_rate: number
    defense_sample: number
    defense_win_rate: number
  }
}

export interface PlayerSummary {
  puuid: string
  name: string
  tag: string
  display: string
  team: string
  agent: string
  agent_id: string
  kills: number
  deaths: number
  assists: number
  score: number
  damage: number
  headshots: number
  bodyshots: number
  legshots: number
}

export interface MatchSummary {
  match_id: string
  map_id: string
  map_name: string
  mode: string
  mode_raw: string
  queue: string
  started_at: number
  game_length_ms: number
  source: string
  is_ranked: boolean
  is_round_based: boolean
  rounds: number
  kills: number
  plants: number
  teams: Record<string, boolean>
  players: PlayerSummary[]
}

export interface MapRow {
  map_name: string
  map_id: string
  matches: number
  kills: number
  plants: number
  modes: string[]
  minimap: string
  splash: string
}

export interface AgentInfo {
  uuid: string
  name: string
  role: string
  icon: string
  portrait: string
  abilities: Record<string, string>
}

export interface WeaponInfo {
  uuid: string
  name: string
  category: string
  icon: string
}


// --- v2 API -------------------------------------------------------------
export interface FacetMap {
  map_name: string
  matches: number
  kills: number
  minimap: string
  splash: string
}

export interface FacetAgent {
  agent: string
  kills: number
  icon: string
  role: string
}

export interface RankBand {
  id: string
  name: string
  tiers: [number, number]
}

export interface FacetAbility {
  ability: string
  agent: string
  kills: number
}

export interface Facets {
  maps: FacetMap[]
  acts: { act: string; matches: number }[]
  agents: FacetAgent[]
  abilities: FacetAbility[]
  weapons: { weapon: string; kills: number }[]
  ranks: RankBand[]
  tier_range: [number, number]
  stats: { matches: number; kills: number; plants: number; generated_at: string | null }
}

export interface KillsResponseV2 {
  map: MapInfo
  points: KillPoint[]
  total: number
  sampled: boolean
  stats: {
    total: number
    matches: number
    traded: number
    trade_rate: number
    first_bloods: number
    post_plant: number
    utility: number
    utility_rate: number
  }
  histogram: { t: number; count: number }[]
}

export interface UtilityResponseV2 extends KillsResponseV2 {
  abilities: { ability: string; agent: string; kills: number }[]
}

export interface InsightsResponseV2 {
  stats: KillsResponseV2['stats']
  agents: { agent: string; kills: number; traded: number }[]
  weapons: { weapon: string; kills: number }[]
  abilities: { ability: string; agent: string; kills: number }[]
  histogram: { t: number; count: number }[]
}
