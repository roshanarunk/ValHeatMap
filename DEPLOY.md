# Deploying ValHeatMap

The site is read-only and cheap to host; the crawler is long-running and
lives wherever you keep a persistent process. They are connected by a
single file: a compact SQLite database published to object storage.

```
   your machine                        Cloudflare R2            Vercel
┌────────────────────┐            ┌──────────────────┐   ┌────────────────┐
│ crawler --forever  │  publish   │ analytics.db.gz  │   │  FastAPI +     │
│   ↓                │───────────▶│   (~35 MB gz)    │──▶│  React SPA     │
│ analytics.db       │            └──────────────────┘   │  (read-only)   │
│ data/raw/*.json    │                                   └────────────────┘
└────────────────────┘
   source of truth                    the handoff            serves queries
```

## Why this shape

A serverless function cannot host the crawler: Vercel kills a function at
300s (Hobby) or 800s (Pro), so there is no "run forever" process. It also
cannot re-parse the raw payloads — 2.5 GB of JSON takes ~40s to read, which
every cold start would pay.

Both problems go away by separating them. The crawler runs locally, where
it is fastest (measured 2,611 matches/hour) and free. It writes a derived
database of ~100 bytes per kill, which is small enough to ship anywhere,
and the site only ever runs indexed SQL against that.

## 1. Local setup

```bash
cd backend
pip install -r requirements.txt
python -m app.build_analytics      # derive analytics.db from data/raw
```

`.env` at the repo root (gitignored):

```ini
HENRIK_API_KEY=HDEV-...
HENRIK_RATE_LIMIT=90

# Cloudflare R2 -- 10 GB free, no egress charges
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=valheatmap
R2_PUBLIC_URL=https://pub-xxxxxxxx.r2.dev
```

## 2. Run the crawler continuously

```bash
cd backend
python -m app.crawler --forever --batch 200 --publish-every 2000
```

Each cycle resumes from the stored frontier, so stopping and restarting
costs nothing and repeats no work. New matches go straight into
`analytics.db`, and a snapshot is published every `--publish-every`
matches.

To keep it running across reboots on Windows, register it with Task
Scheduler:

```powershell
schtasks /create /tn ValHeatMapCrawler /sc onstart /rl highest ^
  /tr "cmd /c cd /d C:\Users\Roshan\Documents\Code\ValHeatMap\backend && python -m app.crawler --forever"
```

Publish by hand at any time with `python -m app.publish`.

## 3. Deploy the site

Create the R2 bucket, enable public access, and copy its public URL. Then
in Vercel, set one environment variable:

```
VALHEATMAP_SNAPSHOT_URL = https://pub-xxxxxxxx.r2.dev/analytics.db.gz
```

and deploy:

```bash
vercel --prod
```

`vercel.json` already wires the Python function, the SPA build and the
`/api/*` rewrite. The database is deliberately **not** bundled: at ~100 MB
and growing it would bloat every deployment, so the function downloads it
into `/tmp` on cold start and reuses it while the instance stays warm.

### Cost

| | |
|---|---|
| Vercel Hobby | $0 |
| Cloudflare R2 (10 GB free, no egress) | $0 |
| **Total** | **$0** |

Nothing here needs Vercel Pro, because nothing is scheduled on Vercel — the
crawler is not running there.

## Alternative: everything on one host

To run the crawler *and* the site on one platform, Fly.io fits: a 512 MB
always-on machine is ~$3.32/month and a 10 GB volume ~$1.50, so ~$5/month
all-in, with the database on the volume and no snapshot handoff at all.

```bash
fly launch --no-deploy
fly volumes create valheatmap_data --size 10
fly secrets set HENRIK_API_KEY=...
fly deploy
```

Run the API as the main process and the crawler as a second process in
`fly.toml`, both pointed at `/data/analytics.db`. The only change needed in
the code is `VALHEATMAP_READ_ONLY=0`, since the volume is writable.

## Keeping the site fresh

The deployed app checks the snapshot's ETag and re-downloads when it
changes (`snapshot.refresh_if_stale`). Freshness is therefore however often
the crawler publishes — every ~2,000 matches by default, roughly hourly at
full crawl rate.

## Rebuilding after an analytics change

Changing how a stat is computed does not mean re-fetching anything:

```bash
cd backend
python -m app.build_analytics --rebuild   # ~40s for 6,900 matches
python -m app.publish
```

That is the reason the raw payloads are kept.
