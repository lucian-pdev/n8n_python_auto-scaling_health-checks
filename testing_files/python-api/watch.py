#!/usr/bin/env python3
"""
Long watch (e.g. 48 h) of python-api on a stack deployed by stack_test.py. One subcommand per Jenkins stage:

    stage-gui py testing_files/python-api/watch.py setup        # watch script + webhook workflow into the stack
    stage-gui py testing_files/python-api/watch.py tick <n>     # one hour: request + metrics + logs -> CSV, report
    stage-gui py testing_files/python-api/watch.py report       # REPORT.md from the CSV files (every tick does it too)

Each tick:
  - sends one request to n8n (webhook pyapi-watch, random data) -> n8n -> python-api (pyapi_watch.py, in a venv)
  - pulls the stack's Prometheus since the last tick, 60 s steps: VM (node-exporter), python-api container
    (its own cgroup numbers), everything python-api exposes (py_api_*, process_*, python_gc_*)
  - reads python-api's "subprocess job=..." log lines (one per spawned subprocess, with exit code) and the container state
Files in $WATCH_DIR (default ./watch): requests.csv, metrics.csv (long: time, metric, labels, value),
subprocesses.csv, container.csv, state.json, REPORT.md.
"""

import csv
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from invoke.exceptions import CommandTimedOut
from linuxvm import LinuxVMError
from stack_test import REMOTE_DIR, StepFailed, vm

WATCH = Path(os.environ.get("WATCH_DIR", "watch"))
STEP_SECONDS = 60
WEBHOOK = "/webhook/pyapi-watch"

# name -> PromQL. VM = node-exporter. Container = python-api's own cgroup numbers (py_api_container_*, metrics.py):
# cAdvisor v0.47 registers no Docker containers on this host (cgroup v2), so its per-container series are empty
QUERIES = {
    "vm_load1": "node_load1",
    "vm_load5": "node_load5",
    "vm_load15": "node_load15",
    "vm_cpu_used_pct": '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[2m])))',
    "vm_cpu_iowait_pct": '100 * avg(rate(node_cpu_seconds_total{mode="iowait"}[2m]))',
    "vm_mem_used_bytes": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",
    "vm_mem_available_bytes": "node_memory_MemAvailable_bytes",
    "vm_mem_cached_bytes": "node_memory_Cached_bytes",
    "vm_swap_used_bytes": "node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes",
    "vm_disk_used_pct": '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
    "vm_net_rx_bytes_per_s": 'sum(rate(node_network_receive_bytes_total{device!~"lo|veth.*|br-.*|docker.*"}[2m]))',
    "vm_net_tx_bytes_per_s": 'sum(rate(node_network_transmit_bytes_total{device!~"lo|veth.*|br-.*|docker.*"}[2m]))',
    "pyapi_container_cpu_pct": '100 * rate(py_api_container_cpu_seconds[2m])',
    "pyapi_container_mem_bytes": 'py_api_container_memory_bytes',
    "pyapi_container_mem_anon_bytes": 'py_api_container_memory_anon_bytes',
    "pyapi_container_oom_kills": 'py_api_container_oom_kills',
    # everything python-api exposes, raw (histogram buckets left out: _sum/_count are enough)
    "pyapi_exposed": '{job="python-api", __name__=~"py_api_.*|process_.*|python_gc_.*", __name__!~".*_bucket"}',
}
SUBPROCESS_LINE = re.compile(
    r"^(?P<time>\S+) .*subprocess job=(?P<job>\S+) attempt=(?P<attempt>\d+) script=(?P<script>\S+) pid=(?P<pid>\d+) "
    r"exit=(?P<exit>\S+) killed_for=(?P<killed_for>\S+) seconds=(?P<seconds>[\d.]+) peak_rss_mb=(?P<peak_rss_mb>[\d.]+)")
