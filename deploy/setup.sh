#!/usr/bin/env bash
#
# Provision a fresh Oracle Cloud (or any Ubuntu ARM/x86) VM to run
# ValHeatMap: the API, the crawler, and Caddy for TLS.
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/ValHeatMap/main/deploy/setup.sh | bash -s -- <domain>
#
# or, having cloned the repo:
#
#   sudo bash deploy/setup.sh valostats.example.com
#
# Idempotent: safe to re-run after a config change or a failed attempt.

set -euo pipefail

DOMAIN="${1:-}"
APP_USER="${APP_USER:-valheatmap}"
APP_DIR="/opt/valheatmap"
REPO="${REPO:-https://github.com/roshanarunk/ValHeatMap.git}"

if [[ -z "$DOMAIN" ]]; then
  echo "usage: sudo bash deploy/setup.sh <domain>" >&2
  echo "   eg: sudo bash deploy/setup.sh valostats.example.com" >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "run with sudo" >&2
  exit 1
fi

echo "==> installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates \
  debian-keyring debian-archive-keyring apt-transport-https

# Caddy terminates TLS and renews certificates on its own, which is the
# whole reason to prefer it here over nginx + certbot.
if ! command -v caddy >/dev/null; then
  echo "==> installing Caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq
  apt-get install -y -qq caddy
fi

echo "==> creating $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"

echo "==> fetching the application"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --quiet origin
  git -C "$APP_DIR" reset --hard --quiet origin/main
else
  rm -rf "$APP_DIR"
  git clone --quiet --depth 1 "$REPO" "$APP_DIR"
fi
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> python environment"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"

echo "==> building the frontend"
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
  apt-get install -y -qq nodejs
fi
sudo -u "$APP_USER" bash -c "cd $APP_DIR/frontend && npm install --silent && npm run build --silent"

# --- .env -------------------------------------------------------------
# Preserved across re-runs: it holds the API key.
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "==> writing a placeholder .env (add your key before starting)"
  cat > "$APP_DIR/.env" <<EOF
HENRIK_API_KEY=
HENRIK_RATE_LIMIT=90
RIOT_REGION=na
EOF
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

# --- services ---------------------------------------------------------
echo "==> installing systemd units"
cat > /etc/systemd/system/valheatmap-api.service <<EOF
[Unit]
Description=ValHeatMap API
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR/backend
Environment=PYTHONUNBUFFERED=1
# The database lives on local disk, so the API reads it directly. No
# snapshot download, and no /tmp ceiling to size against.
ExecStart=$APP_DIR/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/valheatmap-crawler.service <<EOF
[Unit]
Description=ValHeatMap crawler
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR/backend
Environment=PYTHONUNBUFFERED=1
# --publish-every 0: nothing to publish now that the crawler writes the
# database the API reads. The old snapshot handoff existed only because
# the two lived on different machines.
ExecStart=$APP_DIR/.venv/bin/python -m app.crawler --forever --batch 200 --publish-every 0
Restart=always
RestartSec=30
# Keep it from starving the API on a 2-core box.
Nice=10

[Install]
WantedBy=multi-user.target
EOF

echo "==> configuring Caddy for $DOMAIN"
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode zstd gzip

	# Static assets are content-hashed by Vite, so they can be cached hard.
	@assets path /assets/*
	header @assets Cache-Control "public, max-age=31536000, immutable"

	# API responses change as the crawler runs; let the browser revalidate.
	@api path /api/*
	header @api Cache-Control "public, max-age=30, must-revalidate"

	reverse_proxy 127.0.0.1:8000
}
EOF

systemctl daemon-reload
systemctl enable --quiet valheatmap-api valheatmap-crawler caddy
systemctl restart caddy

echo
echo "=============================================================="
echo " Installed. Two things left:"
echo
echo "  1. Add your API key:"
echo "       sudo nano $APP_DIR/.env"
echo
echo "  2. Seed the database, then start the services:"
echo "       sudo -u $APP_USER $APP_DIR/.venv/bin/python -m app.build_analytics"
echo "       sudo systemctl start valheatmap-api valheatmap-crawler"
echo
echo " Point $DOMAIN at this machine's public IP, then check:"
echo "       https://$DOMAIN/api/health"
echo
echo " Logs:  journalctl -u valheatmap-crawler -f"
echo "=============================================================="
