## Maintenance

### Worker Restart Cycle

Workers automatically restart every 5 minutes via `restart_loop()` to prevent memory leaks. Uses `DRAIN` signal for graceful handoff.

**Manual restart:**
```bash
docker compose restart python-api
```

### Script Updates

Scripts are cached by modification time. To force reload:

1. Modify script in `./scripts/`
2. New mtime detected on next request
3. New content loaded and cached

**Or use GitHub sync:**
```bash
sudo systemctl start github_sync.service
```

### SSL Certificate Renewal

Certbot auto-renews via systemd timer. Verify with:
```bash
sudo certbot renew --dry-run
```

### Venv Cleanup

Remove old venvs manually if needed:
```bash
docker compose exec python-api python -c "
from packagemanager import PackageManager
pm = PackageManager()
pm._cleanup_old_venvs(max_age_days=1)
"
```

### Backup Strategy

| Data | Location | Backup Method |
|------|----------|---------------|
| n8n workflows | `./n8n-data/` | `tar czf backup-n8n-$(date +%Y%m%d).tar.gz n8n-data/` |
| scripts | `./scripts/` or GitHub repo | `git push` or `tar czf scripts.tar.gz scripts/` |
| grafana dashboards | `./grafana_data/` | Export via UI or volume backup |
| prometheus data | `./prometheus_data/` | Optional (metrics ephemeral) |

---

## NOTICE

This project uses the following third-party software. See their respective repositories for full license texts.

### Python Dependencies

| Package | License |
|---------|---------|
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| pydantic | MIT |
| prometheus-client | Apache-2.0 |

### Container Images

| Component | License |
|-----------|---------|
| n8n | [Fair-code](https://docs.n8n.io/reference/license/) |
| Redis | BSD-3-Clause |
| Prometheus | Apache-2.0 |
| Grafana | AGPL-3.0 |
| cadvisor | Apache-2.0 |
| node-exporter | Apache-2.0 |

---

**End of README**