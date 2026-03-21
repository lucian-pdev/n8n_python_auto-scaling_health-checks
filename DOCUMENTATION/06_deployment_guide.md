## Deployment Guide

> **WARNING:** n8n docker container's settings: admin/pass, N8N_ENCRYPTION_KEY and Grafana's admin/password at .env are PLACEHOLDERS. Change before production use.

### Environment Selection

```bash
# For Production, the original files are in the repo root dir by default, there's also a backup like so:
cp testing_files/original_for_prod/.env .env
docker compose up -d

# For Testing
cp testing_files/.env .env
docker compose up -d
```

### Prerequisites

- Ubuntu 24.04 Server LTS
- Docker 24.0+ and Docker Compose v2
- Domain name with DNS A record pointing to server
- Root or sudo access

### Quick Deploy (Automatic)

Edit `auto-deploy.sh` with your DOMAIN and EMAIL, then:

```bash
# 1. Transfer project to VM
tar czf n8n.tar.gz n8n_python_project_auto-scaling_health-checks/
scp n8n.tar.gz user@VM_IP:/home/user/

# 2. On VM: run auto-deploy
tar xf n8n.tar.gz && cd n8n_python_project_auto-scaling_health-checks/
chmod +x auto-deploy.sh
sudo ./auto-deploy.sh
```

**What auto-deploy.sh does:**
- Installs nginx, certbot, Docker
- Builds and starts containers
- Installs systemd services (autoscaler, nginx_health, github_sync)
- Configures nginx with SSL
- Sets up firewall (ufw)
- Fixes permissions

### Manual Deploy

```bash
# 1. Transfer and extract
tar xzf n8n.tar.gz
cd n8n-python-project

# 2. Build and start
docker compose build
docker compose up -d

# 3. Fix permissions (first run only)
sudo chown -R 1000:1000 n8n-data/
sudo chown -R 1000:1000 scripts/

# 4. Configure nginx manually
sudo cp HTTPS/nginx.conf /etc/nginx/sites-available/n8n_grafana.conf
sudo ln -sf /etc/nginx/sites-available/n8n_grafana.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 5. Obtain SSL certificate
sudo certbot --nginx -d n8n.dashboard.com --non-interactive --agree-tos -m your-email@example.com

# 6. Configure firewall
sudo ufw default deny incoming
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP → HTTPS redirect
sudo ufw allow 443/tcp     # HTTPS
sudo ufw enable

# 7. Install systemd services
sudo chmod +x ./autoscaler/setup-systemd.sh
sudo ./autoscaler/setup-systemd.sh

# 8. Verify
docker compose ps
sudo systemctl list-timers --all | grep -E "(autoscaler|nginx_health|github_sync)"
```

### Directory Structure

```
n8n-python-project/
├── docker-compose.yml          # Service orchestration
├── .env                        # Environment variables (not in git)
├── auto-deploy.sh              # One-command deployment
├── DOCUMENTATION
├── python-api/                 # FastAPI service build context
│   ├── Dockerfile              # Python 3.12 slim image
│   ├── main.py                 # FastAPI app, metrics, lifespan
│   ├── worker.py               # Multiprocessing worker pool
│   ├── packagemanager.py       # Venv creation and caching
│   ├── wrapper.py              # Subprocess code executor
│   ├── exceptions.py           # Custom exceptions
│   └── requirements.txt        # fastapi, uvicorn, pydantic, prometheus-client
├── scripts/                    # User Python scripts (mounted RO)
│   └── *.py                    # Your business logic
├── HTTPS/                      # SSL and nginx configuration
│   ├── nginx.conf              # Production nginx config
│   ├── nginx_before_ssl.conf   # Pre-certbot config
│   ├── nginx_health.sh         # Health check script
│   └── firewall_rules.sh       # UFW setup
├── autoscaler/                 # Scaling automation
│   ├── autoscaler.sh           # Worker scaling logic
│   └── setup-systemd.sh        # Systemd service installer
├── prometheus/                 # Metrics configuration
│   └── prometheus.yml          # Scrape targets
├── grafana_dashboards/         # Dashboard definitions
│   └── custom.json             # custom dashboard setup
└── testing_files/              # Test configurations
    ├── .env                    # Test environment
    ├── test_data.json          # Sample webhook payload
    ├── nginx_test.conf         # Test nginx config
    └── original_for_prod/      # Production templates
        └── .env                # Production environment template
```

### Script Requirements

Place `.py` files in `./scripts/` on host. Scripts must:

1. **Define `result`** variable (will be returned to n8n)
2. **Access input** via `data` global (injected by wrapper)
3. **Declare dependencies** in header comments: `# requires: package==version`
    Note: the requirements checking stops at first non-commented line

**Example script:**
```python
# scripts/process_person.py
# requires: phonenumbers==8.13.0

import phonenumbers

person = data.get("body", {}).get("payload", {}).get("person", {})

# Normalize phone
phone = person.get("phone")
if phone:
    parsed = phonenumbers.parse(phone, None)
    person["phone_normalized"] = phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164
    )

result = {"person": person, "processed": True}
```