## Deployment Guide

> **WARNING:** Back up `.env` once generated: it holds N8N_ENCRYPTION_KEY. Losing or regenerating it makes every saved n8n credential unreadable.

### Configuration (`./env` → `.env`)

The only file you edit is the template `./env` (no dot). `env-maker.sh` turns it into `.env` (mode 0600, gitignored), which docker compose, deploy.sh and the systemd units all read.

```bash
./env-maker.sh ./env            # creates .env; refuses if .env exists
./env-maker.sh ./env --force    # overwrite (back up .env first!)
```

| Item | Behaviour |
|------|-----------|
| `replace_ssl` / `replace_password` / `replace_user` / `replace_port` tags | Replaced by fresh random values |
| `DOMAIN` / `EMAIL` left as tags | Random `*.localhost` domain; Traefik serves its self-signed cert (**testing only**) |
| `ACME_CA_SERVER` | Let's Encrypt prod (default) or staging URL |
| `N8N_HOST`, `WEBHOOK_URL`, `GF_SERVER_ROOT_URL` | Derived from `DOMAIN` in docker-compose.yml (not in env) |
| `SCRIPTS_*`, `GITHUB_TOKEN`, `SYNC_INTERVAL_MINUTES` | github_sync settings |
| `AUTOSCALE_*` | Autoscaler settings |

### Prerequisites

- Ubuntu 24.04 Server LTS
- Docker 24.0+ and Docker Compose v2
- Domain name with DNS A record pointing to server
- Root or sudo access

### Quick Deploy (Automatic)

Set `DOMAIN` and `EMAIL` in `./env`, then:

```bash
# 1. Transfer project to VM
tar czf n8n.tar.gz naypyers/
scp n8n.tar.gz user@VM_IP:/home/user/

# 2. On VM: run deploy.sh
tar xf n8n.tar.gz && cd naypyers/
chmod +x deploy.sh
sudo ./deploy.sh
```

Test deployments: see `testing_files/Testing Documentation.md`.

**What deploy.sh does:**
1. Installs prerequisites; runs `env-maker.sh <template>` (default `./env`) only if `.env` is missing
2. Installs Docker
3. Retires legacy nginx / certbot / nginx_health units if present
4. Builds and starts containers (Traefik obtains the certificate on first start)
5. Fixes permissions
6. Installs systemd services (autoscaler, github_sync)
7. Sets up firewall (ufw 22/80/443)

### Manual Deploy

```bash
# 1. Transfer and extract
tar xzf n8n.tar.gz
cd n8n-python-project

# 2. Generate .env from the template (edit ./env first)
./env-maker.sh ./env

# 3. Build and start
docker compose build
docker compose up -d

# 4. Watch Traefik obtain the certificate (Ctrl+C when done)
docker compose logs -f traefik

# 5. Fix permissions (first run only)
sudo chown -R 1000:1000 n8n-data/
sudo chown 1000:1000 scripts/

# 6. Configure firewall
sudo ufw default deny incoming
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP → HTTPS redirect
sudo ufw allow 443/tcp     # HTTPS
sudo ufw enable

# 7. Install systemd services (reads .env)
sudo chmod +x ./autoscaler/setup-systemd.sh
sudo ./autoscaler/setup-systemd.sh

# 8. Verify
docker compose ps
sudo systemctl list-timers --all | grep -E "(autoscaler|github_sync)"
```

### Switching Staging → Production Certificate

Traefik keeps the staging cert in its volume, so delete it after switching:

```bash
# set in .env (mirror it in ./env):
ACME_CA_SERVER='https://acme-v02.api.letsencrypt.org/directory'
# staging: https://acme-staging-v02.api.letsencrypt.org/directory

docker compose down traefik && docker volume rm <project>_traefik-letsencrypt && docker compose up -d traefik
```

### Directory Structure

```
n8n-python-project/
├── docker-compose.yml          # Service orchestration
├── env                         # Config template - the only file you edit
├── env-maker.sh                # Generates .env from ./env
├── .env                        # Generated config, 0600 (not in git)
├── deploy.sh                   # One-command deployment
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
├── HTTPS/                      # Firewall (TLS is handled by Traefik in docker-compose.yml)
│   └── firewall_rules.sh       # UFW setup
├── autoscaler/                 # Scaling automation
│   ├── autoscaler.sh           # Worker scaling logic
│   └── setup-systemd.sh        # Systemd service installer
├── prometheus/                 # Metrics configuration
│   └── prometheus.yml          # Scrape targets (incl. traefik:8080)
├── grafana_dashboards/         # Dashboard definitions
│   └── custom.json             # custom dashboard setup
└── testing_files/              # Test configurations
    ├── Testing Documentation.md  # Test deployment guide
    ├── env                     # Test config template (counterpart of ./env)
    ├── stage.conf              # Hooks for VM/remote staging runners
    ├── standardise_phone.py    # Sample script
    └── test_data.json          # Sample webhook payload
```

### Script Requirements

Place `.py` files in `./scripts/` on host. Scripts must:

1. **Define `result`** variable (will be returned to n8n)
2. **Access input** via `data` global (injected by wrapper)
3. **Declare dependencies** in header comments: `# requires: package==version`
    Note: the requirements checking stops at first non-commented line
4. **Fail with `raise ScriptError("msg")`** for bad input; the message shows in n8n as-is (see 04 -> Error Reporting in Scripts)

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

### Script Ownership and Editing Workflow

Scripts are **not edited locally**. The proper workflow is:

1. **Tech team** edits scripts in GitHub repository
2. **github_sync** service (running as root) pulls changes
3. **Files** in `scripts/` are owned by `root:root` (644 permissions)
4. **Directory** `scripts/` is owned by `1000:1000` for host management
5. **python-api** container reads scripts via `:ro` mount as root