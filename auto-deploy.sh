#!/bin/bash

set -e

# Configuration
DOMAIN="n8n.dashboard.com"      # CHANGE THIS
EMAIL="your-email@example.com"  # CHANGE THIS

NDIRS=( 
    prometheus-data
    grafana-data
#   redis-data
    n8n-data
#   postgres-data
    scripts
)

# Exit if not run from project root (where this script resides)
if [[ ! -f "$PWD/auto-deploy.sh" ]]; then
    echo "ERROR: Must run auto-deploy.sh from the project root directory."
    echo "Current directory: $PWD"
    exit 1
fi

SEPARATOR="=================================================="

echo "$SEPARATOR"
echo "=== Starting deployment for $DOMAIN ==="
echo "$SEPARATOR"

# Install prerequisites
echo "$SEPARATOR"
echo "Installing nginx and certbot..."
echo "$SEPARATOR"
sudo apt update
sudo apt install -y nginx ca-certificates curl gnupg certbot python3-certbot-nginx

echo "$SEPARATOR"
echo "Installing Docker..."
echo "$SEPARATOR"
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin python3-pip

# Ensuring the directories exist and are owned by USER before docker creates them as ROOT out of necessity
for d in "${NDIRS[@]}"; do
    mkdir -p "$d"
done

sudo systemctl enable --now docker
sudo groupadd -f docker
sudo usermod -aG docker "$USER"

echo "$SEPARATOR"
echo "Building and starting services..."
echo "$SEPARATOR"
sudo docker compose build
sudo docker compose up -d

# Ensure permissions are good for directories needed by containers
echo "$SEPARATOR"
echo "Ensuring permissions..."
echo "$SEPARATOR"
sudo chown -R 1000:1000 n8n-data/ 2>/dev/null || true
sudo chown -R 1000:1000 scripts/ 2>/dev/null || true

# Install systemd services
echo "$SEPARATOR"
echo "Installing systemd services..."
echo "$SEPARATOR"
sudo chmod +x ./autoscaler/setup-systemd.sh
sudo ./autoscaler/setup-systemd.sh

# Configure nginx
echo "$SEPARATOR"
echo "Configuring nginx..."
echo "$SEPARATOR"
sed -i "s/n8n.dashboard.com/$DOMAIN/g" ./HTTPS/nginx_before_ssl.conf
sed -i "s/n8n.dashboard.com/$DOMAIN/g" ./HTTPS/nginx.conf
sudo cp ./HTTPS/nginx_before_ssl.conf /etc/nginx/sites-available/n8n_grafana.conf
sudo ln -sf /etc/nginx/sites-available/n8n_grafana.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t && sudo systemctl reload nginx

# SSL certificate
echo "$SEPARATOR"
echo "Obtaining SSL certificate..."
echo "$SEPARATOR"
sudo mkdir -p /var/www/certbot
sudo certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" \
    --non-interactive --agree-tos -m "$EMAIL" || {
    echo "Certbot failed. Trying standalone method..."
    sudo certbot certonly --standalone -d "$DOMAIN" \
        --non-interactive --agree-tos -m "$EMAIL" || {
        echo "$SEPARATOR"
        echo "Certbot failed completely. Check domain points to this server. Proceeding without SSL."
        echo "$SEPARATOR"
        sudo cp ./HTTPS/nginx.conf /etc/nginx/sites-available/n8n_grafana.conf
        sudo ln -sf /etc/nginx/sites-available/n8n_grafana.conf /etc/nginx/sites-enabled/
        sudo rm -f /etc/nginx/sites-enabled/default
        sudo nginx -t && sudo systemctl reload nginx
        exit 1
    }
}

# Upgrading HTTP to HTTPSecure
echo "$SEPARATOR"
echo "SSL certificates aquired, proceeding with reloading Nginx..."
echo "$SEPARATOR"
sudo cp ./HTTPS/nginx.conf /etc/nginx/sites-available/n8n_grafana.conf
sudo ln -sf /etc/nginx/sites-available/n8n_grafana.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t && sudo systemctl reload nginx

# Firewall
echo "$SEPARATOR"
echo "Configuring firewall..."
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
echo "$SEPARATOR"
echo "$SEPARATOR"