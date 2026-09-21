# Running ValHeatMap on Oracle Cloud (Always Free)

One VM runs everything: the crawler, the API and the site. The database
lives on local disk, which removes the constraint that broke the Vercel
deployment repeatedly — there is no `/tmp` ceiling, no snapshot to publish,
and no size cap to stay under.

```
        Oracle Cloud VM (Always Free)
┌──────────────────────────────────────────┐
│  crawler ──writes──▶ data/analytics.db   │
│                             ▲            │
│                          reads           │
│                             │            │
│  Caddy (TLS) ──▶ FastAPI ───┘            │
└──────────────────────────────────────────┘
        valostats.roshanarun.com
```

**Cost: $0.** Always Free resources do not expire.

## What Always Free gives you

| | Allocation | What we need |
|---|---|---|
| ARM compute | 2 OCPU, 12 GB RAM | Crawler and API are both light |
| Block storage | 200 GB | ~13 GB raw + ~0.5 GB database today |
| Egress | 10 TB/month | Nowhere close |
| Expiry | none | — |

At current density, 200 GB holds roughly **250,000 matches**.

One rule worth knowing: Oracle reclaims an instance after 7 days if CPU,
network *and* memory are all below 20%. A crawler running continuously
keeps it well clear of that, so this workload is not at risk — but an
instance you stop and forget about is.

---

## 1. Create the instance

1. Sign up at <https://cloud.oracle.com>. A card is required for identity
   verification; Always Free resources are not charged to it.
2. Note your **home region** — Always Free only exists there.
3. **Compute → Instances → Create instance**
   - Image: **Ubuntu 22.04** (or 24.04)
   - Shape: **Ampere A1 Flex**, 2 OCPUs, 12 GB
   - Boot volume: 100 GB (inside the 200 GB allowance)
   - Add your SSH public key
4. Create, then copy the **public IP**.

> ARM capacity in a given region is sometimes exhausted. If creation
> fails with "out of capacity", retry later or pick another availability
> domain — this is common and not a problem with your account.

### Open the ports

Oracle blocks everything by default, at two layers:

**Security list** — Networking → VCN → subnet → default security list →
add ingress rules for `0.0.0.0/0` on TCP **80** and **443**.

**Host firewall** — Ubuntu images ship with iptables rules that also
need opening:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Missing the second step is the usual reason a new Oracle VM appears
unreachable despite correct security lists.

---

## 2. Point the domain at it

In Cloudflare, change the `valostats` record:

- Type **A**, value = the VM's public IP
- Proxy status: **DNS only (grey cloud)** for the first run

Caddy needs to reach Let's Encrypt over port 80 to issue the certificate,
and Cloudflare's proxy interferes with that. Switch the proxy back on once
the certificate is issued if you want its caching and DDoS protection.

---

## 3. Install

SSH in and run:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/roshanarunk/ValHeatMap.git
sudo bash ValHeatMap/deploy/setup.sh valostats.roshanarun.com
```

That installs Python, Node, Caddy and the application, builds the
frontend, and writes systemd units for the API and the crawler. It is
idempotent, so re-running it after a code change is the update path.

Then add your API key and seed the database:

```bash
sudo nano /opt/valheatmap/.env          # HENRIK_API_KEY=HDEV-...
sudo -u valheatmap /opt/valheatmap/.venv/bin/python -m app.build_analytics
sudo systemctl start valheatmap-api valheatmap-crawler
```

The crawler starts from an empty frontier and seeds itself from the
leaderboard, so the database fills on its own.

### Bringing your existing data across

Skip the cold start by copying what you already have. From your PC:

```powershell
# the derived database (~0.5 GB) -- enough to serve immediately
scp data/analytics.db ubuntu@<ip>:/tmp/
```

```bash
# on the VM
sudo mv /tmp/analytics.db /opt/valheatmap/data/
sudo chown valheatmap:valheatmap /opt/valheatmap/data/analytics.db
sudo systemctl restart valheatmap-api
```

The raw payloads (13 GB) are only needed to rebuild the analytics
database after a schema change. Copy them too if you want that ability on
the VM; otherwise keep them on your PC.

---

## 4. Check it

```bash
curl https://valostats.roshanarun.com/api/health
journalctl -u valheatmap-crawler -f
```

`/api/health` should report your match count, and the crawler log should
show batches landing.

---

## Operating it

| Task | Command |
|---|---|
| Update to latest code | `sudo bash /opt/valheatmap/deploy/setup.sh valostats.roshanarun.com` |
| Pause crawling | `sudo systemctl stop valheatmap-crawler` |
| Resume | `sudo systemctl start valheatmap-crawler` |
| Crawler logs | `journalctl -u valheatmap-crawler -f` |
| API logs | `journalctl -u valheatmap-api -f` |
| Rebuild after a schema change | `sudo -u valheatmap /opt/valheatmap/.venv/bin/python -m app.build_analytics --rebuild` |
| Disk usage | `df -h /` |

Both services restart on failure and start on boot, so a reboot needs no
intervention.

---

## What this removes

The Vercel deployment failed repeatedly for one underlying reason: the
database had to be copied into a 550 MB `/tmp` on every cold start, and
the dataset kept outgrowing it. Everything built around that — the slim
build, the size guard, the snapshot publish, the ETag polling, the R2
bucket — existed only to work within that limit.

On a VM the crawler writes the file the API reads. None of that machinery
is needed, and the dataset can grow until the disk fills, which at current
rates is years away.

`app/publish.py` and the R2 configuration are kept: they still work, and
they are the way to run a read-only mirror elsewhere if you ever want one.
