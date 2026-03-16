#!/bin/bash

# Run this to install all systemd services for monitoring and scaling

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/usr/local/bin"

echo "Installing autoscaler and health checker..."

# Install scripts
sudo cp "$SCRIPT_DIR/autoscaler.sh" "$TARGET_DIR/"
sudo cp "$SCRIPT_DIR/nginx-health.sh" "$TARGET_DIR/"
sudo chmod +x "$TARGET_DIR/autoscaler.sh" "$TARGET_DIR/nginx-health.sh"

# Create log file
sudo touch /var/log/nginx-health.log
sudo chmod 644 /var/log/nginx-health.log

# Install bc if not present
sudo apt-get update && sudo apt-get install -y bc jq

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
sudo tee /etc/systemd/system/nginx-health.service > /dev/null << 'EOF'
[Unit]
Description=Nginx Health Checker
After=nginx.service
Wants=nginx.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nginx-health.sh
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/nginx-health.timer > /dev/null << 'EOF'
[Unit]
Description=Run nginx health check every 2 minutes

[Timer]
OnBootSec=10
OnUnitActiveSec=2m
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF

# Reload systemd and enable services
sudo systemctl daemon-reload

sudo systemctl enable autoscaler.timer
sudo systemctl enable nginx-health.timer

sudo systemctl start autoscaler.timer
sudo systemctl start nginx-health.timer

echo ""
echo "=== Services Installed ==="
echo ""
echo "Timers:"
sudo systemctl list-timers --all | grep -E "(autoscaler|nginx-health|NEXT)"
echo ""
echo "Status:"
sudo systemctl status autoscaler.timer --no-pager 2>/dev/null | head -5
sudo systemctl status nginx-health.timer --no-pager 2>/dev/null | head -5
echo ""
echo "Logs:"
echo "  sudo journalctl -u autoscaler.service -f"
echo "  sudo journalctl -u nginx-health.service -f"
echo "  tail -f /var/log/nginx-health.log"
echo ""
echo "Reload nginx to activate healthz endpoint:"
echo "  sudo nginx -t && sudo systemctl reload nginx"