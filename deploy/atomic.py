#!/usr/bin/env python3
"""atomic.py: move a single-folder naypyers install to the code/state layout, step by step, on the host itself.

  <base>/main/       checkout of main (code only)             .env -> ../local/.env
  <base>/<install>/  checkout of the install branch (NB stack) .env -> ../local/.env   (optional)
  <base>/local/      state: .env, scripts/, job-output/, prometheus/conf.d/, backups/

Usage (as root, from anywhere; run the steps in this order, one per call):
  atomic.py plan                      list the steps
  atomic.py step <name> [options]     run one step       (Jenkins: one stage per step)
  atomic.py all [options]             every step in order, stops at the first failure
  atomic.py rollback [options]        back to the old folder (its compose + timers); state untouched
  atomic.py check-state               used by deploy.sh: fail if NAYPYERS_EXPECT_STATE=1 and a volume is missing

Steps: clone inventory backup make-local link config-check stop-old start-new verify
Nothing changes before stop-old. Until then the old folder runs as before; after it, `rollback` restores it.

Options (same on every call):
  --old DIR               the running install     (default /root/n8n_python_auto-scaling_health-checks)
  --base DIR              the new layout          (default /root/naypyers)
  --install-source SRC    bundle file or git URL of the install branch (default /root/naypyers-install.bundle;
                          "none" = no install branch)
  --install-ref REF       branch in it            (default hostinger)
  --install-dir NAME      folder name under base  (default hostinger)
  --expect old|new        verify: which folder must be live (default new)
Stdlib only: the host needs python3, docker, git. Errors print "step <name> failed: <reason>" and exit 1."""

##### CONFIG #####
import argparse, hashlib, json, os, re, shutil, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULTS = {"old": "/root/n8n_python_auto-scaling_health-checks", "base": "/root/naypyers",
            "install_source": "/root/naypyers-install.bundle", "install_ref": "hostinger",
            "install_dir": "hostinger", "expect": "new"}
STEPS = ["clone", "inventory", "backup", "make-local", "link", "config-check", "stop-old", "start-new", "verify"]

SECRET_KEY = re.compile(r"PASS|SECRET|KEY|TOKEN|JWT|AUTH|_URI$", re.I)     # credential-like env keys
# .env keys whose compose fallback is gone (${VAR:?}): if the live .env lacks one, the value the running
# container really uses is written in, so nothing changes. container -> keys
REQUIRED_FROM_CONTAINER = {"postgres": ["POSTGRES_PASSWORD"], "grafana": ["GF_SECURITY_ADMIN_PASSWORD"],
                           "n8n-main": ["N8N_BASIC_AUTH_PASSWORD", "N8N_ENCRYPTION_KEY"]}
IMAGE_PINS = {"postgres": "POSTGRES_IMAGE", "redis": "REDIS_IMAGE", "grafana": "GRAFANA_IMAGE"}
# n8n tables that must have exactly the same rows after the move (the rest may only grow: executions, events)
N8N_SAME_ROWS = ["workflow_entity", "credentials_entity", "user", "project", "shared_workflow", "shared_credentials"]
HEALTH = [("python-api", "http://127.0.0.1:8000/health"), ("n8n", "http://127.0.0.1:5678/healthz/readiness"),
          ("prometheus", "http://127.0.0.1:9090/-/ready")]
RESTART_WATCH_SECONDS = 30      # verify: a container that restarts in this window is crash-looping, even if "running"
TIMERS = ["autoscaler.timer", "github_sync.timer"]
COPY_DIRS = ["scripts", "job-output", "prometheus/conf.d"]      # old folder -> local/ (same relative path)
COPY_FILES = ["setup-grafana.sh"]


##### ERRORS #####
class StepFailed(Exception):
    """A step could not finish; the message says why."""


class CommandFailed(StepFailed):
    """A command returned non-zero."""


