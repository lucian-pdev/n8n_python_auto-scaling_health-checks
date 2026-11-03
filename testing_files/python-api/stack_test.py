#!/usr/bin/env python3
"""
Full naypyers stack on a throwaway Proxmox VM, then python-api checks. One subcommand per Jenkins stage:

    stage-gui py testing_files/python-api/stack_test.py <step> [args]

    create            clone a Docker VM (Project_Testing lib/remote/linuxvm.py), state in $STACK_STATE
    ship              copy this checkout (git-tracked + untracked, not ignored) to ~/naypyers on the VM
    deploy            env-maker.sh + deploy.sh testing_files/env (the documented test deploy), wait for python-api
    scripts           stop github_sync, put testing_files/python-api/scripts/ into the scripts dir
    n8n-venv          import + run venv-test.workflow.json in n8n: python-api must build a venv and use it
    api <steps...>    test_api.py on the VM against python-api (see test_api.py for the steps)
    evidence          compose ps, python-api logs, /health, /metrics into $OUT (default ./evidence)
    destroy           remove the VM

Needs lib/remote on PYTHONPATH (stage-gui py does it). Failures: "step <name> failed: <reason>", exit 1.
"""

import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from invoke.exceptions import CommandTimedOut
from linuxvm import LinuxVM, LinuxVMError

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("STACK_STATE", ".stack-vm.json"))
VM_NAME = os.environ.get("STACK_VM_NAME", "naypyers-pyapi-test")
REMOTE_DIR = "naypyers"   # relative to the VM user's home
WORKFLOW_ID = "PyApiVenvTest01"


class StepFailed(Exception):
    """A step's check did not hold."""


def vm() -> LinuxVM:
    if not STATE.exists():
        raise StepFailed(f"no VM: {STATE} missing (run 'create' first)")
    state = json.loads(STATE.read_text())
    return LinuxVM(state["vmid"], state["target"])


def sh(v: LinuxVM, command: str, timeout: int = 600, check: bool = True) -> str:
    """One command line in the project dir on the VM (repo scripts only, no logic here), output streamed to the log."""
    r = v.conn.run(f"cd ~/{REMOTE_DIR} && {command}", warn=True, timeout=timeout, in_stream=False, pty=False)
    if check and r.exited != 0:
        raise StepFailed(f"'{command}' exited {r.exited}")
    return r.stdout


