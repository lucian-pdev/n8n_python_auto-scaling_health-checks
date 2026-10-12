## Autoscaling & Health Monitoring

### n8n-Worker Autoscaler

Automatically scales `n8n-worker` containers based on the n8n job queue in Redis.

**Configuration** (in `.env`, read via `EnvironmentFile=`; no reinstall needed after changes):
| Variable | Default | Description |
|----------|---------|-------------|
| `AUTOSCALE_MIN_REPLICAS` | 1 | Minimum worker containers |
| `AUTOSCALE_MAX_REPLICAS` | 10 | Maximum worker containers |
| `AUTOSCALE_THRESHOLD_UP` | 4 | Scale up when more than N jobs wait in the queue |
| `AUTOSCALE_COOLDOWN` | 60 | Seconds between scale events |

Runs `docker compose -f $PROJECT_DIR/docker-compose.yml --project-directory $PROJECT_DIR --scale n8n-worker=N` (`PROJECT_DIR` is set by the unit).

**Metric:** n8n's Bull queue in Redis, read every 30s with `redis-cli LLEN`:
- `bull:jobs:wait`: jobs waiting for a free worker slot
- `bull:jobs:active`: jobs running now

Scale up by 1 when `wait > AUTOSCALE_THRESHOLD_UP`. Scale down by 1 when `wait == 0` and `active` fits in one fewer worker (`(replicas-1) * 4`, matching `--concurrency=4`). If Redis can't be read, nothing changes.

```bash
# What the autoscaler sees
docker compose exec -T redis redis-cli LLEN bull:jobs:wait
docker compose exec -T redis redis-cli LLEN bull:jobs:active
```

**Management:**
```bash
# View timer status
sudo systemctl status autoscaler.timer

# View logs
sudo journalctl -u autoscaler.service -f

# Disable autoscaling
sudo systemctl stop autoscaler.timer
sudo systemctl disable autoscaler.timer
```

### Traefik Health

No host timer: the container healthcheck (`traefik healthcheck --ping`, every 30s) plus `restart: unless-stopped` keeps Traefik alive.

**Management:**
```bash
# Health status
docker inspect --format '{{.State.Health.Status}}' traefik

# Logs (routing, ACME, access log)
docker compose logs -f traefik

# Dashboard (from your workstation)
ssh -L 8082:127.0.0.1:8082 user@VM   # then open http://127.0.0.1:8082/dashboard/
```

### GitHub Sync

Automatically pulls vetted scripts from GitHub repository.

**Configuration** (in `.env`, read via `EnvironmentFile=`):
| Variable | Default | Description |
|----------|---------|-------------|
| `SCRIPTS_REPO` | Required | HTTPS URL to GitHub repo |
| `SCRIPTS_BRANCH` | `main` (template sets `scripts`) | Branch to track |
| `GITHUB_TOKEN` | empty | Private repos; sent as `http.extraHeader`, never stored in `scripts/.git/config` |
| `SYNC_INTERVAL_MINUTES` | 5 | Sync frequency; baked into the timer at install, rerun `setup-systemd.sh` after changing |

**Script Ownership Behavior:**

| Sync Run | Directory | Files | Result |
|----------|-----------|-------|--------|
| First | Created as 1000:1000 if needed | Cloned as root:root | Success |
| Subsequent | Unchanged | Updated as root:root | Success |

**Management:**
```bash
# Manual sync
sudo systemctl start github_sync.service

# View logs
sudo journalctl -u github_sync.service -f
```

### Systemd Services Reference

| Service | Type | Trigger | Purpose |
|---------|------|---------|---------|
| `autoscaler.service` | oneshot | `autoscaler.timer` (30s) | Scale workers |
| `github_sync.service` | oneshot | `github_sync.timer` (`SYNC_INTERVAL_MINUTES`) | Sync scripts from GitHub |