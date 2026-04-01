## Autoscaling & Health Monitoring

### n8n-Worker Autoscaler

Automatically scales `n8n-worker` containers based on Prometheus metrics.

**Configuration** (in `autoscaler/autoscaler.sh`):
| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_REPLICAS` | 1 | Minimum worker containers |
| `MAX_REPLICAS` | 10 | Maximum worker containers |
| `QUEUE_THRESHOLD_UP` | 20 | Scale up when queue depth exceeds |
| `QUEUE_THRESHOLD_DOWN` | 5 | Scale down when queue depth below |
| `SCALE_COOLDOWN` | 60 | Seconds between scale events |

**Metric:** `rate(py_api_requests_total[5m])` (proxy for queue depth)

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

### Nginx Health Monitor

Ensures nginx stays running and responsive.

**Management:**
```bash
# View logs
sudo tail -f /var/log/nginx_health.log
sudo journalctl -u nginx_health.service -f

# Manual test
sudo /usr/local/bin/nginx_health.sh
```

### GitHub Sync

Automatically pulls vetted scripts from GitHub repository.

**Configuration** (in `autoscaler/setup-systemd.sh`):
| Variable | Default | Description |
|----------|---------|-------------|
| `SCRIPTS_REPO` | Required | HTTPS URL to GitHub repo |
| `SCRIPTS_BRANCH` | `main` | Branch to track |
| `SYNC_INTERVAL_MINUTES` | 5 | Sync frequency |

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
| `nginx_health.service` | oneshot | `nginx_health.timer` (2m) | Keep nginx alive |
| `github_sync.service` | oneshot | `github_sync.timer` (5m) | Sync scripts from GitHub |