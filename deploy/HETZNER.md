# Running ValHeatMap on Hetzner Cloud

One VPS runs everything: the FastAPI backend, the background crawler, and
Caddy as a reverse proxy with automatic Let's Encrypt HTTPS.

```
              Hetzner Cloud CPX21, Ashburn (3 vCPU, 4 GB RAM, 80 GB disk)
┌────────────────────────────────────────────────────────────────────────┐
│   docker compose                                                       │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │   app container                                                │   │
│   │     crawler ──writes──▶ /data/analytics.db ◀──reads── API :8000│   │
│   │                                                        ▲       │   │
│   │   caddy (TLS on 80/443) ─────────proxy─────────────────┘       │   │
│   └────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
                        valostats.roshanarun.com
```

## Sizing

As of October 2026 the data is about **21 GB**: `analytics.db` ~13.8 GB
(including the ~1 GB `idx_k_heat` index), `valheatmap.db` ~0.2 GB, and
`raw/` ~7 GB. It grows as the crawler runs. Nightly backups keep 3
compressed copies (~20 GB), and the migration briefly needs room for a
compressed copy alongside the unpacked one.

**That rules out the 40 GB plans.** Choose from:

| Plan | Location | vCPU | RAM | Disk | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **CPX21** (recommended) | Ashburn, VA | 3 | 4 GB | 80 GB | Same region as Fly's `iad`, close to NA players |
| CX33 | Nuremberg / Helsinki | 4 | 8 GB | 80 GB | More RAM, but further from NA players |

The CX (cost-optimized) line is EU-only; US locations only offer CPX.
Check the current monthly price in the console when you create the server.

---

## 1. Create the server

1. Log into the [Hetzner Cloud Console](https://console.hetzner.cloud/).
2. **Create Server**:
   - **Location**: Ashburn, VA
   - **Image**: Ubuntu 24.04
   - **Type**: Shared vCPU → **CPX21**
   - **Backups**: enable it (Hetzner's add-on: daily off-server snapshots for
     a percentage of the server price). The nightly `backup.sh` copies live
     on the same disk, so they don't protect against losing the server.
   - **SSH key**: add your public key (`~\.ssh\id_ed25519.pub`). If you
     don't have one: `ssh-keygen -t ed25519` in PowerShell.
   - **Name**: `valheatmap`
3. **Create & Buy Now**. Note the **IPv4** and **IPv6** addresses.

### Firewall
**Security → Firewalls**, create one and apply it to the server:
- Inbound: TCP 22 (SSH), TCP 80 (HTTP), TCP 443 (HTTPS)
- Outbound: allow all (default)

---

## 2. Set up the server

```powershell
ssh root@<HETZNER_IP>
```

On the server:

```bash
apt-get update && apt-get upgrade -y
apt-get install -y git
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

git clone https://github.com/roshanarunk/ValHeatMap.git /opt/valheatmap
cd /opt/valheatmap
cp .env.example .env
nano .env          # set HENRIK_API_KEY; DOMAIN is already valostats.roshanarun.com
mkdir -p data
```

Do **not** start the app yet: an empty start creates an empty database
that the migration would then have to replace.

---

## 3. Move the data from Fly.io

Run everything here from the repo root on your PC, in PowerShell.

### 3a. Make room on the Fly volume

The Fly volume is full (20 GB of 20 GB), and the database file there has
not changed since Sep 26. The export needs ~22 GB of working space, so grow
the volume first. It's billed per GB for the few days until Fly is shut
down, and goes away with the app.

```powershell
fly volumes list                                    # note the vol_... id
fly volumes extend <vol_id> -s 50
```

If you skip this, the script stops and prints this command with the
right size.

### 3b. Run the migration script

```powershell
.\deploy\migrate-from-fly.ps1 -HetznerHost root@<HETZNER_IP>
```

What it does, in order:

1. Checks the Fly volume has enough room, and stops if not.
2. Sets `VALHEATMAP_CRAWLER=0` on Fly, which restarts the machine with the
   crawler off, then confirms the crawler is gone. **From here on, Fly
   collects nothing**, so no data lands on Fly after the copy is taken.
3. On Fly, snapshots both databases with SQLite's backup API (consistent
   even while the site serves), gzips them, and tars `raw/`. This runs
   detached on the machine, so a dropped SSH session doesn't kill it.
