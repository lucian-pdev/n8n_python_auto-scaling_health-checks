#!/usr/bin/env python3

"""
FastAPI + Uvicorn Python container.
It receives HTTP requests on the same virtual network, runs each one as a job in its own subprocess
(at most N at once), then returns the result to the caller (designed for n8n).

Expected JSON format from n8n:
{
  "data": {{ $json }},
  "code_file_name": "script_name.py"
}

Returns: JSON over HTTP, when the job is finished (sync). The whole request has a 300 s budget
(queue wait + attempts): the n8n HTTP Request node's timeout must be above that.

**Inclusion of new packages in python script files on github**
# requires: pandas==x.y.c, numpy==x.y.c
```py
def example_function(A, B):
    import pandas
    # ... your code here today!

result = example_function(data["stuff"], data["other_stuff"])
```

**Errors** (HTTP stays 200; check result.status in n8n)
- `raise ScriptError("msg")` (injected, no import) -> result.error = "msg"
- any other exception -> result.error = "<Type>: <msg> (line N in <func>)"
- status values: success | fatal. Infrastructure hiccups (timeout, memory cap, crashed subprocess)
  are retried inside python-api; when retries or time run out: fatal, with result.last_error.

"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Uvicorn as HTTP server inside python-api container
import uvicorn
from exceptions import FatalError, JobNotFound

# FastAPI for HTTP request handling
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

# Prometheus for metrics
from metrics import (
    ACTIVE_JOBS,
    JOB_RETRIES,
    PARENT_RSS,
    QUEUE_SIZE,
    REQUEST_COUNT,
    REQUEST_DURATION,
    SCRIPT_EXECUTION_DURATION,
    SCRIPT_EXECUTIONS,
    update_container_metrics,
)
from packagemanager import PackageManager
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # type: ignore
from pydantic import BaseModel
from workerv2 import IN_PROGRESS, TODO, Runner, max_concurrent_jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # uvicorn only sets up its own loggers
logger = logging.getLogger("python-api")


##############################################################
# Configuration
##############################################################
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", "/app/scripts")
VENV_BASE = Path(os.environ.get("VENV_BASE", "/app/venvs"))
DISCONNECT_POLL_SECONDS = 1


def _parent_rss_bytes() -> int:
    """VmRSS of this process, 0 where /proc is missing (local run on macOS)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


##############################################################
# FastAPI area
##############################################################
class ExecPayload(BaseModel):
    data: dict[str, Any]
    code_file_name: str


runner: Runner  # set in lifespan: PackageManager runs pip at start


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runner
    runner = Runner(
        PackageManager(SCRIPTS_DIR, VENV_BASE),
        max_concurrent=max_concurrent_jobs(),
        on_retry=lambda job, error: JOB_RETRIES.labels(script_name=job.script).inc(),
    )
    logger.warning(f"python-api ready: at most {runner.max_concurrent} jobs at once")  # warning: uvicorn shows it

    # For Prometheus/grafana
    async def metrics_updater():
        while True:
            counts = runner.active.counts()
            QUEUE_SIZE.set(counts[TODO])
            ACTIVE_JOBS.set(counts[IN_PROGRESS])
            PARENT_RSS.set(_parent_rss_bytes())
            update_container_metrics()
            await asyncio.sleep(10)

    metrics_task = asyncio.create_task(metrics_updater())

    yield # FastAPI runs here

    metrics_task.cancel()
    try:
        await metrics_task
    except asyncio.CancelledError:
        pass    # Expected when cancelled


class PrometheusMiddleware:
    """Plain ASGI middleware. Not @app.middleware("http"): that one wraps `receive`, so the endpoint
    never sees n8n hanging up and a cancelled request's subprocess would keep running."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start_time = time.time()
        status = 500

        async def send_with_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            route = scope.get("route")  # template like /jobs/{job_id}, so labels stay bounded
            REQUEST_DURATION.observe(time.time() - start_time)
            REQUEST_COUNT.labels(
                method=scope["method"],
                endpoint=route.path if route else "unmatched",
                status=status
            ).inc()


app = FastAPI(lifespan=lifespan)
app.add_middleware(PrometheusMiddleware)


async def _run_until_disconnect(request: Request, job: asyncio.Task) -> Any:
    """Wait for the job; if n8n hangs up first, cancel it (the runner kills the subprocess and drops the row)."""
    while True:
        done, _ = await asyncio.wait({job}, timeout=DISCONNECT_POLL_SECONDS)
        if done:
            return job.result()
        if await request.is_disconnected():
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            raise HTTPException(status_code=499, detail="client disconnected")


@app.post("/execute")
async def execute(payload: ExecPayload, request: Request):
    script_name = os.path.basename(payload.code_file_name)  # no paths out of SCRIPTS_DIR
    if not (Path(SCRIPTS_DIR) / script_name).is_file():
        raise HTTPException(status_code=404, detail=f"Script not found: {script_name}")

    start_time = time.time()
    try:
        result = await _run_until_disconnect(request, asyncio.create_task(runner.run(script_name, payload.data)))
    except FatalError as e:
        SCRIPT_EXECUTIONS.labels(script_name=script_name, status=e.status).inc()
        logger.warning("job %s %s %s: %s", e.job_id, script_name, type(e).__name__, e.message)  # docker compose logs python-api
        # "error" + "error_type" keep the shape the n8n workflows read (result.status / result.error)
        return {"result": {**e.to_dict(), "error": e.message, "error_type": type(e).__name__}}

    process_time = time.time() - start_time
    SCRIPT_EXECUTIONS.labels(script_name=script_name, status="success").inc()
    SCRIPT_EXECUTION_DURATION.labels(script_name=script_name).observe(process_time)
    return {"result": {"status": "success", "result": result, "process_time": process_time}}

##############################################################
# Bonus: /scripts endpoint
##############################################################
# Returns available scripts for n8n dropdown population:
# {
#   "scripts": ["normalize_mobile.py", "validate_email.py", "geocode_address.py"]
# }

@app.get("/scripts")
async def list_scripts():
    """List available scripts for n8n dropdown"""
    try:
        files = [f for f in os.listdir(SCRIPTS_DIR) if f.endswith(".py")]
        return {"scripts": files}
    except FileNotFoundError:
        return {"scripts": []}

##############################################################
# Health Check endpoint area
##############################################################

@app.get("/health")
async def health_check():
    counts = runner.active.counts()
    return {
        "status": "ok",
        "jobs": {"todo": counts[TODO], "in_progress": counts[IN_PROGRESS]},
        "max_concurrent": runner.max_concurrent,
        "rss_mb": round(_parent_rss_bytes() / 1024 / 1024, 1),
    }

@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Read-only view of an unfinished job, for debugging. Finished jobs are gone: 404."""
    try:
        job = runner.active.get(job_id)
    except JobNotFound as e:
        raise HTTPException(status_code=404, detail=e.message) from None
    return {
        "job_id": job.job_id,
        "script": job.script,
        "status": job.status,
        "attempts": job.attempts,
        "age_seconds": round(time.time() - job.created_at, 1),
        "time_left": round(job.time_left(), 1),
    }

@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


##############################################################
# Main area
##############################################################

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
