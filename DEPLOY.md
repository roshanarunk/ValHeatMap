# Deploying ValHeatMap

Two halves that meet at one file.

```
   your PC                             Cloudflare R2            Vercel
┌─────────────────────┐            ┌──────────────────┐   ┌────────────────┐
│ crawler (tray icon) │  publish   │ analytics.db.gz  │   │  FastAPI +     │
│   ↓ writes          │───────────▶│   (~35 MB gz)    │──▶│  React SPA     │
│ analytics.db        │            └──────────────────┘   │  (read-only)   │
│ data/raw/*.json     │                                   └────────────────┘
└─────────────────────┘
   source of truth                     the handoff            serves queries
```

**Total hosting cost: $0.** Vercel Hobby and R2's free tier both cover this
comfortably.

> For single-host server deployments running the crawler + API 24/7:
> - **[Hetzner Cloud (deploy/HETZNER.md)](deploy/HETZNER.md)** — **Recommended** (CX23: 4 GB RAM, 40 GB NVMe, Docker Compose + Caddy, ~€5.99/mo)
> - **[Fly.io (deploy/FLY.md)](deploy/FLY.md)** — Managed container platform (~$8.70/mo)
> - **[Oracle Cloud (deploy/ORACLE.md)](deploy/ORACLE.md)** — Always Free Tier ($0/mo)

Why split it this way: a Vercel function is killed after 300s, so it cannot
host a crawler that runs forever, and it cannot re-parse 2.5 GB of raw JSON
on every cold start. Running the crawler on your PC sidesteps both, and is
faster anyway (measured 2,611 matches/hour).

---

# Part 1 — Set up the crawler locally

## 1.1 Install

```powershell
cd C:\Users\Roshan\Documents\Code\ValHeatMap\backend
python -m pip install -r requirements.txt
```

## 1.2 Configure

Create `.env` in the repo root (it is gitignored — the key never gets
committed):

```ini
HENRIK_API_KEY=HDEV-your-key-here
HENRIK_RATE_LIMIT=90
RIOT_REGION=na
```

Get a key at <https://api.henrikdev.xyz/dashboard>. R2 settings get added
to this same file in Part 2.

## 1.3 Build the analytics database

This derives the queryable database from the raw payloads you have already
crawled. It takes about 40 seconds for 6,900 matches.

```powershell
python -m app.build_analytics
```

You should see roughly:

```
  db matches: 6,887
    db kills: 1,014,156
   db plants: 79,447
   file size: 103.0 MB
```

## 1.4 Run it

**With the tray icon (recommended):**

```powershell
cd C:\Users\Roshan\Documents\Code\ValHeatMap
.\crawler.ps1
```

Or double-click **`crawler.vbs`** to start it with no console window at all.

An icon appears in the notification area (you may need to drag it out of
the overflow chevron to keep it visible). The colour is the state:

| Colour | Meaning |
|---|---|
| 🟢 green | crawling |
| 🟠 amber | paused |
| 🔵 blue | publishing a snapshot |
| ⚪ grey | idle — no new matches, backing off |
| 🔴 red | last cycle hit an error |

**Left-click the icon to pause or resume.** Right-click for the full menu:

- live status, dataset size and how many matches this run has added
- **Pause / Resume crawling**
- **Publish snapshot now** — pushes to R2 without waiting for the interval
- **Open data folder** / **Open site**
- **Quit**

A pause is checked between individual API requests, so it takes effect in
about a second rather than at the end of a 200-match batch.

**Headless, no tray:**

```powershell
cd backend
python -m app.crawler --forever --batch 200 --publish-every 2000
```

## 1.5 Start it automatically with Windows

Put a shortcut to `crawler.vbs` in your Startup folder:

```powershell
$startup = [Environment]::GetFolderPath('Startup')
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$startup\ValHeatMap Crawler.lnk")
$s.TargetPath = "C:\Users\Roshan\Documents\Code\ValHeatMap\crawler.vbs"
$s.Save()
```

