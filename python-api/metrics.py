#!/usr/bin/env python3
"""Prometheus metrics, in their own module so main.py and packagemanager.py can share them without a circular import.
Labels stay bounded: route templates and known script names only."""

from prometheus_client import Counter, Gauge, Histogram  # type: ignore

REQUEST_COUNT = Counter('py_api_requests_total', 'Total requests', ['method', 'endpoint', 'status'])
REQUEST_DURATION = Histogram('py_api_request_duration_seconds', 'Request duration')
ACTIVE_JOBS = Gauge('py_api_active_jobs', 'Jobs with an attempt running (status 2)')
QUEUE_SIZE = Gauge('py_api_queue_size', 'Jobs waiting for a slot or the next attempt (status 1)')
PARENT_RSS = Gauge('py_api_parent_rss_bytes', 'Resident memory of the python-api parent process')
SUBPROCESSES = Counter('py_api_subprocesses_total', 'Wrapper subprocesses by exit code (negative = killed by signal)',
                       ['script_name', 'exit_code'])
SCRIPT_EXECUTIONS = Counter('py_api_script_executions_total', 'Script executions', ['script_name', 'status'])
VENV_CREATIONS = Counter('py_api_venv_creations_total', 'Virtual environment creations')
VENV_CACHE_HITS = Counter('py_api_venv_cache_hits_total', 'Virtual environment cache hits')
SCRIPT_EXECUTION_DURATION = Histogram(
    'py_api_script_execution_duration_seconds',
    'Per-script execution time in seconds',
    ['script_name']
)
JOB_RETRIES = Counter(
    'py_api_job_retries_total',
    'Jobs retried due errors',
    ['script_name']
)
VENV_CORRUPTIONS = Counter(
    'py_api_venv_corruptions_total',
    'Venv corruption events detected'
)

# The container's own cgroup (v2): parent + every job subprocess together. Read by python-api itself, so the numbers
# don't depend on cAdvisor (v0.47 does not register Docker containers on some cgroup v2 hosts).
CGROUP = "/sys/fs/cgroup"
CONTAINER_MEMORY = Gauge('py_api_container_memory_bytes', 'Container memory in use (cgroup memory.current, incl. page cache)')
CONTAINER_MEMORY_ANON = Gauge('py_api_container_memory_anon_bytes', 'Container anonymous memory (cgroup memory.stat anon: heaps, no cache)')
CONTAINER_MEMORY_LIMIT = Gauge('py_api_container_memory_limit_bytes', 'Container memory limit (cgroup memory.max, 0 = none)')
CONTAINER_CPU_SECONDS = Gauge('py_api_container_cpu_seconds', 'Container CPU time used since start (cgroup cpu.stat usage_usec): rate() = CPUs busy')
CONTAINER_OOM_KILLS = Gauge('py_api_container_oom_kills', 'Processes the kernel OOM-killed in this container (cgroup memory.events oom_kill)')


def _cgroup_keys(name: str) -> dict[str, int]:
    with open(f"{CGROUP}/{name}") as f:
        return {k: int(v) for k, v in (line.split() for line in f)}


def update_container_metrics():
    """Refresh the container gauges; silently skipped where there is no cgroup v2 (local run outside a container)."""
    try:
        with open(f"{CGROUP}/memory.current") as f:
            CONTAINER_MEMORY.set(int(f.read()))
        with open(f"{CGROUP}/memory.max") as f:
            limit = f.read().strip()
        CONTAINER_MEMORY_LIMIT.set(0 if limit == "max" else int(limit))
        CONTAINER_MEMORY_ANON.set(_cgroup_keys("memory.stat").get("anon", 0))
        CONTAINER_CPU_SECONDS.set(_cgroup_keys("cpu.stat").get("usage_usec", 0) / 1e6)
        CONTAINER_OOM_KILLS.set(_cgroup_keys("memory.events").get("oom_kill", 0))
    except (OSError, ValueError):
        pass
