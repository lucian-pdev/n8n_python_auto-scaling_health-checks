#!/usr/bin/env python3

import asyncio
import json
import logging
import math
import os
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from exceptions import (
    JobError,
    JobNotFound,
    JobTimeout,
    MemoryCapExceeded,
    RetriesExhausted,
    RetryableError,
    ScriptError,
    WorkerCrashed,
)
from metrics import SUBPROCESSES
from packagemanager import PackageManager

logger = logging.getLogger(__name__)
WRAPPER_PATH = Path("/app/wrapper.py")

##############################################################
# Jobs and Queues
##############################################################
##### CONFIG #####
TODO, IN_PROGRESS = 1, 2   # a finished job leaves the queue, so there is no "done"/"failed" row
MAX_ATTEMPTS = 3           # 1 try + 2 retries (RetryableError only)
REQUEST_BUDGET_SECONDS = int(os.environ.get("REQUEST_BUDGET_SECONDS", "300"))  # one budget for the whole request: queue wait + every attempt + pauses

@dataclass
class Job:
    """One request from n8n, from arrival until its answer is sent. The result and the error are not stored:
    the result is the return value, the error is an exception (see exceptions.py).
    """
    script: str     # name only; the code is loaded when the job runs, so the row stays small
    data: dict
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: int = TODO
    attempts: int = 0
    started_at: float | None = None
    created_at: float = field(default_factory=time.time)

    def start(self):
        """Begin an attempt: status 2, attempts + 1."""
        self.status = IN_PROGRESS
        self.attempts += 1
        self.started_at = time.time()

    def requeue(self):
        """Retry after a RetryableError: back to status 1, waiting for the next attempt."""
        self.status = TODO
        self.started_at = None

    def time_left(self) -> float:
        """Seconds left in the request budget (never below 0). Also the timeout for the next attempt."""
        return max(0.0, REQUEST_BUDGET_SECONDS - (time.time() - self.created_at))


