#!/bin/bash

# Run this to install all systemd services for monitoring, scaling, and GitHub sync

set -e

# === CONFIGURATION - EDIT THESE ===
SCRIPTS_REPO="https://github.com/your-org/vetted-scripts.git"  # Full HTTPS URL
SCRIPTS_BRANCH="main"                                          # Branch to track
SCRIPTS_DIR="/app/scripts"                                     # Where to clone/pull
GITHUB_TOKEN=""                                                # Optional: for private repos
SYNC_INTERVAL_MINUTES="5"                                      # How often to sync
# =================================

# Derived config
if [ -n "$GITHUB_TOKEN" ]; then
    # Insert token into URL for private repos
    AUTH_REPO=$(echo "$SCRIPTS_REPO" | sed "s|https://|https://${GITHUB_TOKEN}@|")
else
    AUTH_REPO="$SCRIPTS_REPO"
fi

TARGET_DIR="/usr/local/bin"

echo "Installing autoscaler, health checker, and GitHub sync..."

# Install scripts
sudo cp "./autoscaler/autoscaler.sh" "$TARGET_DIR/"
sudo cp "./HTTPS/nginx_health.sh" "$TARGET_DIR/"
sudo chmod +x "$TARGET_DIR/autoscaler.sh" "$TARGET_DIR/nginx_health.sh"

# Create log files
sudo touch /var/log/nginx_health.log
sudo touch /var/log/github_sync.log
sudo chmod 644 /var/log/nginx_health.log /var/log/github_sync.log

# Install dependencies
sudo apt-get update && sudo apt-get install -y bc jq git

# Create scripts directory
sudo mkdir -p "$SCRIPTS_DIR"
sudo chown "$(whoami)":"$(whoami)" "$SCRIPTS_DIR" 2>/dev/null || true

# === AUTOSCALER SERVICE ===
sudo tee /etc/systemd/system/autoscaler.service > /dev/null << 'EOF'
[Unit]
Description=Docker Compose Autoscaler for n8n-worker
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/autoscaler.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/autoscaler.timer > /dev/null << 'EOF'
[Unit]
Description=Run autoscaler every 30 seconds

[Timer]
OnBootSec=30
OnUnitActiveSec=30
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF

# === NGINX HEALTH SERVICE ===
sudo tee /etc/systemd/system/nginx_health.service > /dev/null << 'EOF'
[Unit]
Description=Nginx Health Checker
After=nginx.service
Wants=nginx.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nginx_health.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/nginx_health.timer > /dev/null << 'EOF'
[Unit]
Description=Run nginx health check every 2 minutes

[Timer]
OnBootSec=10
OnUnitActiveSec=2m
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF

# === GITHUB SYNC SERVICE ===
sudo tee /etc/systemd/system/github_sync.service > /dev/null << EOF
[Unit]
Description=GitHub Scripts Sync
After=network.target
Wants=network.target

[Service]
Type=oneshot
Environment="SCRIPTS_REPO=$AUTH_REPO"
Environment="SCRIPTS_BRANCH=$SCRIPTS_BRANCH"
Environment="SCRIPTS_DIR=$SCRIPTS_DIR"
ExecStart=/usr/local/bin/github_sync.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/github_sync.timer > /dev/null << EOF
[Unit]
Description=Run GitHub sync every ${SYNC_INTERVAL_MINUTES} minutes

[Timer]
OnBootSec=10
OnUnitActiveSec=${SYNC_INTERVAL_MINUTES}m
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF

# === GITHUB SYNC SCRIPT ===
sudo tee /usr/local/bin/github_sync.sh > /dev/null << 'SCRIPT'
#!/bin/bash

# GitHub sync script - clone or pull vetted scripts

set -e

REPO="${SCRIPTS_REPO:-}"
BRANCH="${SCRIPTS_BRANCH:-main}"
TARGET="${SCRIPTS_DIR:-/app/scripts}"

if [ -z "$REPO" ]; then
    echo "$(date): ERROR: SCRIPTS_REPO not set" >&2
    exit 1
fi

# Ensure target directory exists
mkdir -p "$TARGET"

if [ -d "$TARGET/.git" ]; then
    # Existing repo: fetch and reset to ensure clean state
    echo "$(date): Pulling latest from $BRANCH..."
    cd "$TARGET"
    git fetch origin "$BRANCH"
    git reset --hard "origin/$BRANCH"
else
    # Fresh clone
    echo "$(date): Cloning $REPO to $TARGET..."
    git clone --branch "$BRANCH" --single-branch "$REPO" "$TARGET"
fi

SCRIPT_COUNT=$(find "$TARGET" -name "*.py" -type f 2>/dev/null | wc -l)
echo "$(date): Sync complete. $SCRIPT_COUNT Python scripts available."

# Optional: notify python-api to refresh (if health endpoint supports it)
# curl -sf http://localhost:8000/health || true
SCRIPT

sudo chmod +x /usr/local/bin/github_sync.sh

# Reload systemd and enable services
sudo systemctl daemon-reload

sudo systemctl enable autoscaler.timer
sudo systemctl enable nginx_health.timer
sudo systemctl enable github_sync.timer

sudo systemctl start autoscaler.timer
sudo systemctl start nginx_health.timer
sudo systemctl start github_sync.timer

echo ""
echo "=== Services Installed ==="
echo ""
echo "Configuration:"
echo "  Repo:    $SCRIPTS_REPO"
echo "  Branch:  $SCRIPTS_BRANCH"
echo "  Target:  $SCRIPTS_DIR"
echo "  Interval: ${SYNC_INTERVAL_MINUTES}m"
[ -n "$GITHUB_TOKEN" ] && echo "  Auth:    Token configured" || echo "  Auth:    None (public repo)"
echo ""
echo "Timers:"
sudo systemctl list-timers --all | grep -E "(autoscaler|nginx_health|github_sync|NEXT)"
echo ""
echo "Status:"
sudo systemctl status autoscaler.timer --no-pager 2>/dev/null | head -3
sudo systemctl status nginx_health.timer --no-pager 2>/dev/null | head -3
sudo systemctl status github_sync.timer --no-pager 2>/dev/null | head -3
echo ""
echo "Logs:"
echo "  sudo journalctl -u autoscaler.service -f"
echo "  sudo journalctl -u nginx_health.service -f"
echo "  sudo journalctl -u github_sync.service -f"
echo "  tail -f /var/log/nginx_health.log"
echo ""
echo "Manual sync:"
echo "  sudo systemctl start github_sync.service"
echo ""
echo "Reload nginx to activate healthz endpoint:"
echo "  sudo nginx -t && sudo systemctl reload nginx"