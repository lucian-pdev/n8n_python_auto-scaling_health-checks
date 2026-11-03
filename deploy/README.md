# deploy/: releases and the move to the code/state layout

| Script | Runs on | Does |
|---|---|---|
| `release.py` | dev machine, naypyers checkout | `check` main is publishable; `github --push` publishes main to the public repo as ONE new commit (parent = the previous public commit, no private history, no force); `install-bundle` packs the install branch into a bundle file for the server |
| `atomic.py` | the server, as root | moves a single-folder install to `<base>/{main,<install>,local}` step by step, with a rollback; `check-state` is deploy.sh's guard against starting next to missing volumes |

## The move, step by step (`atomic.py`)

| Step | Changes | What it does |
|---|---|---|
| `clone` | `<base>/<install>/` | checks `main/` (you clone it from GitHub) is clean; clones the install branch from its bundle |
| `inventory` | nothing | project name from Docker's labels, containers, volumes, row counts of every database, how many credentials n8n can decrypt, timers; redacted compose config; secrets written directly in compose files |
| `backup` | nothing | `upgrade/backup.sh` of the running install (dumps, volumes, config) into `local/backups/atomic/backup` |
| `make-local` | `local/` | copies `scripts/`, `job-output/`, `prometheus/conf.d/`, `setup-grafana.sh`; `db/` into the install checkout (untracked); writes `local/.env` = old `.env` + pinned identity (project name, state dir, running n8n version, image digests) |
| `link` | symlinks | `main/.env` and `<install>/.env` -> `../local/.env` |
| `config-check` | nothing | old vs new resolved config: same project, volumes exist, same network names, every credential identical |
| `stop-old` | **downtime starts** | patient stop (databases last), timers paused |
| `start-new` | | `docker compose up -d --build` in `main/`, `nb.py up -d` in the install checkout, timers reinstalled there |
| `verify` | nothing | same data, credentials, volumes, containers; health URLs; timers point at the new folders |
| `rollback` | | stop the new stack, start the old folder again (its compose and timers); nothing is deleted |

Until `stop-old` the old folder runs untouched. The old folder is kept until you remove it.
Rehearsal: Jenkins job `naypyers-atomic` (a VM built like the server: switch, rollback, switch again).