Remove it by deleting that `.lnk`. Task Scheduler also works, but a
Startup shortcut keeps the tray icon in your own session, which is what
you want for something you interact with.

---

# Part 2 — Cloudflare R2 (the handoff)

R2's free tier is 10 GB with **no egress charges**, which matters because
the site downloads the database on every cold start.

You need a domain already on Cloudflare for step 2.2. If you do not have
one yet, the `r2.dev` subdomain works for testing — see the comparison
there — and switching later is a one-line change to
`VALHEATMAP_SNAPSHOT_URL`.

## 2.1 Create the bucket

1. <https://dash.cloudflare.com> → **R2** → **Create bucket**
2. Name it `valheatmap`, any location, **Create**

## 2.2 Put the bucket behind a custom domain

The Vercel function fetches the snapshot over plain HTTPS, so the bucket
has to be reachable publicly. There are two ways, and they are not
equivalent:

| | `r2.dev` subdomain | Custom domain |
|---|---|---|
| Setup | one click | needs a domain on Cloudflare |
| CDN caching | **none** | yes |
| Rate limits | throttled, dev-only | normal Cloudflare limits |
| Cloudflare's stance | "development purposes" only | production |

Without caching, **every cold start pulls the full ~37 MB from the origin
bucket**. With a custom domain it is served from the edge cache instead,
which is faster and does not count against the bucket's operations. Use a
custom domain.

1. Open the bucket → **Settings** → **Custom Domains** → **Connect Domain**
2. Enter a hostname on a domain already in your Cloudflare account, e.g.
   `data.yourdomain.com`
3. **Continue** → **Connect Domain**

Cloudflare creates the DNS record itself and provisions a certificate.
Status goes *Initializing* → *Active*, usually within a minute or two.

Your snapshot URL is then:

```
https://data.yourdomain.com/analytics.db.gz
```

> Do **not** CNAME your own domain at the `r2.dev` URL. Cloudflare treats
> that as an unsupported access path; the Custom Domains feature above is
> the supported route.

### Required: a Cache Rule for the snapshot

**Do not skip this.** Cloudflare's *Browser Cache TTL* zone setting
defaults to 4 hours and overrides the origin's `Cache-Control` whenever
the origin value is lower -- which ours always is. Without a rule, a fresh
publish can sit behind a stale edge copy for four hours, and the site will
serve old data even though the origin is correct.

1. Your domain → **Caching** → **Cache Rules** → **Create rule**
2. Name it `ValHeatMap snapshot`
3. **When incoming requests match**:
   - Field `Hostname`, Operator `equals`, Value `data.yourdomain.com`
4. **Then**:
   - Cache eligibility: **Eligible for cache**
   - Edge TTL: **Override origin**, `1 minute`
   - Browser TTL: **Respect origin TTL**
5. **Deploy**

Edge TTL is what matters: it bounds how long Cloudflare keeps serving an
old copy after a publish. One minute is comfortably below the crawler's
publish interval, and the object is fetched about once per cold start, so
the extra origin reads are negligible against R2's free tier.

Verify it took effect:

```powershell
cd backend
python -m app.check_snapshot
```

A healthy result reports `edge copy matches the origin` and an edge TTL of
60s. If it reports the edge serving an older copy, purge once (**Caching**
→ **Configuration** → **Purge Everything**) to clear the entry that was
cached under the old 4-hour TTL.

## 2.3 Create API credentials

1. R2 → **Manage R2 API Tokens** → **Create API token**
2. Permissions: **Object Read & Write**
3. Scope it to the `valheatmap` bucket
4. **Create**, then copy the **Access Key ID** and **Secret Access Key** —
   the secret is shown once

Your Account ID is in the R2 sidebar.

## 2.4 Add them to `.env`

