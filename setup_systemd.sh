#!/bin/bash

# Run this to install all systemd services for monitoring and scaling

set -e

# SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/usr/local/bin"

echo "Installing health checker..."

# Install scripts
sudo cp "./HTTPS/nginx_health.sh" "$TARGET_DIR/"
sudo chmod +x "$TARGET_DIR/nginx_health.sh"

# Create log file
sudo touch /var/log/nginx_health.log
sudo chmod 644 /var/log/nginx_health.log

# Install bc if not present
sudo apt-get update && sudo apt-get install -y bc jq

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

# Reload systemd and enable services
sudo systemctl daemon-reload

sudo systemctl enable nginx_health.timer
sudo systemctl start nginx_health.timer

echo ""
echo "=== Services Installed ==="
echo ""
echo "Timers:"
sudo systemctl list-timers --all | grep -E "(nginx_health|NEXT)"
echo ""
echo "Status:"
sudo systemctl status nginx_health.timer --no-pager 2>/dev/null | head -5
echo ""
echo "Logs:"
echo "  sudo journalctl -u nginx_health.service -f"
echo "  tail -f /var/log/nginx_health.log"
echo ""
echo "Reload nginx to activate healthz endpoint:"
echo "  sudo nginx -t && sudo systemctl reload nginx"