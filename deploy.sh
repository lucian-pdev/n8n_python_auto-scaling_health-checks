#!/bin/bash
set -e

SEPARATOR="=================================================="

NDIRS=( 
    prometheus-data
    grafana-data
#   redis-data
    n8n-data
#   postgres-data
    scripts
    job-output
)

# Exit if not run from project root (where this script resides)
if [[ ! -f "$PWD/deploy.sh" ]]; then
    echo "ERROR: Must run deploy.sh from the project root directory."
    echo "Current directory: $PWD"
    exit 1
fi

echo "$SEPARATOR"
echo "=== Starting deployment ==="
echo "$SEPARATOR"

# Install prerequisites
echo "$SEPARATOR"
echo "[1] Installing prerequisites and creating the configuration file (.env)..."
echo "$SEPARATOR"
sudo apt update
sudo apt install -y ca-certificates curl openssl ufw    # ufw: not preinstalled on Debian

# .env is generated once from a template; replace_* tags become random values
# live: ./deploy.sh   test: ./deploy.sh testing_files/env
[[ -f .env ]] || ./env-maker.sh "${1:-./env}"
set -a; source .env; set +a

echo "$SEPARATOR"
echo "[2] Installing Docker..."
echo "$SEPARATOR"
# Docker's official convenience script picks the repo for this distro (Ubuntu, Debian, ...)
if command -v docker >/dev/null && docker compose version &>/dev/null; then
    echo "Docker already installed: $(docker --version)"
else
    # download first: with `curl | sh` a failed download runs an empty script and the deploy goes on without Docker
    curl -fsSL --retry 5 --retry-all-errors https://get.docker.com -o /tmp/get-docker.sh
    sudo sh /tmp/get-docker.sh && rm -f /tmp/get-docker.sh
fi
sudo apt install -y python3-pip

# Ensuring the directories exist and are owned by USER before docker creates them as ROOT out of necessity
for d in "${NDIRS[@]}"; do
    mkdir -p "$d"
done

sudo systemctl enable --now docker
sudo groupadd -f docker
sudo usermod -aG docker "$USER"

# Traefik needs ports 80/443: retire the old host nginx/certbot setup if present
echo "$SEPARATOR"
echo "[3] Retiring legacy nginx/certbot (if installed)..."
echo "$SEPARATOR"
for unit in nginx_health.timer certbot.timer nginx.service; do
    sudo systemctl disable --now "$unit" 2>/dev/null || true    # no-op when the unit doesn't exist
done

echo "$SEPARATOR"
echo "[4] Building and starting services..."
echo "$SEPARATOR"
sudo docker compose build
sudo docker compose up -d

# Ensure permissions are good for directories needed by containers
echo "$SEPARATOR"
echo "[5] Ensuring permissions..."
echo "$SEPARATOR"
sudo chown -R 1000:1000 n8n-data/ 2>/dev/null || true
sudo chown 1000:1000 scripts/ 2>/dev/null || true

# Install systemd services
echo "$SEPARATOR"
echo "[6] Installing systemd services..."
echo "$SEPARATOR"
sudo chmod +x ./autoscaler/setup-systemd.sh
sudo ./autoscaler/setup-systemd.sh

# Firewall
echo "$SEPARATOR"
echo "[7] Configuring firewall..."
echo "$SEPARATOR"
sudo chmod +x ./HTTPS/firewall_rules.sh
sudo ./HTTPS/firewall_rules.sh

echo "$SEPARATOR"
echo "=== Deployment complete ==="
echo "$SEPARATOR"
echo "Access n8n at: https://$DOMAIN"
echo "Access Grafana at: https://$DOMAIN/grafana/"
echo "$SEPARATOR"
echo "Check status with: docker compose ps"
echo "View logs with: docker compose logs -f"
echo "Certificate / routing logs: docker compose logs -f traefik"
echo "Traefik dashboard: ssh -L 8082:127.0.0.1:8082 $USER@<this-host>, then http://127.0.0.1:8082/dashboard/"
echo "$SEPARATOR"
echo "$SEPARATOR"