```ini
R2_ACCOUNT_ID=your-account-id
R2_ACCESS_KEY_ID=your-access-key
R2_SECRET_ACCESS_KEY=your-secret-key
R2_BUCKET=valheatmap
R2_PUBLIC_URL=https://data.yourdomain.com
```

An R2 API token page shows three values. Only two are used here:

| Shown on the token page | Used? |
|---|---|
| **Access Key ID** | yes → `R2_ACCESS_KEY_ID` |
| **Secret Access Key** | yes → `R2_SECRET_ACCESS_KEY` |
| *Token value* | no — that is for Cloudflare's REST API, not the S3 one |

`R2_ACCOUNT_ID` is in the R2 sidebar, not on the token page.

## 2.5 Staying inside the free tier

**Cloudflare has no hard spending cap.** Budget alerts email you after the
fact; they never stop anything. So the protection is built into the
publisher instead, which refuses to upload when a self-imposed limit would
break:

| Guard | Limit | Why |
|---|---|---|
| Snapshot size | 2 GB | A snapshot that large means the database is broken, not that you grew |
| Uploads/month | 10,000 | R2 allows 1M; this catches a runaway loop |
| Gap between uploads | 60s | Publishing faster does not make the site fresher |

A blocked publish makes no network call at all, and the crawler logs it and
carries on rather than crashing.

Check usage any time:

```powershell
cd backend
python -m app.quota
```

### Where you actually sit

For context, with ~7,000 matches:

| R2 free tier | Limit | Our usage |
|---|---|---|
| Storage | 10 GB | **~37 MB** — publishing overwrites one object, so this stays flat regardless of how often you push |
| Class A (writes) | 1,000,000/mo | **~720/mo** at hourly publishes (0.07%) |
| Class B (reads) | 10,000,000/mo | one GET per site cold start — you would need ~5M cold starts to exhaust it |

Storage only becomes a question at roughly **2 million matches**. The
dataset would have to grow 280x.

### Belt and braces: a budget alert

Worth setting even though you are far from any limit:

1. Cloudflare dashboard → **Manage Account** → **Billing** → **Budget alerts**
2. Add an alert at **$1** with your email

Since correct operation costs $0, any alert at all means something is
wrong and is worth investigating.

## 2.6 Publish once by hand

```powershell
cd backend
python -m app.publish
```

```
compressing analytics.db (103 MB) ...
  -> 35 MB gzipped
uploading ...
published: https://data.yourdomain.com/analytics.db.gz
```

Then verify the URL is reachable and actually being cached:

```powershell
python -m app.check_snapshot
```

```
checking https://data.yourdomain.com/analytics.db.gz

  HTTP 200, 37.0 MB
  looks like gzip, as expected

  cache status: MISS
  second request: HIT
  -> edge caching is working

Use this in Vercel:
  VALHEATMAP_SNAPSHOT_URL = https://data.yourdomain.com/analytics.db.gz
```

`R2_PUBLIC_URL` may be a bare hostname or a full URL; both work.

| Result | Cause |
|---|---|
| 401 on upload | Access Key ID / Secret wrong, or the token lacks Object Read & Write |
| 404 / "not found" in browser | Custom domain still *Initializing*, or the hostname is wrong |
| 522 / connection error | DNS record was edited by hand — remove it and reconnect via Custom Domains |

---

# Part 3 — Deploy the site to Vercel

## 3.1 Push to GitHub

```powershell
cd C:\Users\Roshan\Documents\Code\ValHeatMap
git remote add origin https://github.com/<you>/valheatmap.git
git push -u origin main
```

`.gitignore` already excludes `.env`, `data/raw/` and the databases, so
none of that is uploaded.

## 3.2 Import into Vercel

1. <https://vercel.com/new> → import the repository
2. Leave the framework preset alone — `vercel.json` configures the build
3. Before deploying, open **Environment Variables** and add:

   | Name | Value |
   |---|---|
   | `VALHEATMAP_SNAPSHOT_URL` | `https://data.yourdomain.com/analytics.db.gz` |

