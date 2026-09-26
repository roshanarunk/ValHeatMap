# Running ValHeatMap on Hetzner Cloud (CX23)

One high-performance cloud VPS runs everything: the FastAPI backend, continuous background crawler, and Caddy reverse proxy for automatic Let's Encrypt HTTPS.

```
                  Hetzner Cloud CX23 (4 GB RAM, 40 GB NVMe)
┌────────────────────────────────────────────────────────────────────────┐
│                                                                        │
│   docker-compose                                                       │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │                                                                │   │
│   │   valheatmap-crawler ──writes──▶ /data/analytics.db            │   │
│   │                                          ▲                     │   │
│   │                                        reads                   │   │
│   │                                          │                     │   │
│   │   valheatmap-caddy ────proxy────▶ valheatmap-api               │   │
│   │   (Auto TLS 80/443)                  (:8000)                   │   │
│   └────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
                        valostats.roshanarun.com
```

### Specs & Cost Comparison

| Provider | Plan | Monthly Cost | RAM | vCPU | NVMe SSD | Transfer |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Hetzner Cloud** | **CX23** | **€5.99 (~$6.50)** | **4 GB** | **2 vCPU** | **40 GB** | **20 TB** |
| Fly.io (Previous) | Shared-1x + 20GB vol | ~$8.70 | 1 GB | 1 vCPU | 20 GB | 100 GB |

You get **4x the RAM** and **double the storage** for less money. With 4 GB of RAM, the app will never face OOM kills.

---

## 1. Create the Server on Hetzner