##### HELPERS #####
def run(cmd, cwd=None, check=True, quiet=False, env=None):
    """Run cmd (argument list); return stdout. Raise CommandFailed with its output when it fails."""
    if not quiet:
        print("  $", " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                       env=dict(os.environ, **(env or {})))
    if check and r.returncode != 0:
        raise CommandFailed(f"{' '.join(str(c) for c in cmd)}: {(r.stderr or r.stdout).strip()[-800:]}")
    return r.stdout.strip() if r.returncode == 0 else ""


def stream(cmd, cwd=None, env=None):
    """Run cmd with its output shown live (long steps: backup, shutdown, compose up).
    ATOMIC_DEBUG=1: bash scripts run with -x (every command they run is shown)."""
    if os.environ.get("ATOMIC_DEBUG") == "1" and cmd[0] == "bash":
        cmd = ["bash", "-x", *cmd[1:]]
    print("  $", " ".join(str(c) for c in cmd), flush=True)
    rc = subprocess.run([str(c) for c in cmd], cwd=cwd, env=dict(os.environ, **(env or {}))).returncode
    if rc != 0:
        raise CommandFailed(f"{' '.join(str(c) for c in cmd)}: exit {rc} (output above)")


def docker(*args, check=True):
    return run(["docker", *args], check=check, quiet=True)


def read_env(path):
    """KEY=VALUE pairs of a .env file (last one wins, quotes stripped, comments skipped)."""
    return parse_env(Path(path).read_text())


def parse_env(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.split(" #")[0].strip().strip("'\"")
    return values


def redact(obj):
    """Copy of a compose config with every credential-like value replaced by ***."""
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEY.search(k) and isinstance(v, str) and v else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def save(paths, name, data):
    f = paths["evidence"] / name
    f.write_text(json.dumps(data, indent=1, sort_keys=True) if not isinstance(data, str) else data)
    os.chmod(f, 0o600)
    print(f"  saved {f}")


def load(paths, name):
    f = paths["evidence"] / name
    if not f.exists():
        raise StepFailed(f"{f} missing: run the earlier steps first")
    return json.loads(f.read_text())


def compose_config(cwd, extra=None):
    """Resolved compose config (JSON) of the project in cwd; fails on a missing required .env key."""
    cmd = extra or ["docker", "compose"]
    return json.loads(run(cmd + ["config", "--format", "json"], cwd=cwd, quiet=True))


def checksums(root):
    root = Path(root)
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.relative_to(root).parts}


def copy_tree(src, dst):
    """Copy src into dst keeping modes and owners (python-api and n8n read these as uid 1000)."""
    src, dst = Path(src), Path(dst)
    if not src.exists():
        return 0
    n = 0
    for p in [src, *sorted(src.rglob("*"))]:
        target = dst / p.relative_to(src)
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            shutil.copy2(p, target)
            n += 1
        st = p.stat()
        os.chown(target, st.st_uid, st.st_gid)
        shutil.copymode(p, target)
    return n


def container_env(name):
    raw = docker("inspect", "-f", "{{json .Config.Env}}", name, check=False)
    return dict(e.split("=", 1) for e in json.loads(raw or "[]") if "=" in e)


def http_ok(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


##### INVENTORY (read-only, used before and after) #####
def project_of(old):
    names = set(docker("ps", "-a", "--filter", f"label=com.docker.compose.project.working_dir={old}",
                       "--format", '{{.Label "com.docker.compose.project"}}').split())
    if len(names) != 1:
        raise StepFailed(f"expected one compose project started from {old}, found {sorted(names) or 'none'}")
    return names.pop()


def containers(project):
    out = []
    for proj in (project, f"{project}-nb"):
        ids = docker("ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={proj}").split()
        for i in ids:
            c = json.loads(docker("inspect", i))[0]
            lab = c["Config"]["Labels"]
            out.append({"name": c["Name"].lstrip("/"), "image": c["Config"]["Image"], "image_id": c["Image"],
                        "running": c["State"]["Running"], "restarts": c["RestartCount"],
                        "restarting": c["State"]["Restarting"], "project": lab.get("com.docker.compose.project"),
                        "service": lab.get("com.docker.compose.service"),
                        "working_dir": lab.get("com.docker.compose.project.working_dir"),
                        "networks": sorted(c["NetworkSettings"]["Networks"])})
    return sorted(out, key=lambda c: c["name"])


def row_counts(container):
    """{db: {table: rows}} for every database in a Postgres container (exact counts)."""
    env = container_env(container)
    user = env.get("POSTGRES_USER", "postgres")
    q = ("SELECT table_schema||'.'||table_name, (xpath('/row/c/text()', query_to_xml(format("
         "'select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text "
         "FROM information_schema.tables WHERE table_type='BASE TABLE' "
         "AND table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1")
    dbs = docker("exec", container, "psql", "-U", user, "-d", "postgres", "-At", "-c",
                 "SELECT datname FROM pg_database WHERE NOT datistemplate").split()
    counts = {}
    for db in dbs:
        rows = docker("exec", container, "psql", "-U", user, "-d", db, "-At", "-F", "\t", "-c", q).splitlines()
        counts[db] = {t: int(n) for t, n in (r.split("\t") for r in rows if "\t" in r)}
    return counts


def n8n_credentials():
    """How many credentials n8n can decrypt (proves N8N_ENCRYPTION_KEY still matches). Nothing leaves the container."""
    f = "/home/node/.n8n/atomic-credcheck.json"
    r = subprocess.run(["docker", "exec", "n8n-main", "n8n", "export:credentials", "--all", "--decrypted",
                        f"--output={f}"], capture_output=True, text=True)
    if r.returncode != 0:
        if "No credentials found" in r.stdout + r.stderr:
            return 0
        raise StepFailed(f"n8n can't export (decrypt) its credentials: {(r.stderr or r.stdout).strip()[-300:]}")
    try:
        n = len(json.loads(docker("exec", "n8n-main", "cat", f)))
    finally:
        docker("exec", "n8n-main", "rm", "-f", f, check=False)
    return n


def unit_env(unit):
    out = run(["systemctl", "show", unit, "-p", "Environment", "--value"], check=False, quiet=True)
    return dict(e.split("=", 1) for e in out.split() if "=" in e)


def collect(project):
    cs = containers(project)
    names = {c["name"] for c in cs}
    data = {"time": datetime.now(timezone.utc).isoformat(), "project": project, "containers": cs,
            "volumes": sorted(v for v in docker("volume", "ls", "--format", "{{.Name}}").split()
                              if v.startswith(f"{project}_")),
            "networks": sorted(n for n in docker("network", "ls", "--format", "{{.Name}}").split()
                               if n.startswith(f"{project}_")),
            "rows": {c: row_counts(c) for c in ("postgres", "postgres-nb") if c in names},
            "units": {u: unit_env(u.replace(".timer", ".service")) for u in TIMERS},
            "timers_active": {u: run(["systemctl", "is-active", u], check=False, quiet=True) or "inactive"
                              for u in TIMERS}}
    if "n8n-main" in names:
        data["n8n_version"] = docker("exec", "n8n-main", "n8n", "--version").splitlines()[-1].strip()
        data["n8n_credentials"] = n8n_credentials()
    return data


##### STEPS #####
def step_clone(o, paths):
    """Check main/ (cloned by you) and clone the install branch next to it."""
    main = paths["main"]
    if not (main / ".git").exists() or not (main / "deploy" / "atomic.py").exists():
        raise StepFailed(f"{main} is not a checkout of main: git clone it first (see deploy/README.md)")
    if run(["git", "status", "--porcelain"], cwd=main, quiet=True):
        raise StepFailed(f"{main} has local changes: it must be a clean checkout")
    print(f"  main: {run(['git', 'log', '-1', '--format=%h %s'], cwd=main, quiet=True)}")
    if o.install_source == "none":
        return
    inst = paths["install"]
    if not inst.exists():
        run(["git", "clone", "--branch", o.install_ref, o.install_source, inst])
    if run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=inst, quiet=True) != o.install_ref:
        raise StepFailed(f"{inst} is not on branch {o.install_ref}")
    if not (inst / "nb.py").exists():
        raise StepFailed(f"{inst} has no nb.py: wrong branch or source?")
    print(f"  {inst.name}: {run(['git', 'log', '-1', '--format=%h %s'], cwd=inst, quiet=True)}")


def step_inventory(o, paths):
    """Read-only snapshot of the running install: project, containers, volumes, rows, credentials, timers."""
    old = Path(o.old)
    project = project_of(old)
    if project != old.name:
        print(f"  note: project '{project}' differs from the folder name '{old.name}'")
    data = collect(project)
    data["old_dir"] = str(old)
    data["git"] = {"head": run(["git", "rev-parse", "HEAD"], cwd=old, quiet=True),
                   "status": run(["git", "status", "--porcelain"], cwd=old, quiet=True)}
    data["images"] = {c["service"]: {"ref": c["image"], "digests": json.loads(
        docker("image", "inspect", "-f", "{{json .RepoDigests}}", c["image_id"]) or "[]")}
        for c in data["containers"] if c["service"]}
    data["secrets_in_compose"] = literal_secrets(old)
    save(paths, "inventory-before.json", data)
    save(paths, "compose-old-redacted.json", redact(compose_config(old)))
    if data["secrets_in_compose"]:
        print(f"  WARN secrets written directly in the old compose files: {data['secrets_in_compose']}")
        print("       config-check compares every credential the services get, old vs new")
    print(f"  project {project}: {sum(c['running'] for c in data['containers'])} running containers, "
          f"{len(data['volumes'])} volumes, n8n {data.get('n8n_version')}, {data.get('n8n_credentials')} credentials")


def literal_secrets(old):
    """Credential-like keys given a literal value (not ${VAR}) in the old folder's compose files."""
    found = []
    for f in sorted(Path(old).glob("*compose*.y*ml")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            m = re.match(r"\s*-?\s*([A-Z0-9_]+)\s*[=:]\s*(.+)", line)
            if m and SECRET_KEY.search(m[1]) and "${" not in m[2] and not line.strip().startswith("#"):
                value = m[2].strip().strip("'\"")
                if value and value.lower() not in ("true", "false"):
                    found.append(f"{f.name}:{n} {m[1]}")
    return found


def step_backup(o, paths):
    """Full backup of the running install with the new version's upgrade/backup.sh (changes nothing)."""
    out = paths["evidence"] / "backup"
    if out.exists() and any(out.iterdir()):
        print(f"  {out} exists: kept (delete it to back up again)")
        return
    stream(["bash", paths["main"] / "upgrade" / "backup.sh", out], cwd=o.old)


def step_make_local(o, paths):
    """local/: copy the state out of the old folder; write the live .env with the install's identity pinned."""
    old, local, inv = Path(o.old), paths["local"], load(paths, "inventory-before.json")
    local.mkdir(parents=True, exist_ok=True)
    os.chmod(local, 0o700)
    for d in COPY_DIRS:
        for attempt in range(3):           # github_sync rewrites scripts/ every few minutes: retry a copy it raced
            n = copy_tree(old / d, local / d)
            (local / d).mkdir(parents=True, exist_ok=True)
            if not (old / d).exists() or checksums(old / d) == checksums(local / d):
                break
            time.sleep(20)
        else:
            raise StepFailed(f"copy of {d}/ keeps differing from the original")
        print(f"  {d}/: {n} files")
    for f in COPY_FILES:
        if (old / f).exists():
            shutil.copy2(old / f, local / f)
    if o.install_source != "none" and (old / "db").exists():
        n = copy_tree(old / "db", paths["install"] / "db")
        if {k: v for k, v in checksums(paths["install"] / "db").items() if k != ".gitkeep"} != checksums(old / "db"):
            raise StepFailed("copy of db/ differs from the original")
        print(f"  db/ -> {paths['install'].name}/db/: {n} files (untracked there)")
    write_env(old, local, inv, paths)


def write_env(old, local, inv, paths):
    live = read_env(old / ".env")
    template = read_env(paths["main"] / "env")
    pins = {"COMPOSE_PROJECT_NAME": inv["project"], "NAYPYERS_LOCAL": str(local), "NAYPYERS_EXPECT_STATE": "1",
            "N8N_VERSION": inv["n8n_version"]}
    for service, key in IMAGE_PINS.items():
        digests = inv["images"].get(service, {}).get("digests") or []
        if not digests:
            raise StepFailed(f"no registry digest for the running {service} image: can't pin {key}")
        pins[key] = digests[0]
    for container, keys in REQUIRED_FROM_CONTAINER.items():
        env = container_env(container)
        for k in keys:
            if not live.get(k):
                if not env.get(k):
                    raise StepFailed(f"{k} is neither in .env nor in the running {container}")
                pins[k] = env[k]
                print(f"  {k}: not in .env, using the value {container} runs with")
    if live.get("N8N_VERSION") and live["N8N_VERSION"] != pins["N8N_VERSION"]:
        raise StepFailed(f".env says N8N_VERSION={live['N8N_VERSION']} but {pins['N8N_VERSION']} is running: fix .env first")
    missing = {k: v for k, v in template.items() if k not in live and k not in pins}
    needs_value = [k for k, v in missing.items() if "replace_" in v]
    if needs_value:
        raise StepFailed(f"new keys in the env template need a real value in the old .env first: {needs_value}")
    text = (old / ".env").read_text().rstrip("\n") + "\n\n"
    text += f"# --- added by deploy/atomic.py {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC ---\n"
    text += "# install identity and pins (see env template)\n"
    text += "".join(f"{k}={v}\n" for k, v in pins.items())
    if missing:
        text += "# keys new in the env template (template values)\n"
        text += "".join(f"{k}={v}\n" for k, v in missing.items())
    target = local / ".env"
    if target.exists():
        if read_env(target) != parse_env(text):          # rerun: same values = done (only the date comment differs)
            raise StepFailed(f"{target} exists with other values: compare it with the old .env, remove it, rerun")
        print(f"  {target} already written, same values")
        return
    target.write_text(text)
    os.chmod(target, 0o600)
    print(f"  {target}: old .env + {len(pins)} pinned keys + {len(missing)} new template keys")


def step_link(o, paths):
    """main/.env and the install's .env -> ../local/.env"""
    dirs = [paths["main"]] + ([paths["install"]] if o.install_source != "none" else [])
    for d in dirs:
        link = d / ".env"
        if link.is_symlink() and os.readlink(link) == "../local/.env":
            continue
        if link.exists() or link.is_symlink():
            raise StepFailed(f"{link} exists and isn't the symlink to ../local/.env")
        link.symlink_to("../local/.env")
        print(f"  {link} -> ../local/.env")


def step_config_check(o, paths):
    """Old vs new resolved config: project, volumes, networks, every credential, images. Changes nothing."""
    inv = load(paths, "inventory-before.json")
    old_cfg = compose_config(o.old)
    new_cfg = compose_config(paths["main"])
    problems, notes = [], []
    if new_cfg["name"] != inv["project"]:
        problems.append(f"project: new '{new_cfg['name']}' != running '{inv['project']}'")
    existing = set(docker("volume", "ls", "--format", "{{.Name}}").split())
    for key, v in new_cfg.get("volumes", {}).items():
        if v["name"] not in existing:
            problems.append(f"volume {v['name']} doesn't exist: the new stack would start with it EMPTY")
    for key, net in new_cfg.get("networks", {}).items():
        if key in old_cfg.get("networks", {}) and net["name"] != old_cfg["networks"][key]["name"]:
            problems.append(f"network {key}: {old_cfg['networks'][key]['name']} -> {net['name']}")
    for name, svc in new_cfg["services"].items():
        old_svc = old_cfg["services"].get(name)
        if not old_svc:
            notes.append(f"service {name} is new")
            continue
        old_env, new_env = old_svc.get("environment") or {}, svc.get("environment") or {}
        for k in sorted(set(old_env) | set(new_env)):
            if SECRET_KEY.search(k) and old_env.get(k) != new_env.get(k):
                problems.append(f"{name}: {k} would change")
        if svc.get("image") and svc["image"] != old_svc.get("image"):
            notes.append(f"{name}: image {old_svc.get('image')} -> {svc['image']}")
    for name in old_cfg["services"]:
        if name not in new_cfg["services"]:
            notes.append(f"service {name} not in the new main compose (install branch: {name.endswith('-nb')})")
    if o.install_source != "none":
        nb = json.loads(run(["python3", paths["install"] / "nb.py", "config", "--format", "json"], quiet=True))
        if nb["name"] != f"{inv['project']}-nb":
            problems.append(f"NB project '{nb['name']}' != '{inv['project']}-nb'")
        for v in nb.get("volumes", {}).values():
            if v["name"] not in existing:
                problems.append(f"NB volume {v['name']} doesn't exist")
        for name, svc in nb["services"].items():
            old_env = (old_cfg["services"].get(name) or {}).get("environment") or {}
            for k, val in (svc.get("environment") or {}).items():
                if SECRET_KEY.search(k) and old_env and old_env.get(k) != val:
                    problems.append(f"{name}: {k} would change")
    report = {"problems": problems, "notes": notes}
    save(paths, "config-check.json", report)
    save(paths, "compose-new-redacted.json", redact(new_cfg))
    for n in notes:
        print(f"  note: {n}")
    if problems:
        raise StepFailed("; ".join(problems))
    print("  same project, volumes, networks and credentials")


def step_stop_old(o, paths):
    """Patient stop of the old stack (main + NB containers of the project), timers paused."""
    project = load(paths, "inventory-before.json")["project"]
    stream(["bash", paths["main"] / "upgrade" / "db-shutdown.sh", "stop", "--project", project,
            "--project", f"{project}-nb", "--no-dump"])


def step_start_new(o, paths):
    """Start the stack from main/ (and NB from the install branch), reinstall the timers there."""
    # --force-recreate: unchanged services (traefik, cadvisor...) would otherwise just restart with the old folder's labels
    stream(["docker", "compose", "up", "-d", "--build", "--force-recreate"], cwd=paths["main"])
    if o.install_source != "none":
        stream(["python3", paths["install"] / "nb.py", "up", "-d"])
    stream(["bash", paths["main"] / "autoscaler" / "setup-systemd.sh"], cwd=paths["main"])
    stream(["bash", paths["main"] / "upgrade" / "db-shutdown.sh", "start"])     # clears its state, resumes timers


def step_verify(o, paths):
    """Same data, credentials, volumes and containers as before; health URLs; timers point at the live folder."""
    before = load(paths, "inventory-before.json")
    live = Path(o.old) if o.expect == "old" else paths["main"]
    wait_healthy()
    after = collect(before["project"])
    save(paths, f"inventory-after-{o.expect}.json", after)
    problems = crash_loops(before["project"], after)
    new_vols = set(after["volumes"]) - set(before["volumes"])
    if new_vols:
        problems.append(f"new (empty) volumes appeared: {sorted(new_vols)}")
    if set(before["volumes"]) - set(after["volumes"]):
        problems.append(f"volumes gone: {sorted(set(before['volumes']) - set(after['volumes']))}")
    running_before = {c["name"] for c in before["containers"] if c["running"] and "n8n-worker" not in c["name"]}
    running_after = {c["name"] for c in after["containers"] if c["running"]}
    if running_before - running_after:
        problems.append(f"not running: {sorted(running_before - running_after)}")
    for c in after["containers"]:
        if c["running"] and c["working_dir"] and Path(c["working_dir"]) not in (live, paths["install"]):
            problems.append(f"{c['name']} runs from {c['working_dir']}, expected {live}")
    for container, dbs in before["rows"].items():
        for db, tables in dbs.items():
            for t, n in tables.items():
                m = after["rows"].get(container, {}).get(db, {}).get(t)
                same = container == "postgres" and t.split(".")[-1] in N8N_SAME_ROWS
                if m is None or (m != n if same else m < n):
                    problems.append(f"{container}/{db}/{t}: {n} rows before, {m} after")
    if after.get("n8n_credentials") != before.get("n8n_credentials"):
        problems.append(f"n8n decrypts {after.get('n8n_credentials')} credentials, {before.get('n8n_credentials')} before")
    if after.get("n8n_version") != before.get("n8n_version"):
        problems.append(f"n8n {before.get('n8n_version')} -> {after.get('n8n_version')}")
    want_dir = str(live)
    if after["units"]["autoscaler.timer"].get("PROJECT_DIR") != want_dir:
        problems.append(f"autoscaler PROJECT_DIR={after['units']['autoscaler.timer'].get('PROJECT_DIR')}, expected {want_dir}")
    want_scripts = str((live if o.expect == "old" else paths["local"]) / "scripts")
    if after["units"]["github_sync.timer"].get("SCRIPTS_DIR") != want_scripts:
        problems.append(f"github_sync SCRIPTS_DIR={after['units']['github_sync.timer'].get('SCRIPTS_DIR')}, expected {want_scripts}")
    for t, state in after["timers_active"].items():
        if before["timers_active"].get(t) == "active" and state != "active":
            problems.append(f"{t} is {state}")
    exporter = docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", "postgres-exporter", check=False)
    if exporter and f"{before['project']}_n8n-network" not in json.loads(exporter):
        problems.append("postgres-exporter lost the n8n network: restart it (docker restart postgres-exporter)")
    if problems:
        raise StepFailed("; ".join(problems))
    print(f"  live from {live}: data, credentials, volumes, containers, health, timers as before")


def crash_loops(project, after):
    """Containers restarting by themselves (Docker still calls them running between two crashes)."""
    time.sleep(RESTART_WATCH_SECONDS)
    later = {c["name"]: c for c in containers(project)}
    out = []
    for c in after["containers"]:
        now = later.get(c["name"])
        if c["running"] and now and (now["restarting"] or now["restarts"] > c["restarts"]):
            out.append(f"{c['name']} keeps restarting (docker logs {c['name']})")
    return out


def wait_healthy(timeout=600):
    deadline = time.time() + timeout
    pending = dict(HEALTH)
    while pending and time.time() < deadline:
        pending = {n: u for n, u in pending.items() if not http_ok(u)}
        if pending:
            time.sleep(10)
    if pending:
        raise StepFailed(f"not healthy after {timeout}s: {', '.join(f'{n} ({u})' for n, u in pending.items())}")


def step_rollback(o, paths):
    """Back to the old folder: stop the new stack, NB back inside the old project, old timers."""
    project = load(paths, "inventory-before.json")["project"]
    stream(["bash", paths["main"] / "upgrade" / "db-shutdown.sh", "stop", "--project", project,
            "--project", f"{project}-nb", "--no-dump"])
    if o.install_source != "none" and (paths["install"] / "nb.py").exists():
        stream(["python3", paths["install"] / "nb.py", "down"])          # frees the container names; volume kept
    # --build: start-new built the NEW python-api under the same image name; rebuild it from the old folder's code
    stream(["docker", "compose", "up", "-d", "--build", "--force-recreate"], cwd=o.old)
    stream(["bash", Path(o.old) / "autoscaler" / "setup-systemd.sh"], cwd=o.old)
    stream(["bash", paths["main"] / "upgrade" / "db-shutdown.sh", "start"])
    print(f"  old folder {o.old} is live again; {paths['base']} kept (start-new switches back)")


def check_state():
    """deploy.sh guard: with NAYPYERS_EXPECT_STATE=1, every volume of this stack must already exist."""
    env = read_env(".env") if Path(".env").exists() else {}
    if env.get("NAYPYERS_EXPECT_STATE") != "1":
        return
    cmd = ["docker"] if subprocess.run(["docker", "info"], capture_output=True).returncode == 0 else ["sudo", "docker"]
    cfg = compose_config(".", cmd + ["compose"])
    have = set(run(cmd + ["volume", "ls", "--format", "{{.Name}}"], quiet=True).split())
    missing = [v["name"] for v in cfg.get("volumes", {}).values() if v["name"] not in have]
    if missing:
        raise StepFailed(f"NAYPYERS_EXPECT_STATE=1 but these volumes are missing (Docker would create them empty): "
                         f"{missing}. Wrong COMPOSE_PROJECT_NAME or folder?")


STEP_FUNCS = {"clone": step_clone, "inventory": step_inventory, "backup": step_backup,
              "make-local": step_make_local, "link": step_link, "config-check": step_config_check,
              "stop-old": step_stop_old, "start-new": step_start_new, "verify": step_verify,
              "rollback": step_rollback}


##### RUNNER #####
def parse(argv):
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("cmd")
    p.add_argument("name", nargs="?")
    for k, v in DEFAULTS.items():
        p.add_argument("--" + k.replace("_", "-"), dest=k, default=v)
    return p.parse_args(argv)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    o = parse(argv)
    if o.cmd == "plan":
        print(" ".join(STEPS))
        return 0
    if o.cmd == "check-state":
        names = ["check-state"]
    else:
        names = {"all": STEPS, "rollback": ["rollback"], "step": [o.name]}.get(o.cmd)
        if not names or names[0] not in STEP_FUNCS:
            sys.exit(f"unknown command or step: {' '.join(argv)}\n\n{__doc__}")
    base = Path(o.base)
    paths = {"base": base, "main": base / "main", "install": base / o.install_dir, "local": base / "local",
             "evidence": base / "local" / "backups" / "atomic"}
    for name in names:
        try:
            if name == "check-state":
                check_state()
                continue
            if os.geteuid() != 0:
                raise StepFailed("run as root (the install, its timers and docker are root's)")
            paths["evidence"].mkdir(parents=True, exist_ok=True)
            os.chmod(base / "local", 0o700)
            print(f"== {name}: {STEP_FUNCS[name].__doc__}", flush=True)
            STEP_FUNCS[name](o, paths)
        except StepFailed as e:
            print(f"step {name} failed: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