4. **Deploy**

Or from the CLI:

```powershell
npm i -g vercel
vercel link
vercel env add VALHEATMAP_SNAPSHOT_URL production
vercel --prod
```

## 3.3 Check it

```
https://<your-project>.vercel.app/api/health
```

```json
{"status":"ok","matches":6887,"kills":1014156,"read_only":true}
```

If `matches` is 0, the function could not fetch the snapshot — check
`VALHEATMAP_SNAPSHOT_URL` opens in a browser. The first request after a
deploy is slow (a few seconds) because it downloads the database; after
that the instance is warm.

---

# How updates reach the site

```
crawler stores matches
   -> publishes to R2 every 2,000 new matches (or via the tray menu)
   -> the deployed function re-checks the snapshot's ETag every 2 minutes
   -> a changed ETag triggers a re-download, and the next request sees it
```

The re-check is a conditional HEAD, so the usual case costs nothing.
`VALHEATMAP_REFRESH_SECONDS` tunes the interval if you want it tighter.

## If the site looks stale

Work outward from the origin:

```powershell
cd backend
python -m app.check_snapshot
```

That reports the three things that go wrong, in order:

1. **The origin is old** — the crawler has not published. Use *Publish
   snapshot now* in the tray menu.
2. **The edge is serving an older copy than the origin** — Cloudflare
   defaults large objects to a 4-hour TTL, so a fresh publish can sit
   behind a stale cache. Purge it (Caching → Configuration → Purge
   Everything) and add the Cache Rule in 2.2 so it stops recurring.
3. **Both are current but the site is not** — the function has not
   re-checked yet. Wait out `VALHEATMAP_REFRESH_SECONDS`, or redeploy to
   force a cold start.

`/api/health` shows `generated_at`, the timestamp of the snapshot that
instance is actually serving. Comparing it against your last publish tells
you immediately which of the three you are looking at.



Nothing is scheduled on Vercel, which is why none of this needs Vercel Pro.

---

# Routine tasks

**Change how a stat is computed** — no re-fetching, the raw payloads are
kept precisely so this is cheap:

```powershell
cd backend
python -m app.build_analytics --rebuild   # ~40s
python -m app.publish
```

**Add a new map or agent** after a Riot patch:

```powershell
python -m app.refresh_reference
```

**Check dataset size:**

```powershell
python -c "import sys; sys.path.insert(0,'.'); from app.analytics_db import AnalyticsDB; print(AnalyticsDB().stats())"
```

---

# Troubleshooting

| Symptom | Cause |
|---|---|
| Tray icon never appears | `pip install pystray pillow`; check the overflow chevron |
| Icon is red | Right-click to see the error; usually a missing or expired `HENRIK_API_KEY` |
| Icon stays grey | Frontier exhausted — it retries every 120s and recovers on its own |
| Site shows 0 matches | `VALHEATMAP_SNAPSHOT_URL` unset or the R2 object is not public |
| Publish fails with 401 | R2 token lacks Object Read & Write, or the keys are wrong |
| Site data looks stale | Publish from the tray menu; check the crawler is not paused |

---

# Alternative: one host instead of two

To avoid the snapshot handoff entirely, Fly.io can run the API *and* the
crawler on one machine with the database on a persistent volume — about
$5/month (512 MB machine ~$3.32, 10 GB volume ~$1.50).

```powershell
fly launch --no-deploy
fly volumes create valheatmap_data --size 10
fly secrets set HENRIK_API_KEY=... 
fly deploy
```

Run the API as the main process and the crawler as a second process in
`fly.toml`, both pointed at `/data/analytics.db`, and set
`VALHEATMAP_READ_ONLY=0` since the volume is writable. You lose the tray
icon (there is no desktop session), so pausing becomes `fly machine stop`.
