# ValHeatMap

Spatial Valorant analytics — kill heatmaps, utility-damage maps, plant
heatmaps and **plant-spot win rates**, with agent filters, a round-time
scrubber and trade detection.

The point of this project is the stats other trackers don't show. Sites like
tracker.gg tell you a player's K/D; they don't tell you *where* on Ascent
that player keeps dying untraded, which plant position actually wins the
round, or how much of a Viper's damage converts into kills.

![Kill heatmap](docs/screenshot-kills.png)

## What it shows

**Kill heatmap** — every duel plotted on the real minimap, filterable by
agent, side, round window and weapon. A dual-handle time slider scrubs the
round, with a kill-density strip so you can see when the fights happen.
Plot either where people died or where the killers stood.

**Trade detection** — a death counts as traded when a teammate kills the
killer within a time window *and* near where the death happened. Both the
window (default 3s) and the distance (default 30m) are adjustable in the UI,
because "what counts as a trade" is genuinely contested. Traded deaths can be
highlighted on the map or isolated with a filter.

**Utility damage** — kills finished by damaging abilities, resolved to real
ability names. Riot reports only the loadout slot (`GrenadeAbility`,
`Ultimate`, …), so the app joins slot + agent to recover "Paint Shells" or
"Showstopper", and breaks the map down per ability.

**Plants & win rate** — the differentiated view. Individual plant coordinates
are too sparse to compute a rate per exact point, so nearby plants are
clustered into spots (per site, so an A plant can never merge with a B one)
and the round win rate is computed per spot. Spots are drawn on the map sized
by sample and coloured by win rate; anything below the sample threshold is
dashed and greyed rather than quietly presented as fact.

![Plant spots by win rate](docs/screenshot-plants.png)

**Advanced stats** — opening-duel win rates and how often they convert into
round wins, per-player trade economy (whose deaths get avenged, whose kills
go unpunished), engagement-range distributions per weapon, round-timing
profiles by side, map control zones and multikill rounds.

**Deathmatch and other modes** — the analytics engine is mode-aware. Any
non-Bomb mode flows through the same pipeline, with round, side and plant
panels hidden automatically and opening-kill tracking disabled (in DM every
kill would otherwise look like an opening).

## Running it

Requires Python 3.11+ and Node 18+.

```bash
# backend
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --reload      # http://127.0.0.1:8000

# frontend (separate terminal)
cd frontend
npm install
npm run dev                                  # http://127.0.0.1:5173
```

The dev server proxies `/api` to the backend. For a single-process
deployment, build the frontend and let FastAPI serve it:

```bash
cd frontend && npm run build
cd ../backend && python -m uvicorn app.main:app --port 8000
```

Six example matches (Ascent, Bind, Fracture, Haven, Icebox, Split) are
bundled in `data/matches/`, so the app is useful with no API key at all.

## Adding your own matches

Three routes, all in the **Add matches** panel:

1. **Upload** a raw match JSON — Riot `match-v1` or HenrikDev v4 shape, both
   auto-detected. No key required.
2. **HenrikDev API** — set `HENRIK_API_KEY` and fetch by Riot ID
   (`Name#TAG`) or match id. Keys: <https://api.henrikdev.xyz/dashboard>
3. **Official Riot API** — set `RIOT_API_KEY`. Wired up and ready for when a
   production `match-v1` key is available.

```bash
export HENRIK_API_KEY=...     # or RIOT_API_KEY=...
export RIOT_REGION=na         # na | eu | ap | kr | latam | br
```

Matches from every source land in the same store, so a heatmap can span a
bundled match, one pulled live and one uploaded by hand.

## How it works

```
data sources ──▶ adapters ──▶ canonical model ──▶ analytics ──▶ API ──▶ React
 Riot v1          riot.py       models.py          kills.py
 HenrikDev v4     henrik.py                        plants.py
 uploaded JSON                                     insights.py
```

Every source is normalised into one canonical `Match` before any statistic
is computed, so adding a source never touches the analytics, and the same
trade rule applies identically to a Riot payload and a HenrikDev one.

### The coordinate transform

Riot ships per-map calibration constants. Mapping world units onto the
minimap is the single most error-prone part of the project, and most
community implementations get it wrong, because **the axes swap**:

```
minimap_x = world_y * xMultiplier + xScalarToAdd
minimap_y = world_x * yMultiplier + yScalarToAdd
```

This was derived empirically and is verified by a test asserting that every
kill in all six bundled matches lands inside the minimap bounds.

### The trade rule

Measured over the bundled 5v5 matches, genuine revenge kills land a median
1358 world units from the original death, and 90% fall within ~2850. The
default radius is 3000 (~30m): wide enough to keep almost every real trade,
tight enough to reject an unrelated kill on the far side of the map.

Note that the bundled matches show trade rates around 6–9%, well below the
20–30% typical of coordinated play. That is a property of the sample — these
are pub games where deaths often go unpunished — not a bug.

## Tests

```bash
cd backend && python -m pytest
```

32 tests covering both source adapters, the coordinate transform, trade
detection windows and radii, plant clustering, filtering and mode awareness.

## Reference data

Map calibration, agents and weapons are cached in `backend/app/data/` from
[valorant-api.com](https://valorant-api.com) so the app runs offline.
Refresh after a new agent or map ships:

```bash
cd backend && python -m app.refresh_reference
```

## Layout

```
backend/app/
  models.py              canonical match model
  reference.py           maps/agents/weapons + coordinate transform
  store.py               match loading, caching, multi-match selection
  sources/               riot.py, henrik.py, clients.py
  analytics/             kills.py, plants.py, insights.py
  main.py                FastAPI app
frontend/src/
  lib/heatmap.ts         canvas density renderer
  components/            MapCanvas, TimeSlider, InsightsView, …
data/matches/            bundled sample matches
legacy/                  the original Flask + matplotlib prototype
```

The original prototype is kept in `legacy/` for reference; nothing in the
current app depends on it.
