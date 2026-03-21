#!/usr/bin/env python3

"""
FastAPI + Uvicorn Python container.
It will receive HTTP requests on the same virtual network, queue them in a workerpool, then return the results to the caller (desgined for n8n).

Expected JSON format from n8n:
{
  "data": {{ $json }},
  "code_file_name": "script_name.py"
}

Returns: JSON over HTTP

**Inclusion of new packages in python script files on github**
# requires: pandas==x.y.c, numpy==x.y.c
```py
def example_function(A, B):
    import pandas
    # ... your code here today!
    
result = example_function(data["stuff"], data["other_stuff"]) 
```

"""

import os
import time
import asyncio
from pydantic import BaseModel
from typing import Dict, Any
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
# Prometheus for metrics
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST # type: ignore
# FastAPI for HTTP request handling
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
# Uvicorn as HTTP server inside python-api container
import uvicorn
from exceptions import JobRetryingException


##############################################################
# Configuration
##############################################################
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", "/app/scripts")
VENV_BASE = Path(os.environ.get("VENV_BASE", "/app/venvs"))
SCRIPTS_REPO = os.environ.get("SCRIPTS_REPO")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
MAX_VENV_SIZE_GB = float(os.environ.get("MAX_VENV_SIZE_GB", "10"))

##############################################################
# Prometheus Metrics
##############################################################
REQUEST_COUNT = Counter('py_api_requests_total', 'Total requests', ['method', 'endpoint', 'status'])
REQUEST_DURATION = Histogram('py_api_request_duration_seconds', 'Request duration')
ACTIVE_WORKERS = Gauge('py_api_active_workers', 'Number of active worker processes')
QUEUE_SIZE = Gauge('py_api_queue_size', 'Current job queue size')
WORKER_RESTARTS = Counter('py_api_worker_restarts_total', 'Total worker restarts')
SCRIPT_EXECUTIONS = Counter('py_api_script_executions_total', 'Script executions', ['script_name', 'status'])
VENV_CREATIONS = Counter('py_api_venv_creations_total', 'Virtual environment creations')
VENV_CACHE_HITS = Counter('py_api_venv_cache_hits_total', 'Virtual environment cache hits')

##############################################################
# Virtual environment handling area
##############################################################
# local files
from worker import WorkerManager

##############################################################
# Script loader area
##############################################################
_script_cache: dict[str, tuple[float, str]] = {}
_script_lock = asyncio.Lock()

async def load_scripts(code_file_name: str) -> tuple[str, str]:
    name = os.path.basename(code_file_name)
    file_path = Path(SCRIPTS_DIR) / name

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Script not found: {name}")

    current_mtime = file_path.stat().st_mtime

    # Fast path: no lock
    cached = _script_cache.get(name)
    if cached and cached[0] == current_mtime:
        return name, cached[1]

    # Slow path: lock + re-check + load
    async with _script_lock:
        cached = _script_cache.get(name)
        if cached and cached[0] == current_mtime:
            return name, cached[1]

        code = await asyncio.to_thread(file_path.read_text)
        _script_cache[name] = (current_mtime, code)
        return name, code

##############################################################
# FastAPI area
##############################################################
class ExecPayload(BaseModel):
    data: Dict[str, Any]
    code_file_name: str

manager = WorkerManager(num_workers=4)

@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start_workers()
    
    # For Prometheus/grafana
    async def metrics_updater():
        while True:
            # Approximate queue size (multiprocessing.Queue doesn't expose size reliably)
            QUEUE_SIZE.set(0)  # Placeholder - would need custom queue for accurate size
            await asyncio.sleep(10)
    
    # BACKGROUND TASK: runs concurrently with FastAPI
    async def restart_loop():
        while True:
            await asyncio.sleep(300)  # 5 minutes
            manager.restart_oldest_worker()
    
    metrics_task = asyncio.create_task(metrics_updater())
    restart_task = asyncio.create_task(restart_loop())
    
    yield # FastAPI runs here

    # Request cancellation
    metrics_task.cancel()
    restart_task.cancel()
        
    # Shutdown: cancel loop, stop workers
  
    try:
        # Wait for task to actually stop
        await metrics_task
        await restart_task
    except asyncio.CancelledError:
        pass    # Expected when cancelled
    manager.stop_workers()
    

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    
    REQUEST_DURATION.observe(duration)
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code
    ).inc()
    
    return response

@app.post("/execute")
async def execute(payload: ExecPayload):
    script_name, code = await load_scripts(payload.code_file_name)
    
    job_id = str(uuid.uuid4())
    manager.submit(job_id, script_name, code, payload.data)
    
    try:
        result = manager.get_result(job_id)
        
        SCRIPT_EXECUTIONS.labels(
            script_name=payload.code_file_name,
            status=result.get("status", "unknown") if result else "unknown"
        ).inc()
        
        return {"result": result}
        
    except JobRetryingException as e:
        # Return 202 Accepted for retry scenario
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=202,
            content={
                "result": None,
                "status": "retrying",
                "message": e.message,
                "job_id": e.job_id,
                "attempt": e.attempt,
                "script_name": e.script_name
            }
        )

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
    return {"status": "ok", "workers": len(manager.workers)}

@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


##############################################################
# Main area
##############################################################

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)