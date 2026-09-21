# Running ValHeatMap on Fly.io

One machine runs the API and the crawler; a volume holds the database
they share.

```
              Fly.io machine
┌────────────────────────────────────────┐
│  crawler ──writes──▶ /data/analytics.db│
│                            ▲           │
│                         reads          │
│                            │           │
│  Fly edge (TLS) ──▶ FastAPI┘           │
└────────────────────────────────────────┘
         valostats.roshanarun.com
```

This removes the constraint that broke the serverless deployment
repeatedly. There is no `/tmp` ceiling, no snapshot to publish, no size
guard to stay under, and the dataset grows until the volume fills.

## Cost

| | |
|---|---|
| shared-cpu-1x, 512 MB, 24/7 | $3.32/mo |
| 20 GB volume | $3.00/mo |
| **Total** | **~$6.32/mo** |

512 MB is comfortable: SQLite uses very little memory, and both processes
are I/O-bound rather than memory-hungry. 20 GB holds the database plus
roughly 50,000 matches of raw payloads; 10 GB ($1.50) is enough if you
keep the raw JSON on your PC.

---

## 1. Install and sign in

```powershell
iwr https://fly.io/install.ps1 -useb | iex
fly auth signup      # or: fly auth login
```

Fly asks for a card at signup and bills monthly for what runs.

## 2. Create the app and volume

From the repo root:

```powershell
fly launch --no-deploy --name valheatmap --region iad
fly volumes create valheatmap_data --size 20 --region iad
```

`fly launch` will notice the existing `fly.toml` and use it. Keep the app
name and region consistent with the volume, or the machine will have
nothing to mount.

## 3. Set the API key

```powershell
fly secrets set HENRIK_API_KEY=HDEV-your-key-here
fly secrets set HENRIK_RATE_LIMIT=90 RIOT_REGION=na
```

Secrets are encrypted and injected as environment variables; they are not
in the image or the repo.

## 4. Deploy

```powershell
fly deploy
```

The first build takes a few minutes: it compiles the frontend in one
stage and installs Python dependencies in another. Then:

```powershell
fly status
fly logs
curl https://valheatmap.fly.dev/api/health
```

The API answers immediately on an empty volume, reporting zero matches,
while the crawler fills it.

## 5. Bring your existing data across

Skip the cold start by copying the database you already have:

```powershell
# a shell on the machine, with the volume mounted
fly ssh console

# in another terminal, push the file
fly ssh sftp shell
put data/analytics.db /data/analytics.db
```

For a 500 MB file, `fly sftp` can be slow; an alternative is to let the
crawler rebuild from scratch, which takes a few hours and needs no
transfer.

After copying, restart so the API picks it up:

```powershell
fly apps restart valheatmap
```

## 6. Point the domain at it

```powershell
fly certs add valostats.roshanarun.com
```

Fly prints the DNS records to create. In Cloudflare:

- **A** record for `valostats` → the IPv4 Fly gives you
- **AAAA** record → the IPv6
- Proxy status **DNS only (grey cloud)** until the certificate is issued

Check with `fly certs show valostats.roshanarun.com`. Once it reports
issued, you can switch Cloudflare's proxy back on.

---

## Operating it

| Task | Command |
|---|---|
| Deploy a change | `fly deploy` |
| Logs (both processes) | `fly logs` |
| Shell on the machine | `fly ssh console` |
| Pause crawling | `fly secrets set VALHEATMAP_CRAWLER=0` (restarts the app) |
| Resume | `fly secrets unset VALHEATMAP_CRAWLER` |
| Disk usage | `fly ssh console -C "df -h /data"` |
| Rebuild the database | `fly ssh console -C "cd /app/backend && python -m app.build_analytics --rebuild"` |
| Scale the volume | `fly volumes extend <id> --size 40` |

Both processes restart automatically if they exit, and the machine is
configured never to auto-stop, because a sleeping machine is not crawling.

---

## What this replaces

`app/publish.py`, the R2 bucket, `app/slim.py` and the publish size guard
all existed to fit a database into a 550 MB serverless `/tmp`. None of
them are needed here.

They are kept rather than deleted: they still work, and publishing a
snapshot is how you would run a read-only mirror elsewhere, or hand the
data to someone else. The local crawler and tray icon also still work
unchanged if you ever want to crawl from your PC again.