FILES = {
    "requests.csv": ["tick", "time_utc", "rows", "groups", "seed", "http_code", "seconds", "status", "python", "error"],
    "metrics.csv": ["time_utc", "metric", "labels", "value"],
    "subprocesses.csv": ["time_utc", "job_id", "attempt", "script", "pid", "exit_code", "killed_for", "seconds", "peak_rss_mb"],
    "container.csv": ["tick", "time_utc", "status", "started_at", "restart_count", "oom_killed", "container_id"],
}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def vm_ip() -> str:
    return vm().ip


def http(url: str, body: bytes | None = None, timeout: float = 60) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def ready(url: str) -> bool:
    """200 from url; refused/timeouts (service restarting) count as not yet."""
    try:
        return http(url, timeout=10)[0] == 200
    except OSError:
        return False


def append(name: str, rows: list[list]):
    path = WATCH / name
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(FILES[name])
        w.writerows(rows)


def read(name: str) -> list[dict]:
    path = WATCH / name
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def state() -> dict:
    path = WATCH / "state.json"
    if not path.exists():
        raise StepFailed(f"{path} missing: run 'setup' first")
    return json.loads(path.read_text())


##############################################################
# Steps
##############################################################
def step_setup():
    v = vm()
    v.sudo("install", "-o", "1000", "-g", "1000", "-m", "644",
           f"{REMOTE_DIR}/testing_files/python-api/scripts/pyapi_watch.py", f"{REMOTE_DIR}/scripts/pyapi_watch.py")
    v.sudo("docker", "cp", f"{REMOTE_DIR}/testing_files/python-api/watch.workflow.json", "n8n-main:/tmp/watch.json")
    print(v.sudo("docker", "exec", "n8n-main", "n8n", "import:workflow", "--input=/tmp/watch.json"))
    print(v.sudo("docker", "exec", "n8n-main", "n8n", "publish:workflow", "--id=PyApiWatch000001", check=False))
    # CLI activation is picked up on start
    v.conn.run(f"cd ~/{REMOTE_DIR} && sudo docker compose restart n8n-main n8n-worker", hide=True, timeout=300)
    ip = v.ip
    deadline = time.time() + 300
    while not ready(f"http://{ip}:5678/healthz/readiness"):
        if time.time() > deadline:
            raise StepFailed("n8n not ready 300 s after the restart")
        time.sleep(5)
    WATCH.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (WATCH / "state.json").write_text(json.dumps({"start": now, "last": now, "ip": ip}))
    print(f"watch starts {iso(now)}, files in {WATCH}")


def send_request(ip: str, tick: int) -> list:
    seed = random.randrange(2**32)
    rnd = random.Random(seed)
    groups = [f"g{n:02d}" for n in range(rnd.randint(1, 20))]
    rows = [{"name": f"row{n}", "value": round(rnd.uniform(-1000, 1000), 3), "group": rnd.choice(groups)}
            for n in range(rnd.randint(10, 5000))]
    body = json.dumps({"tick": tick, "rows": rows}).encode()
    start = time.time()
    try:
        code, raw = http(f"http://{ip}:5678{WEBHOOK}", body, timeout=400)
    except OSError as e:
        return [tick, iso(start), len(rows), len(groups), seed, 0, round(time.time() - start, 2), "no answer", "", str(e)]
    took = round(time.time() - start, 2)
    try:
        answer = json.loads(raw)
        answer = answer[0] if isinstance(answer, list) else answer
        result = answer.get("result") or {}
    except (ValueError, AttributeError, IndexError):
        result = {"status": "unreadable", "error": raw[:300].decode(errors="replace")}
    python = (result.get("result") or {}).get("python", "") if isinstance(result.get("result"), dict) else ""
    return [tick, iso(start), len(rows), len(groups), seed, code, took, result.get("status", ""), python, result.get("error", "")]