def wait_http(v: LinuxVM, url: str, timeout: int = 300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if v.run("curl", "-fsS", "-o", "/dev/null", url, check=False, timeout=15) == "":
            return
        time.sleep(5)
    raise StepFailed(f"{url} not answering after {timeout}s")


def metric(v: LinuxVM, name: str) -> float:
    body = v.run("curl", "-fsS", "http://localhost:8000/metrics")
    return sum(float(line.split()[-1]) for line in body.splitlines() if line.startswith(name))


##############################################################
# Steps
##############################################################
def step_create():
    for old in LinuxVM.find(VM_NAME):   # leftover of an earlier run
        print(f"removing leftover VM {old}")
        LinuxVM.destroy_id(old)
    v = LinuxVM.create(VM_NAME, cores=int(os.environ.get("STACK_CORES", "4")),
                       memory_mb=int(os.environ.get("STACK_MEMORY", "4096")))
    STATE.write_text(json.dumps({"vmid": v.vmid, "target": v.target}))
    print(f"VM {v.vmid} {v.target}")


def step_ship():
    v = vm()
    files = subprocess.run(["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            if (REPO / f).exists():
                tar.add(REPO / f, arcname=f)
    buf.seek(0)
    v.run("mkdir", "-p", REMOTE_DIR)
    v.conn.put(buf, "/tmp/naypyers.tgz")
    v.run("tar", "xzf", "/tmp/naypyers.tgz", "-C", REMOTE_DIR)
    v.run("rm", "/tmp/naypyers.tgz")
    print(f"{len(files)} files -> {v.target}:~/{REMOTE_DIR}")


def step_deploy():
    v = vm()
    sh(v, "./env-maker.sh testing_files/env")
    sh(v, "mkdir -p scripts job-output")
    sh(v, "bash ./deploy.sh testing_files/env", timeout=2400)
    wait_http(v, "http://localhost:8000/health")
    print(v.run("curl", "-fsS", "http://localhost:8000/health"))


def step_scripts():
    v = vm()
    v.sudo("systemctl", "disable", "--now", "github_sync.timer", check=False)  # it would reset scripts/ every minute
    for script in sorted((HERE / "scripts").glob("*.py")):
        v.sudo("install", "-o", "1000", "-g", "1000", "-m", "644",
               f"{REMOTE_DIR}/testing_files/python-api/scripts/{script.name}", f"{REMOTE_DIR}/scripts/{script.name}")
    print(v.run("curl", "-fsS", "http://localhost:8000/scripts"))


def step_n8n_venv():
    v = vm()
    wait_http(v, "http://localhost:5678/healthz/readiness")
    created_before = metric(v, "py_api_venv_creations_total")
    v.sudo("docker", "cp", f"{REMOTE_DIR}/testing_files/python-api/venv-test.workflow.json", "n8n-main:/tmp/venv-test.json")
    print(v.sudo("docker", "exec", "n8n-main", "n8n", "import:workflow", "--input=/tmp/venv-test.json"))
    r = v.conn.run(f"sudo docker exec n8n-main n8n execute --id={WORKFLOW_ID} --rawOutput",
                   warn=True, hide=True, timeout=600, in_stream=False)
    print(r.stdout[-3000:], r.stderr[-2000:])
    if r.exited != 0:
        raise StepFailed(f"n8n execute exited {r.exited} (the IF node failed into 'Venv failed', or n8n could not run it)")
    if '"python"' not in r.stdout or "/venvs/" not in r.stdout:
        raise StepFailed("n8n output does not show the script running in a venv")
    created = metric(v, "py_api_venv_creations_total") - created_before
    if created != 1:
        raise StepFailed(f"py_api_venv_creations_total +{created}, expected +1 (venv built for this run)")
    print("n8n -> python-api: venv built and used")


def step_api(steps: list[str]):
    v = vm()
    sh(v, f"python3 testing_files/python-api/test_api.py --url http://localhost:8000 {' '.join(steps)}", timeout=1800)


def step_evidence():
    v = vm()
    out = Path(os.environ.get("OUT", "evidence"))
    out.mkdir(parents=True, exist_ok=True)
    for name, command in {
        "compose-ps.txt": "sudo docker compose ps",
        "python-api.log": "sudo docker compose logs --no-color --tail=1000 python-api",
        "python-api-stats.txt": "sudo docker stats --no-stream python-api",
        "health.json": "curl -fsS http://localhost:8000/health",
        "metrics.txt": "curl -fsS http://localhost:8000/metrics",
    }.items():
        r = v.conn.run(f"cd ~/{REMOTE_DIR} && {command}", warn=True, hide=True, timeout=120, in_stream=False)
        (out / name).write_text(r.stdout + r.stderr)
    print(f"evidence in {out}: {', '.join(sorted(p.name for p in out.iterdir()))}")


def step_destroy():
    if not STATE.exists():
        print("no VM to remove")
        return
    print(LinuxVM.destroy_id(json.loads(STATE.read_text())["vmid"]))
    STATE.unlink()


STEPS = {"create": step_create, "ship": step_ship, "deploy": step_deploy, "scripts": step_scripts,
         "n8n-venv": step_n8n_venv, "evidence": step_evidence, "destroy": step_destroy}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in [*STEPS, "api"]:
        print(__doc__)
        return 2
    name = sys.argv[1]
    try:
        if name == "api":
            step_api(sys.argv[2:])
        else:
            STEPS[name]()
    except (StepFailed, LinuxVMError, CommandTimedOut, OSError, subprocess.CalledProcessError) as e:
        print(f"step {name} failed: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
