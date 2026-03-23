# N8N Python Executor

A FastAPI-based Python execution service designed for n8n workflow automation. Features horizontal scaling with queue-based n8n workers, automatic SSL provisioning, nginx reverse proxy, systemd-based autoscaling, and comprehensive monitoring with Prometheus and Grafana.


WORK IN PROGRESS!!!


## Full Documentation is avaiable in the DOCUMENTATION/ directory.


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

**Notes:**
- Grafana is used unmodified via official Docker image. AGPL-3.0 applies if modified or redistributed.
- n8n's Sustainable Use License restricts high-volume production use; review terms before scaling.
- Full license texts: https://opensource.org/licenses

I do NOT take responsability for any use of these files.
I do NOT restrict or monetize them.
I do NOT employ myself to provide support, maintenance, or updates.