def pull_metrics(ip: str, since: float, until: float) -> list[list]:
    rows = []
    for name, query in QUERIES.items():
        params = urllib.parse.urlencode({"query": query, "start": since, "end": until, "step": STEP_SECONDS})
        code, raw = http(f"http://{ip}:9090/api/v1/query_range?{params}")
        if code != 200:
            raise StepFailed(f"Prometheus {name}: HTTP {code} {raw[:200]!r}")
        for series in json.loads(raw)["data"]["result"]:
            labels = dict(series["metric"])
            metric = labels.pop("__name__", name) if name == "pyapi_exposed" else name
            for drop in ("job", "instance"):
                labels.pop(drop, None)
            label_text = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
            rows += [[iso(float(ts)), metric, label_text, value] for ts, value in series["values"]]
    return rows


def pull_subprocesses(since: float) -> list[list]:
    """New subprocess lines since the last tick. Reads 10 min more and drops lines already in the CSV (by job id,
    attempt, pid), so a clock difference between Jenkins and the VM can't lose or double a line."""
    out = vm().sudo("docker", "logs", "--timestamps", "--since", iso(since - 600), "python-api")
    seen = {(s["job_id"], s["attempt"], s["pid"]) for s in read("subprocesses.csv")}
    rows = []
    for line in out.splitlines():
        m = SUBPROCESS_LINE.match(line)
        if m and (m["job"], m["attempt"], m["pid"]) not in seen:
            seen.add((m["job"], m["attempt"], m["pid"]))
            rows.append([m["time"][:19] + "Z", m["job"], m["attempt"], m["script"], m["pid"], m["exit"],
                         m["killed_for"], m["seconds"], m["peak_rss_mb"]])
    return rows


def container_state(tick: int) -> list:
    info = json.loads(vm().sudo("docker", "inspect", "python-api"))[0]
    s = info["State"]
    return [tick, iso(time.time()), s["Status"], s["StartedAt"][:19] + "Z", info["RestartCount"], s["OOMKilled"], info["Id"]]


def step_tick(tick: int):
    st = state()
    since, now = st["last"], time.time()
    request = send_request(st["ip"], tick)
    append("requests.csv", [request])
    print(f"request: HTTP {request[5]} {request[7]} in {request[6]}s ({request[2]} rows) {request[9]}")
    until = time.time()
    cont = container_state(tick)
    append("container.csv", [cont[:-1] + [cont[-1][:12]]])
    metrics = pull_metrics(st["ip"], since, until)
    append("metrics.csv", metrics)
    subs = pull_subprocesses(since)
    append("subprocesses.csv", subs)
    st["last"] = until
    (WATCH / "state.json").write_text(json.dumps(st))
    bad = [s for s in subs if s[5] != "0"]
    if not any(m[1] == "pyapi_container_mem_bytes" for m in metrics):
        print("WARNING: no py_api_container_* samples: container rows stay empty")
    print(f"metrics: {len(metrics)} samples; subprocesses: {len(subs)} ({len(bad)} exit != 0); "
          f"container {cont[2]}, restarts {cont[4]}, OOMKilled {cont[5]}")
    step_report()
    if request[7] != "success":
        raise StepFailed(f"tick {tick}: n8n -> python-api answered {request[5]} {request[7]}: {request[9]}")


##############################################################
# Report
##############################################################
def series(metrics: list[dict], name: str) -> dict[str, list[tuple[str, float]]]:
    """{labels: [(time, value)...]} for one metric."""
    out: dict[str, list[tuple[str, float]]] = {}
    for m in metrics:
        if m["metric"] == name:
            out.setdefault(m["labels"], []).append((m["time_utc"], float(m["value"])))
    return out


def increase(points: list[tuple[str, float]]) -> float:
    """Counter growth over the points; a drop is a restart (the counter starts again from 0)."""
    total = 0.0
    for (_, a), (_, b) in zip(points, points[1:]):
        total += b - a if b >= a else b
    return total


def stats(values: list[float]) -> str:
    if not values:
        return "| - | - | - | - |"
    p95 = sorted(values)[int(0.95 * (len(values) - 1))]
    return f"| {min(values):,.2f} | {statistics.fmean(values):,.2f} | {p95:,.2f} | {max(values):,.2f} |"


def mb(values: list[float]) -> list[float]:
    return [v / 1024 / 1024 for v in values]


