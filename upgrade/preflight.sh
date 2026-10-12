#!/bin/bash
# Step 4: prove the new version will use the same data, passwords and keys as the running one, BEFORE
# anything restarts. Compares the resolved compose config (new code + env file) with the snapshot backup.sh
# took of the running one. Secrets are compared, never printed.
#
# Usage (from the project directory): bash <upgrade-dir>/preflight.sh <backup_dir> [env_file]
#   env_file default: .env.upgraded if it exists (code upgrade), else .env (image-only update)
set -euo pipefail
source "$(dirname "$0")/_common.sh"

bdir="${1:?usage: preflight.sh <backup_dir> [env_file]}"
envf="${2:-}"; [[ -n "$envf" ]] || { [[ -f .env.upgraded ]] && envf=.env.upgraded || envf=.env; }
[[ -f "$bdir/compose-config-old.json" ]] || die "no $bdir/compose-config-old.json: run backup.sh first"
[[ -f "$envf" ]] || die "no $envf"

new="$(mktemp)"; trap 'rm -f "$new"' EXIT
config_json "$new" "$envf"
say "comparing running (backup snapshot) vs new ($envf, not started)"
ALLOW="${PREFLIGHT_ALLOW_CHANGE[*]:-}" python3 - "$bdir/compose-config-old.json" "$new" <<'PY'
import json, os, re, sys
old, new = (json.load(open(p)) for p in sys.argv[1:3])
allow = set(os.environ.get("ALLOW", "").split())
SECRET = re.compile(r"PASSWORD|PASSWD|SECRET|KEY|TOKEN|_USER$|^USER$|USERNAME|_URI$|DATABASE_URL|DSN", re.I)
DB_IMG = re.compile(r"(^|/)(postgres|postgis|timescaledb|pgvector|mysql|mariadb|percona|redis|valkey|keydb|mongo)[^/]*(:|$)")
fails = warns = 0

def env(cfg, svc):
    e = (cfg.get("services", {}).get(svc) or {}).get("environment") or {}
    return e if isinstance(e, dict) else dict(x.split("=", 1) for x in e if "=" in x)

def report(level, label, detail=""):
    global fails, warns
    color = {"OK": "32", "FAIL": "31", "WARN": "33"}[level]
    print(f"  \033[1;{color}m{level:<5}\033[0m {label}{(': ' + detail) if detail else ''}")
    fails += level == "FAIL"; warns += level == "WARN"

# 1. same compose project + same volume names = same data
report("OK" if old.get("name") == new.get("name") else "FAIL", "compose project (data volume prefix)",
       f"{old.get('name')!r}" if old.get("name") == new.get("name") else f"{old.get('name')!r} -> {new.get('name')!r}")
for v, spec in (old.get("volumes") or {}).items():
    a = (spec or {}).get("name"); b = ((new.get("volumes") or {}).get(v) or {}).get("name")
    if b is None: report("WARN", f"volume {v}", "not used by the new version (its data stays on disk)")
    elif a != b:  report("FAIL", f"volume {v}", f"{a!r} -> {b!r}")
    else:         report("OK", f"volume {v}", a)

# 2. database images: a new MAJOR version can't read the old data files (needs dump + restore)
for svc, s in (old.get("services") or {}).items():
    img_a = s.get("image", ""); img_b = ((new.get("services") or {}).get(svc) or {}).get("image", "")
    if DB_IMG.search(img_a or ""):
        tag = lambda i: (i.rsplit(":", 1)[1] if ":" in i.split("/")[-1] else "latest").split("-")[0].split(".")[0]
        if not img_b: report("WARN", f"database {svc}", "gone from the new version")
        elif tag(img_a) != tag(img_b): report("FAIL", f"database {svc} image {img_a} -> {img_b}", "major version change: plan a dump + restore")
        else: report("OK", f"database {svc} image", img_b)

# 3. every credential-like value, in every service: identical
for svc in sorted(set(old.get("services") or {}) & set(new.get("services") or {})):
    a, b = env(old, svc), env(new, svc)
    for k in sorted(set(a) | set(b)):
        if not SECRET.search(k): continue
        if k in allow: report("WARN" if a.get(k) != b.get(k) else "OK", f"{svc}: {k}", "changes (allowed)" if a.get(k) != b.get(k) else ""); continue
        if a.get(k) == b.get(k): report("OK", f"{svc}: {k}")
        elif k not in b: report("FAIL", f"{svc}: {k}", "missing in the new version")
        elif k not in a: report("WARN", f"{svc}: {k}", "new in this version (check it's not a database password for an existing database)")
        else: report("FAIL", f"{svc}: {k}", "differs")

# 4. Traefik routes on the network named in providers.docker.network / the traefik.docker.network label. It must
#    be the REAL Docker name (compose prefixes the project): otherwise Traefik logs "Could not find network" and
#    picks the container's first network, sometimes one it isn't on -> 504 after any container recreate.
real = {(spec or {}).get("name", k) for k, spec in (new.get("networks") or {}).items()}
for svc, s in sorted((new.get("services") or {}).items()):
    cmd = s.get("command") or []
    cmd = cmd.split() if isinstance(cmd, str) else cmd
    labels = s.get("labels") or {}
    labels = dict(l.split("=", 1) for l in labels if "=" in l) if isinstance(labels, list) else labels
    wanted = [(f"{svc}: --providers.docker.network", a.split("=", 1)[1]) for a in cmd if a.startswith("--providers.docker.network=")]
    if labels.get("traefik.docker.network"): wanted.append((f"{svc}: label traefik.docker.network", labels["traefik.docker.network"]))
    for label, n in wanted:
        if n in real: report("OK", f"traefik network {label}", n)
        else: report("FAIL", f"traefik network {label}={n}", f"no network has that exact name (real: {', '.join(sorted(real))})")

print()
if fails:
    print(f"\033[1;31m{fails} check(s) failed: do NOT upgrade. Fix the env file and rerun.\033[0m"); sys.exit(1)
print(f"\033[1;32mAll checks passed{f' ({warns} warnings)' if warns else ''}: the new version uses the existing data, passwords and keys.\033[0m")
PY