1. Log into the [Hetzner Cloud Console](https://console.hetzner.cloud/).
2. Click **Create Server**:
   - **Location**: **Ashburn, VA (US East)** *(matches Fly.io's `iad` region for optimal API latency)* or **Falkenstein / Nuremberg (EU)**.
   - **Image**: **Ubuntu 24.04** (or select the **Docker CE** image under the *Apps* tab).
   - **Type**: **Shared vCPU (x86)** → **CX23** (€5.99 / mo).
   - **SSH Keys**: Add your public SSH key (`~/.ssh/id_rsa.pub` or `id_ed25519.pub`).
   - **Name**: `valheatmap`
3. Click **Create & Buy Now**. Note down the server's **IPv4** and **IPv6** addresses.

### Configure Hetzner Cloud Firewall
Under **Security → Firewalls**, create a firewall rule assigned to your server:
- **Inbound Rules**:
  - Accept `TCP` port `22` (SSH)
  - Accept `TCP` port `80` (HTTP)
  - Accept `TCP` port `443` (HTTPS)
- **Outbound Rules**:
  - Accept all traffic (default)

---

## 2. Server Initial Setup

SSH into your new server:

```bash
ssh root@<HETZNER_IP>
```

### Install Docker & Docker Compose (if using standard Ubuntu 24.04)
```bash
# Update packages
apt-get update && apt-get upgrade -y
apt-get install -y curl git ufw

# Install Docker using official script
curl -fsSL https://get.docker.com | sh

# Enable and start Docker
systemctl enable --now docker
```

### Clone Repository & Configure Environment
```bash
# Clone the repository
git clone https://github.com/roshanarunk/ValHeatMap.git /opt/valheatmap
cd /opt/valheatmap

# Create your .env file
cp .env.example .env
nano .env
```

Ensure `.env` contains your Henrik API key and domain:
```ini
HENRIK_API_KEY=HDEV-your-actual-key-here
HENRIK_RATE_LIMIT=90
RIOT_REGION=na
DOMAIN=valostats.roshanarun.com
VALHEATMAP_DATA_DIR=/data
VALHEATMAP_READ_ONLY=0
VALHEATMAP_CRAWLER=1
```

---

## 3. Migrate Production Data from Fly.io

ValHeatMap has over 80,000 matches and 11.9M kills on Fly.io. A dedicated migration script moves the database directly without data loss.

### Option A: Run from your local PC (PowerShell)
From the repo root on your local computer:

```powershell
.\deploy\migrate-from-fly.ps1 -HetznerHost root@<HETZNER_IP>
```

This automatically:
1. Flushes SQLite WAL logs on Fly.io (`PRAGMA wal_checkpoint(TRUNCATE)`).
2. Pauses the Fly.io crawler to guarantee write consistency.
3. Compresses `analytics.db`, `valheatmap.db`, and `raw/`.
4. Streams the archive to `/opt/valheatmap/data` on Hetzner and uncompresses it.

### Option B: Manual transfer via SCP
If you ran `.\deploy\migrate-from-fly.ps1` without `-HetznerHost`, copy the downloaded archive manually:

```powershell
scp .\data\valheatmap-migration.tar.gz root@<HETZNER_IP>:/opt/valheatmap/data/
```

On the Hetzner server:
```bash
cd /opt/valheatmap/data
tar -xzf valheatmap-migration.tar.gz
rm valheatmap-migration.tar.gz
```

Verify the files are present:
```bash
ls -lah /opt/valheatmap/data
# You should see: analytics.db (~2.3 GB), valheatmap.db (~70 MB), and raw/
```

---

## 4. Start the Application

On the Hetzner server:

```bash
cd /opt/valheatmap
docker compose up -d --build
```

### Check Logs & Health
```bash
# Follow logs
docker compose logs -f

# Verify API health
curl http://localhost:8000/api/health
```

Output should show:
```json
{"status":"ok","matches":80700+,"kills":11900000+,"read_only":false,"live_sources":{"henrik":true,"riot":false}}
```

---

## 5. Point Your Domain (Cloudflare DNS)

1. Go to your Cloudflare dashboard (or DNS provider).
2. Update the DNS records for `valostats.roshanarun.com`:
   - **A** record: point to your Hetzner **IPv4**
   - **AAAA** record: point to your Hetzner **IPv6**
   - Proxy status: **DNS only (grey cloud)** for the initial Let's Encrypt certificate issuance.
3. Caddy will immediately obtain the TLS certificate over ports 80/443.
4. Test in your browser:
   ```
   https://valostats.roshanarun.com/api/health
   ```
5. Once HTTPS is working, you can switch Cloudflare's proxy status back to **Proxied (orange cloud)** if desired.

---

## 6. Setup Automated Nightly Backups

Configure a nightly cron job to take non-blocking atomic backups of `analytics.db`:

```bash
# Make the backup script executable
chmod +x /opt/valheatmap/deploy/backup.sh

# Open crontab
crontab -e
```

Add the following line to run every night at 4:00 AM:
```cron
0 4 * * * /opt/valheatmap/deploy/backup.sh >> /var/log/valheatmap-backup.log 2>&1
```

Backups will be saved in `/opt/valheatmap/backups/` and automatically pruned after 7 days.

---

## 7. Day-to-Day Operations

| Task | Command |
| :--- | :--- |
| **Deploy latest code** | `cd /opt/valheatmap && git pull && docker compose up -d --build` |
| **View app & crawler logs** | `docker compose logs -f app` |
| **View Caddy access logs** | `docker compose logs -f caddy` |
| **Restart services** | `docker compose restart` |
| **Stop application** | `docker compose down` |
| **Pause crawler** | Set `VALHEATMAP_CRAWLER=0` in `.env` then `docker compose up -d` |
| **Resume crawler** | Set `VALHEATMAP_CRAWLER=1` in `.env` then `docker compose up -d` |
| **Check disk usage** | `df -h /opt/valheatmap/data` |
| **Force database reindex** | `docker exec -it valheatmap-app python -m app.build_analytics --rebuild` |

---

## 8. Decommissioning Fly.io (Stop Billing)

Once `valostats.roshanarun.com` is smoothly running on Hetzner and the crawler is actively ingesting matches:

1. Verify Fly.io is no longer receiving traffic.
2. Destroy the Fly machine and volume:
   ```powershell
   fly apps destroy valheatmap
   ```
3. Your Fly.io monthly billing is now stopped.
