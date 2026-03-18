#!/bin/bash

set -e

# Configuration
DOMAIN="n8n.dashboard.com"      # CHANGE THIS
EMAIL="your-email@example.com"  # CHANGE THIS

echo "=== Starting deployment for $DOMAIN ==="

# Install prerequisites
echo "Installing nginx and certbot..."
sudo apt update
sudo apt install -y nginx ca-certificates curl gnupg certbot python3-certbot-nginx

echo "Installing Docker..."
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin python3-pip

sudo systemctl enable --now docker
sudo groupadd -f docker
sudo usermod -aG docker "$USER"

echo "Building and starting services..."
docker compose build
docker compose up -d

# Fix permissions AFTER containers create directories
echo "Fixing permissions..."
sudo chown -R 1000:1000 n8n-data/ 2>/dev/null || true
sudo chown -R 1000:1000 scripts/ 2>/dev/null || true

# Install systemd services
echo "Installing systemd services..."
sudo chmod +x ./autoscaler/setup-systemd.sh
sudo ./autoscaler/setup-systemd.sh

# Configure nginx
echo "Configuring nginx..."
sudo cp ./HTTPS/nginx.conf /etc/nginx/sites-available/n8n_grafana.conf
sudo ln -sf /etc/nginx/sites-available/n8n_grafana.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default  # Remove default site

sudo nginx -t && sudo systemctl reload nginx

# SSL certificate
echo "Obtaining SSL certificate..."
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" || {
    echo "Certbot failed. Check domain points to this server."
    exit 1
}

# Firewall
echo "Configuring firewall..."
sudo chmod +x ./HTTPS/firewall_rules.sh
sudo ./HTTPS/firewall_rules.sh

echo "=== Deployment complete ==="
echo "Access n8n at: https://$DOMAIN"
echo "Access Grafana at: https://$DOMAIN/grafana/"
echo ""
echo "Check status with: docker compose ps"
echo "View logs with: docker compose logs -f"