class Queue:
    """Registry of unfinished jobs, keyed by job_id (dicts keep arrival order).
    Created with a name, e.g. Queue("active"), so logs and errors say which one.
    The request handler adds a job on arrival and removes it in `finally`, so rows cannot leak.
    """
    def __init__(self, name: str):
        self.name = name
        self.jobs: dict[str, Job] = {}

    def add(self, job: Job):
        self.jobs[job.job_id] = job

    def get(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise JobNotFound(f"Job {job_id} is not in queue '{self.name}'", job_id=job_id) from None

    def take(self, job_id: str) -> Job:
        """Remove and return the job. Raises JobNotFound if it is not there."""
        job = self.get(job_id)
        del self.jobs[job_id]
        return job

    def discard(self, job_id: str):
        """Remove the job if present, never raises. For `finally` blocks."""
        self.jobs.pop(job_id, None)

    def __len__(self):
        return len(self.jobs)

    def counts(self) -> dict[int, int]:
        """{status: how many jobs}, for /health and the queue-size gauge."""
        counts = {TODO: 0, IN_PROGRESS: 0}
        for job in self.jobs.values():
            counts[job.status] += 1
        return counts


##############################################################
# Job execution
##############################################################
JOB_RSS_LIMIT_MB = int(os.environ.get("JOB_RSS_LIMIT_MB", "400"))  # sizing: N × limit + ~150MB parent ≤ container limit
RSS_POLL_SECONDS = 2


def _fail(error: type[JobError], message: str, job: Job) -> JobError:
    """Build the error with the job's id, attempt and script name, ready to raise."""
    return error(message, job_id=job.job_id, attempt=job.attempts, script_name=job.script)


def _prepare_payload(job_id: str, script_name: str, code: str, data: dict) -> str:
    """Serialize payload for wrapper stdin. script_name labels tracebacks."""
    return json.dumps({
        "job_id": job_id,
        "script_name": script_name,
        "code": code,
        "data": data
    })


def _read_rss_mb(pid: int) -> float:
    """Resident memory of a process in MB, from /proc/<pid>/status. 0 once the process is gone."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # value is in kB
    except OSError:
        pass  # process already gone
    return 0.0


def _kill_process_group(proc: asyncio.subprocess.Process):
    """Kill the wrapper and anything it started (it runs in its own session, so pgid == pid)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # already gone


def _log_subprocess(job: Job, proc: asyncio.subprocess.Process, seconds: float, peak_rss_mb: float, killed_for: str | None):
    """One line + one count per subprocess, also for failed ones: what the 48h watch (stack watch.py) reads.
    exit 0 = answer sent, 1 = the script failed (wrapper.py), negative = killed by that signal."""
    exit_code = proc.returncode if proc.returncode is not None else "none"
    SUBPROCESSES.labels(script_name=job.script, exit_code=str(exit_code)).inc()
    logger.info(f"subprocess job={job.job_id} attempt={job.attempts} script={job.script} pid={proc.pid} "
                f"exit={exit_code} killed_for={killed_for or '-'} seconds={seconds:.2f} peak_rss_mb={peak_rss_mb:.1f}")


async def _run_wrapper(
    job: Job,
    python_exe: str,
    scripts_dir: Path,
    payload: str
) -> subprocess.CompletedProcess:
    """Execute wrapper.py in a subprocess with the given payload, as a watchdog:
    kills the process group when the budget runs out (JobTimeout), RSS goes over
    JOB_RSS_LIMIT_MB (MemoryCapExceeded) or the request is cancelled (client disconnected).
    """
    proc = await asyncio.create_subprocess_exec(
        python_exe, str(WRAPPER_PATH),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env={
            **os.environ,
            "PYTHONPATH": str(scripts_dir),
            "SCRIPT_NAME": job.script,
            "PYTHONUNBUFFERED": "1"
        }
    )
    communicate = asyncio.create_task(proc.communicate(payload.encode()))
    started, peak_rss_mb, killed_for = time.time(), 0.0, None
    try:
        while not communicate.done():
            if job.time_left() <= 0:
                killed_for = "timeout"
                raise _fail(JobTimeout, f"Killed after the {REQUEST_BUDGET_SECONDS}s request budget", job)
            rss_mb = _read_rss_mb(proc.pid)
            peak_rss_mb = max(peak_rss_mb, rss_mb)
            if rss_mb > JOB_RSS_LIMIT_MB:
                killed_for = "memory_cap"
                raise _fail(MemoryCapExceeded, f"Killed at {rss_mb:.0f}MB RSS (limit {JOB_RSS_LIMIT_MB}MB)", job)
            await asyncio.wait({communicate}, timeout=min(RSS_POLL_SECONDS, job.time_left()))
        stdout, stderr = communicate.result()
    finally:
        if not communicate.done():  # timeout, memory cap or cancelled request
            killed_for = killed_for or "cancelled"
            _kill_process_group(proc)
            await asyncio.gather(communicate, return_exceptions=True)  # reaps the process
        _log_subprocess(job, proc, time.time() - started, peak_rss_mb, killed_for)

    return subprocess.CompletedProcess(
        args=[python_exe, str(WRAPPER_PATH)],
        returncode=proc.returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace")
    )


def _detect_venv_corruption(error_str: str) -> bool:
    """Check if error indicates venv corruption."""
    corruption_indicators = [
        "No module named",
        "ImportError",
        "ModuleNotFoundError"
    ]
    return any(x in error_str for x in corruption_indicators)


async def run_attempt(job: Job, package_manager: PackageManager) -> Any:
    """
    One attempt of a job: venv, script code, wrapper subprocess. The caller runs job.start()
    before it and decides what a RetryableError means (retry loop).
    Returns the script's `result`. Everything else is raised (exceptions.py):
    - ScriptError: the script raised, set no `result`, or cannot be read
    - JobTimeout / MemoryCapExceeded: killed by the watchdog in _run_wrapper
    - WorkerCrashed: the wrapper died without an answer, or could not start
    - RetryableError: the venv looks corrupted (missing module); the next attempt rebuilds it
    - VenvCreationException / JobTimeout / ScriptError from PackageManager.prepare_environment
    """
    start_time = time.time()
    python_exe, scripts_dir, venv_path = await asyncio.to_thread(package_manager.prepare_environment, job)

    script_path = scripts_dir / job.script
    try:
        code = await asyncio.to_thread(script_path.read_text)
    except OSError as e:
        raise _fail(ScriptError, f"Cannot read script {script_path}: {e}", job) from None

    payload = _prepare_payload(job.job_id, job.script, code, job.data)
    try:
        result = await _run_wrapper(job, python_exe, scripts_dir, payload)
    except OSError as e:
        package_manager.mark_venv_corrupted(venv_path)  # no-op for system Python
        raise _fail(WorkerCrashed, f"Wrapper could not start with {python_exe}: {e}", job) from None

    if result.returncode != 0 and "__RESULT_END__" not in result.stdout:
        raise _fail(WorkerCrashed, f"Wrapper exited {result.returncode} without an answer: {result.stderr[-2000:]}", job)

    output = _parse_wrapper_output(result, job.job_id, time.time() - start_time)
    if output["status"] == "success":
        return output["result"]

    error = str(output.get("error"))
    if venv_path and _detect_venv_corruption(error):
        package_manager.mark_venv_corrupted(venv_path)
        raise _fail(RetryableError, f"Venv looks corrupted, rebuilding it: {error}", job)
    if output.get("traceback"):
        logger.warning(f"job {job.job_id} {job.script} traceback:\n{output['traceback']}")
    raise _fail(ScriptError, error, job)


##############################################################
# Output parsing
##############################################################
def _parse_wrapper_output(result: subprocess.CompletedProcess, job_id: str, process_time: float) -> dict[str, Any]:
    """Parse wrapper.py output with markers.

    Markers first, whatever the exit code: wrapper.py exits 1 on a script error but
    still prints the error JSON. stderr is only the fallback when the wrapper itself
    crashed (no markers), e.g. interpreter or import failure.
    """
    stdout = result.stdout
    start_marker = "__RESULT_START__\n"
    end_marker = "\n__RESULT_END__"

    try:
        if start_marker in stdout and end_marker in stdout:
            return _extract_json_output(stdout, start_marker, end_marker, job_id, process_time)
    except json.JSONDecodeError as e:
        return _format_json_error_output(e, stdout, job_id, process_time)

    if result.returncode != 0:
        return {
            "job_id": job_id,
            "status": "error",
            "error": result.stderr[-2000:] if result.stderr else f"wrapper exited {result.returncode} without output",
            "result": None,
            "process_time": process_time,
            "venv_corrupted": False
        }
    return _format_missing_markers_output(stdout, job_id, process_time)


def _extract_json_output(
    stdout: str,
    start_marker: str,
    end_marker: str,
    job_id: str,
    process_time: float
) -> dict[str, Any]:
    """Extract and parse JSON from between markers."""
    json_part = stdout.split(start_marker)[1].split(end_marker)[0]
    parsed = json.loads(json_part)
    output = {
        "job_id": job_id,
        "status": parsed.get("status", "success"),
        "result": parsed.get("result"),
        "error": parsed.get("error"),
        "process_time": process_time,
        "venv_corrupted": False
    }
    if output["status"] == "error":                     # script frames only, see wrapper._describe
        output["error_type"] = parsed.get("error_type")
        output["traceback"] = (parsed.get("traceback") or "")[-4000:] or None
    return output


def _format_missing_markers_output(stdout: str, job_id: str, process_time: float) -> dict[str, Any]:
    """Format error when markers not found."""
    return {
        "job_id": job_id,
        "status": "error",
        "error": f"Invalid output format: {stdout[:500]}",
        "result": None,
        "process_time": process_time,
        "venv_corrupted": False
    }


def _format_json_error_output(e: json.JSONDecodeError, stdout: str, job_id: str, process_time: float) -> dict[str, Any]:
    """Format error when JSON parsing fails."""
    return {
        "job_id": job_id,
        "status": "error",
        "error": f"JSON parse error: {e}",
        "result": stdout[-1000:],
        "process_time": process_time,
        "venv_corrupted": False
    }


##############################################################
# Concurrency cap
##############################################################
def available_cpus() -> float:
    """CPUs this container may use: the cgroup v2 quota (compose `cpus:`) if set, else the CPUs it can run on."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass  # no cgroup v2 file (e.g. local run outside a container)
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1  # macOS has no sched_getaffinity


def max_concurrent_jobs() -> int:
    """N = floor(0.7 × available CPUs), minimum 1. MAX_CONCURRENT_JOBS overrides it."""
    override = os.environ.get("MAX_CONCURRENT_JOBS")
    if override:
        return max(1, int(override))
    return max(1, math.floor(0.7 * available_cpus()))


##############################################################
# Runner: one request from n8n = one Job = sequential attempts in one slot
##############################################################
RETRY_PAUSE_SECONDS = 1.5   # pause between attempts, counted in the request budget


class Runner:
    """Runs n8n requests as Jobs: one wrapper subprocess per attempt, at most N attempts running at once.

    Flow of run():
        add to `active` -> wait for a slot -> attempt (job.start(), run_attempt)
        success                                -> return the script's result
        RetryableError, attempts and time left -> log, pause, job.requeue(), next attempt
        RetryableError, nothing left           -> RetriesExhausted (carries the last error)
        FatalError                             -> raised as is
        finally: the row leaves `active`; a cancelled request (client gone) kills the subprocess
    Every error raised is a FatalError, so the caller never sees status "retrying".
    """
    def __init__(self, package_manager: PackageManager, max_concurrent: int,
                 on_retry: Callable[[Job, RetryableError], None] | None = None):
        self.package_manager = package_manager
        self.max_concurrent = max_concurrent
        self.active = Queue("active")
        self._slots = asyncio.Semaphore(max_concurrent)
        self._on_retry = on_retry   # e.g. the JOB_RETRIES metric, kept in main.py

    async def run(self, script: str, data: dict) -> Any:
        job = Job(script=script, data=data)
        self.active.add(job)
        try:
            try:
                await asyncio.wait_for(self._slots.acquire(), timeout=job.time_left())
            except TimeoutError:
                raise _fail(RetriesExhausted, f"No free slot within the {REQUEST_BUDGET_SECONDS}s request budget "
                                              f"({self.max_concurrent} jobs running)", job) from None
            try:
                return await self._attempts(job)
            finally:
                self._slots.release()
        finally:
            self.active.discard(job.job_id)

    async def _attempts(self, job: Job) -> Any:
        while True:
            job.start()
            try:
                return await run_attempt(job, self.package_manager)
            except RetryableError as e:
                logger.warning(f"job {job.job_id} {job.script} attempt {job.attempts}/{MAX_ATTEMPTS} failed: "
                               f"{type(e).__name__}: {e.message}")
                if job.attempts >= MAX_ATTEMPTS or job.time_left() <= RETRY_PAUSE_SECONDS:
                    raise RetriesExhausted(
                        f"Gave up after {job.attempts} attempt(s), last error {type(e).__name__}: {e.message}",
                        job_id=job.job_id, attempt=job.attempts, script_name=job.script, last_error=e) from e
                if self._on_retry:
                    self._on_retry(job, e)
                job.requeue()
                await asyncio.sleep(RETRY_PAUSE_SECONDS)