def step_report():
    st = state()
    requests, metrics = read("requests.csv"), read("metrics.csv")
    subs, cont = read("subprocesses.csv"), read("container.csv")
    lines = [f"# python-api watch: {iso(st['start'])} -> {iso(st['last'])} "
             f"({(st['last'] - st['start']) / 3600:.1f} h, {len(requests)} ticks)", ""]

    ok = [r for r in requests if r["status"] == "success"]
    lines += ["## Hourly n8n requests (webhook -> python-api, random data)", "",
              f"{len(ok)} of {len(requests)} answered success; in a venv: {sum('/venvs/' in r['python'] for r in ok)}.", ""]
    failed = [r for r in requests if r["status"] != "success"]
    if failed:
        lines += ["| tick | time | HTTP | status | error |", "|---|---|---|---|---|"]
        lines += [f"| {r['tick']} | {r['time_utc']} | {r['http_code']} | {r['status']} | {r['error'][:150]} |" for r in failed]
        lines.append("")

    nonzero = [s for s in subs if s["exit_code"] != "0"]
    by_code: dict[str, int] = {}
    for s in subs:
        by_code[s["exit_code"]] = by_code.get(s["exit_code"], 0) + 1
    lines += ["## Subprocesses (from python-api's log, one line each)", "",
              f"Spawned {len(subs)}, exit 0 {by_code.get('0', 0)}, other exit codes {len(nonzero)}. "
              "Exit 1 = the script failed (wrapper.py), negative = killed by that signal (-9: watchdog).", "",
              "| exit code | count |", "|---|---|"] + [f"| {c} | {n} |" for c, n in sorted(by_code.items())] + [""]
    if subs:
        lines += ["| per subprocess | min | avg | p95 | max |", "|---|---|---|---|---|",
                  f"| seconds {stats([float(s['seconds']) for s in subs])}",
                  f"| peak RSS MB {stats([float(s['peak_rss_mb']) for s in subs])}", ""]
    if nonzero:
        lines += ["| time | job_id | attempt | script | pid | exit | killed for |", "|---|---|---|---|---|---|---|"]
        lines += [f"| {s['time_utc']} | {s['job_id']} | {s['attempt']} | {s['script']} | {s['pid']} | {s['exit_code']} "
                  f"| {s['killed_for']} |" for s in nonzero[:200]]
        lines.append("")

    def table(title: str, rows: list[tuple[str, list[float]]]):
        lines.extend([f"## {title}", "", "| metric | min | avg | p95 | max |", "|---|---|---|---|---|"])
        lines.extend(f"| {name} {stats(values)}" for name, values in rows)
        lines.append("")

    def values(name: str) -> list[float]:
        return [v for pts in series(metrics, name).values() for _, v in pts]

    table("VM (node-exporter, 60 s samples)", [
        ("load 1m", values("vm_load1")), ("load 5m", values("vm_load5")), ("load 15m", values("vm_load15")),
        ("CPU used %", values("vm_cpu_used_pct")), ("CPU iowait %", values("vm_cpu_iowait_pct")),
        ("memory used MB", mb(values("vm_mem_used_bytes"))), ("memory available MB", mb(values("vm_mem_available_bytes"))),
        ("page cache MB", mb(values("vm_mem_cached_bytes"))), ("swap used MB", mb(values("vm_swap_used_bytes"))),
        ("disk / used %", values("vm_disk_used_pct")),
        ("net rx KB/s", [v / 1024 for v in values("vm_net_rx_bytes_per_s")]),
        ("net tx KB/s", [v / 1024 for v in values("vm_net_tx_bytes_per_s")]),
    ])
    table("python-api container (its cgroup: parent + job subprocesses)", [
        ("CPU % (100 = one core)", values("pyapi_container_cpu_pct")),
        ("memory anon MB (heaps, no cache)", mb(values("pyapi_container_mem_anon_bytes"))),
        ("memory in use MB (incl. page cache)", mb(values("pyapi_container_mem_bytes"))),
    ])
    table("python-api parent process (its own /metrics)", [
        ("resident memory MB (process_resident_memory_bytes)", mb(values("process_resident_memory_bytes"))),
        ("virtual memory MB", mb(values("process_virtual_memory_bytes"))),
        ("open fds", values("process_open_fds")),
        ("jobs running (py_api_active_jobs)", values("py_api_active_jobs")),
        ("jobs waiting (py_api_queue_size)", values("py_api_queue_size")),
    ])

    # memory trend: first vs last hour averages; a leak shows as steady growth
    lines += ["## Memory trend (first hour vs last hour, avg MB)", "", "| series | first hour | last hour | change |", "|---|---|---|---|"]
    for title, name in [("container anon memory", "pyapi_container_mem_anon_bytes"),
                        ("parent resident memory", "process_resident_memory_bytes"),
                        ("VM memory used", "vm_mem_used_bytes")]:
        pts = sorted(p for s in series(metrics, name).values() for p in s)
        if len(pts) >= 120:
            first, last = mb([v for _, v in pts[:60]]), mb([v for _, v in pts[-60:]])
            a, b = statistics.fmean(first), statistics.fmean(last)
            lines.append(f"| {title} | {a:,.1f} | {b:,.1f} | {b - a:+,.1f} |")
        else:
            lines.append(f"| {title} | - | - | needs 2 h of data |")
    lines.append("")

    lines += ["## python-api counters (growth over the window, restarts counted)", "", "| metric | labels | increase |", "|---|---|---|"]
    counters = sorted({m["metric"] for m in metrics if m["metric"].endswith(("_total", "_count", "_sum"))
                       and (m["metric"].startswith(("py_api_", "process_cpu", "python_gc_")))})
    for name in counters:
        for labels, pts in sorted(series(metrics, name).items()):
            lines.append(f"| {name} | {labels or '-'} | {increase(pts):,.2f} |")
    lines.append("")

    if cont:
        restarts = {c["started_at"] for c in cont}
        lines += ["## python-api container state", "",
                  f"Last tick: {cont[-1]['status']}, RestartCount {cont[-1]['restart_count']}, "
                  f"OOMKilled {cont[-1]['oom_killed']}, start times seen: {len(restarts)} ({', '.join(sorted(restarts))}). "
                  f"kernel OOM kills in the container: {max(values('pyapi_container_oom_kills') or [0]):.0f}.", ""]

    # one row per tick: the shape of the 48 h at a glance
    lines += ["## Per tick", "", "| tick | request | subprocesses (exit != 0) | container anon MB avg | parent MB avg | VM CPU % avg |",
              "|---|---|---|---|---|---|"]
    ticks = [(r["tick"], r["time_utc"], r["status"]) for r in requests]
    bounds = [t for _, t, _ in ticks] + [iso(st["last"])]
    for i, (tick, start, status) in enumerate(ticks):
        lo, hi = start, bounds[i + 1]   # from this tick's request to the next tick
        in_tick = [s for s in subs if lo <= s["time_utc"] < hi]

        def avg(name: str, scale: float = 1.0) -> str:
            vals = [v for pts in series(metrics, name).values() for t, v in pts if lo <= t < hi]
            return f"{statistics.fmean(vals) / scale:,.1f}" if vals else "-"
        lines.append(f"| {tick} | {status} | {len(in_tick)} ({sum(s['exit_code'] != '0' for s in in_tick)}) "
                     f"| {avg('pyapi_container_mem_anon_bytes', 1024 * 1024)} "
                     f"| {avg('process_resident_memory_bytes', 1024 * 1024)} | {avg('vm_cpu_used_pct')} |")
    (WATCH / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"report: {WATCH / 'REPORT.md'}")


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if name == "setup":
            step_setup()
        elif name == "tick" and len(sys.argv) == 3:
            step_tick(int(sys.argv[2]))
        elif name == "report":
            step_report()
        else:
            print(__doc__)
            return 2
    except (StepFailed, LinuxVMError, CommandTimedOut, OSError, ValueError, KeyError) as e:
        print(f"step {name} failed: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
