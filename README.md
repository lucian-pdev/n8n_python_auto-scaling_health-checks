# N8N Python Executor

A FastAPI-based Python execution service designed for n8n workflow automation. Features horizontal scaling with queue-based n8n workers, Traefik reverse proxy with automatic Let's Encrypt TLS, systemd-based autoscaling, and comprehensive monitoring with Prometheus and Grafana.


## Full Documentation is avaiable in the DOCUMENTATION/ directory.


## Supported deployments

| Deployment | Branch | What it adds |
|------------|--------|--------------|
| Proxmox VM (office server, "linksys") | this branch | nothing: the stack as documented here, deployed fresh with `deploy.sh` |
| Hostinger VPS | `hostinger` | the NB database stack (`docker-compose.nb.yml` + `nb.sh`: Postgres + PostgREST on the main stack's network), the upgrade procedure from the older nginx-based version, and its rehearsal hooks |

Fixes to the shared stack go here first, then get merged into `hostinger`.


## Upgrades and OS updates

`upgrade/` holds the upgrade workflow: back up, switch code, carry the `.env` over, prove the new version uses
the same data, passwords and keys, stop the databases cleanly, deploy, verify. Also an OS update (packages +
reboot) that never lets Docker kill a database mid-write. Read `upgrade/README.md`; settings in `upgrade/upgrade.conf`.
The n8n image version is pinned in `.env` (`N8N_VERSION`): updating n8n is a deliberate `.env` change that goes
through the same workflow.


## NOTICE

This project uses the following third-party software. See their respective repositories for full license texts.

### Python Dependencies

| Package | License |
|---------|---------|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Pydantic | MIT |
| prometheus-client | Apache-2.0 |

### Container Images

| Component | License |
|-----------|---------|
| Prometheus | Apache-2.0 |
| Grafana | AGPL-3.0 |
| cAdvisor | Apache-2.0 |
| Node Exporter | Apache-2.0 |
| n8n | [Sustainable Use License](https://github.com/n8n-io/n8n/blob/master/LICENSE.md) |
| Redis | BSD-3-Clause |
| Traefik | MIT |

**Notes:**
- Grafana is used unmodified via official Docker image. AGPL-3.0 applies if modified or redistributed.
- n8n's Sustainable Use License restricts high-volume production use; review terms before scaling.
- Full license texts: https://opensource.org/licenses

I do NOT take responsability for any use of these files.
I do NOT restrict or monetize them.
I do NOT employ myself to provide support, maintenance, or updates.

