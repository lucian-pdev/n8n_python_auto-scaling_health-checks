# N8N Python Executor

A FastAPI-based Python execution service designed for n8n workflow automation. Features horizontal scaling with queue-based n8n workers, automatic SSL provisioning, nginx reverse proxy, systemd-based autoscaling, and comprehensive monitoring with Prometheus and Grafana.


WORK IN PROGRESS!!!


## Full Documentation is avaiable in the DOCUMENTATION/ directory.

Must edit to account for:
- adding postgres

- the new workflow with postgress (run a logical simulation as far as needed, both sent and receive, use present data for systems not changed to abstract and focus on changes at the Data Flow in file 03_...)

- in-depth settings of n8n nodes related the python container

- the usage of docker logs and other verification commands for troubleshooting investigation

- n8n node setups and links to endpoints

- test the github sync of the scripts/ dir. Note to self: remember that docker COPIES the files from host's scripts/ when starting the container.
-> https://www.geeksforgeeks.org/devops/copying-files-to-and-from-docker-containers/#how-to-docker-copy-files-from-host-to-container-a-stepbystep-guide
How to Mount a Host Directory to a Docker Container?

The following command helps in mounting a host directory to a docker container:

 docker run -v /path/on/host:/path/in/container image_name

    Here, replace <Host path to mount> with your host path mounting directory and <name of the container> with your wishing container's path. 

Example

The following command is an example of mounting the host directory with the container directory:

docker run -v /myfold:/tmp --name  mycontainer1 centos

INFO:
 Should you run docker run -v from the shell, or bake it into the Dockerfile?
✔️ Short answer:

You MUST do it from the shell (or docker-compose).
You CANNOT bake a bind mount into a Dockerfile.
Why?

Because:

    Dockerfile describes the image, not the runtime environment.

    Bind mounts (-v) are runtime-only configuration.

    Dockerfile has no instruction that can define a host path to mount.

    Even if you tried to hack it, the image build context cannot reference arbitrary host paths.

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