4. Downloads everything to `data\migration\` and checks each file's
   SHA-256 against the one computed on Fly, then removes the export from
   the Fly volume.
5. Uploads to `/opt/valheatmap/data` on Hetzner, checks the SHA-256 values
   again, unpacks, runs `PRAGMA quick_check`, and confirms the row counts
   match Fly's.

Expect an hour or more, mostly transfer time. The Fly site keeps serving
throughout, apart from a brief restart in step 2. If anything fails, the
script stops with a message, and you can safely rerun it.

Useful options:
- `-SkipRaw`: leave `raw/` behind (7 GB). The site doesn't need it; only
  the backfill scripts re-read it.
- Without `-HetznerHost`: only downloads to `data\migration\`. Rerun with
  it later to upload.

---

## 4. Start the app

On the server:

```bash
cd /opt/valheatmap
docker compose up -d --build
docker compose logs -f app          # Ctrl+C to stop following
curl http://localhost:8000/api/health
```

The health check should report the same match and kill counts the
migration script printed, with `"read_only": false`.

---

## 5. Point the domain at Hetzner

The domain is currently served by Fly (`fly certs list` shows
`valostats.roshanarun.com`). In your DNS provider (Cloudflare):

1. Replace the existing record for `valostats` (likely a CNAME to
   `valheatmap.fly.dev`) with:
   - **A** → the Hetzner IPv4
   - **AAAA** → the Hetzner IPv6
   - Proxy status: **DNS only (grey cloud)** while Caddy gets its
     certificate.
2. Caddy requests the certificate automatically once DNS resolves.
   Watch for it with `docker compose logs -f caddy`.
3. Check `https://valostats.roshanarun.com/api/health` in a browser.
4. Optional: switch the record back to **Proxied (orange cloud)**. If you
   do, set Cloudflare's SSL/TLS mode to **Full (strict)**, or requests
   will loop.

---

## 6. Nightly backups

```bash
chmod +x /opt/valheatmap/deploy/backup.sh
crontab -e
```

Add:
```cron
0 4 * * * /opt/valheatmap/deploy/backup.sh >> /var/log/valheatmap-backup.log 2>&1
```

Backups go to `/opt/valheatmap/backups/`. The newest 3 of each database
are kept (`KEEP=3`, ~20 GB at today's size). The script skips the backup,
rather than filling the disk, if there isn't room for the uncompressed
snapshot.

---

## 7. Day-to-day operations

| Task | Command |
| :--- | :--- |
| Deploy latest code | `cd /opt/valheatmap && git pull && docker compose up -d --build` |
| App and crawler logs | `docker compose logs -f app` |
| Caddy logs | `docker compose logs -f caddy` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Pause the crawler | set `VALHEATMAP_CRAWLER=0` in `.env`, then `docker compose up -d` |
| Resume the crawler | set `VALHEATMAP_CRAWLER=1` in `.env`, then `docker compose up -d` |
| Disk usage | `df -h /opt/valheatmap` |

Keep an eye on disk usage: the data grows with every crawled match.
Hetzner servers can be resized to a bigger disk from the console (the
server must be powered off briefly).

---

## 8. Shut down Fly.io

Wait until the site has been served from Hetzner for a day or two, and the
crawler there is adding matches (the counts in `/api/health` keep
rising). Then:

```powershell
fly certs remove valostats.roshanarun.com --app valheatmap
fly apps destroy valheatmap
```

That deletes the machine and the volume and stops Fly billing. Delete the
local copies in `data\migration\` once you no longer need them.
