#!/usr/bin/env python3
"""
python-api checks against a running container (stdlib only, runs anywhere).

    python3 test_api.py --url http://localhost:8000 <step> [<step> ...]   # or: all

The container must see the scripts in testing_files/python-api/scripts/ (mounted or copied into its
scripts dir) and run with test limits, so the slow cases stay short:
    REQUEST_BUDGET_SECONDS=20  JOB_RSS_LIMIT_MB=200  MAX_CONCURRENT_JOBS=2
Each step prints what it checked; a failure prints "step <name> failed: <reason>" and exits 1.
"""

import argparse
import http.client
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


class StepFailed(Exception):
    """A check did not hold."""


class Api:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.url + path, timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def health(self) -> dict:
        status, body = self.get("/health")
        if status != 200:
            raise StepFailed(f"/health answered {status}: {body[:200]}")
        return json.loads(body)

    def metric(self, name: str) -> float:
        """Sum of all samples of a counter/gauge in /metrics (0 if absent)."""
        _, body = self.get("/metrics")
        return sum(float(m) for m in re.findall(rf"^{name}(?:{{[^}}]*}})? (\S+)$", body, re.MULTILINE))

    def execute(self, script: str, data: dict | None = None, timeout: float = 400) -> tuple[int, dict]:
        body = json.dumps({"code_file_name": script, "data": data or {}}).encode()
        req = urllib.request.Request(self.url + "/execute", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


def expect(condition: bool, message: str):
    if not condition:
        raise StepFailed(message)


def expect_idle(api: Api, wait: float = 10):
    """No job rows left (the registry must be empty after every request)."""
    deadline = time.time() + wait
    while True:
        jobs = api.health()["jobs"]
        if jobs == {"todo": 0, "in_progress": 0}:
            return
        if time.time() > deadline:
            raise StepFailed(f"jobs left in the registry: {jobs}")
        time.sleep(0.5)


def expect_fatal(response: dict, error_type: str, attempts: int, last_error: str | None = None):
    result = response.get("result") or {}
    expect(result.get("status") == "fatal", f"status {result.get('status')!r}, expected 'fatal': {result}")
    expect(result.get("error_type") == error_type, f"error_type {result.get('error_type')!r}, expected {error_type!r}")
    expect(result.get("attempt") == attempts, f"attempt {result.get('attempt')}, expected {attempts}")
    if last_error:
        got = (result.get("last_error") or {}).get("type")
        expect(got == last_error, f"last_error {got!r}, expected {last_error!r}")
    print(f"  {error_type} after {attempts} attempt(s): {result.get('error')}")


##############################################################
# Steps
##############################################################
def step_health(api: Api):
    health = api.health()
    print(f"  {health}")
    expect(health["status"] == "ok", f"status {health['status']}")
    expect_idle(api)


def step_ok(api: Api):
    code, response = api.execute("pyapi_ok.py", {"n": 1})
    result = response.get("result") or {}
    expect(code == 200 and result.get("status") == "success", f"HTTP {code}: {response}")
    expect(result["result"]["echo"] == {"n": 1}, f"wrong echo: {result['result']}")
    print(f"  success on {result['result']['python']} in {result['process_time']:.2f}s")
    expect_idle(api)


def step_missing(api: Api):
    code, response = api.execute("does_not_exist.py")
    expect(code == 404, f"HTTP {code}, expected 404: {response}")
    print("  404 for a missing script")


def step_venv(api: Api):
    created_before = api.metric("py_api_venv_creations_total")
    hits_before = api.metric("py_api_venv_cache_hits_total")
    for run in ("build", "reuse"):
        code, response = api.execute("pyapi_venv.py", {"rows": [["x", 1]]})
        result = response.get("result") or {}
        expect(code == 200 and result.get("status") == "success", f"{run}: HTTP {code}: {response}")
        prefix = result["result"]["python"]
        expect("/venvs/" in prefix, f"{run}: ran on {prefix}, not in a venv")
        print(f"  {run}: success in venv {prefix} ({result['process_time']:.1f}s)")
    created = api.metric("py_api_venv_creations_total") - created_before
    hits = api.metric("py_api_venv_cache_hits_total") - hits_before
    expect(hits >= 1, f"second run was no cache hit (creations +{created}, hits +{hits})")
    print(f"  venv creations +{created:.0f}, cache hits +{hits:.0f}")
    expect_idle(api)


def step_raise(api: Api):
    code, response = api.execute("pyapi_raise.py", {"message": "boom"})
    expect(code == 200, f"HTTP {code}: {response}")
    expect_fatal(response, "ScriptError", 1)
    expect(response["result"]["error"] == "boom", f"error {response['result']['error']!r}, expected 'boom'")
    expect_idle(api)


def step_crash(api: Api):
    retries_before = api.metric("py_api_job_retries_total")
    _, response = api.execute("pyapi_crash.py")
    expect_fatal(response, "RetriesExhausted", 3, "WorkerCrashed")
    retries = api.metric("py_api_job_retries_total") - retries_before
    expect(retries == 2, f"py_api_job_retries_total +{retries}, expected +2")
    expect_idle(api)


def step_alloc(api: Api):
    _, response = api.execute("pyapi_alloc.py", {"mb": 600, "hold": 10})
    expect_fatal(response, "RetriesExhausted", 3, "MemoryCapExceeded")
    expect_idle(api)


def step_timeout(api: Api):
    start = time.time()
    _, response = api.execute("pyapi_sleep.py", {"seconds": 400})
    took = time.time() - start
    expect_fatal(response, "RetriesExhausted", 1, "JobTimeout")
    print(f"  answered after {took:.0f}s")
    expect_idle(api)


def step_disconnect(api: Api):
    """Client hangs up mid-job: the job row and its subprocess must go."""
    u = urlparse(api.url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
    conn.request("POST", "/execute", json.dumps({"code_file_name": "pyapi_sleep.py", "data": {"seconds": 60}}),
                 {"Content-Type": "application/json"})
    time.sleep(3)
    expect(api.health()["jobs"]["in_progress"] == 1, "the sleep job is not running")
    conn.close()
    expect_idle(api, wait=10)
    print("  job dropped within 10s of the disconnect")


def step_concurrency(api: Api):
    """More jobs than slots: never more than max_concurrent in progress, the rest wait (todo)."""
    cap = api.health()["max_concurrent"]
    jobs = cap + 2
    results, peak = [], {"in_progress": 0, "todo": 0}
    threads = [threading.Thread(target=lambda: results.append(api.execute("pyapi_sleep.py", {"seconds": 3})))
               for _ in range(jobs)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        now = api.health()["jobs"]
        peak = {k: max(peak[k], now[k]) for k in peak}
        time.sleep(0.3)
    expect(peak["in_progress"] <= cap, f"{peak['in_progress']} jobs ran at once, cap is {cap}")
    expect(peak["todo"] >= 1, f"no job ever waited for a slot ({jobs} jobs, cap {cap})")
    failed = [r for r in results if (r[1].get("result") or {}).get("status") != "success"]
    expect(not failed, f"{len(failed)} of {jobs} jobs failed: {failed[:2]}")
    print(f"  {jobs} jobs, cap {cap}: peak {peak['in_progress']} running, {peak['todo']} waiting")
    expect_idle(api)


def step_soak(api: Api, count: int = 1000, parallel: int = 4):
    """Many small jobs: the parent's memory stays flat and the registry ends empty."""
    for _ in range(50):                                     # warm-up, so imports/caches are loaded
        api.execute("pyapi_ok.py")
    rss_before = api.health()["rss_mb"]
    failures, lock, next_job = [], threading.Lock(), iter(range(count))

    def worker():
        while True:
            with lock:
                if next(next_job, None) is None:
                    return
            _, response = api.execute("pyapi_ok.py", {"pad": "x" * 2000})
            if (response.get("result") or {}).get("status") != "success":
                failures.append(response)

    start = time.time()
    threads = [threading.Thread(target=worker) for _ in range(parallel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rss_after = api.health()["rss_mb"]
    print(f"  {count} jobs in {time.time() - start:.0f}s, parent RSS {rss_before} -> {rss_after} MB")
    expect(not failures, f"{len(failures)} jobs failed: {failures[:2]}")
    expect(rss_after - rss_before <= 10, f"parent RSS grew {rss_after - rss_before:.1f} MB (limit 10)")
    expect_idle(api)


STEPS = {
    "health": step_health,
    "ok": step_ok,
    "missing": step_missing,
    "venv": step_venv,
    "raise": step_raise,
    "crash": step_crash,
    "alloc": step_alloc,
    "timeout": step_timeout,
    "disconnect": step_disconnect,
    "concurrency": step_concurrency,
    "soak": step_soak,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("steps", nargs="+", choices=[*STEPS, "all"])
    args = parser.parse_args()
    api = Api(args.url)
    steps = list(STEPS) if "all" in args.steps else args.steps
    for name in steps:
        print(f"step {name}")
        try:
            STEPS[name](api)
        except (StepFailed, OSError, ValueError, KeyError) as e:
            print(f"step {name} failed: {type(e).__name__}: {e}")
            return 1
        print(f"step {name} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
