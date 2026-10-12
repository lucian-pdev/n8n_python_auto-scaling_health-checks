# Upgrade workflow (template)

Update the OS of a Docker host, or upgrade a Docker Compose app (new code, new images, or both), **without
losing database data, passwords or keys**, and prove it before and after. Copy this folder into a project as
`upgrade/`, fill in `upgrade.conf`, rehearse it on a test VM, then run it on the real server.

| Script | Step | Changes |
|--------|------|---------|
| `backup.sh [dir]` | 1 | nothing: config, git state, dumps of every database it finds, volumes, host files, an **inventory** (every table's row count) |
| `switch-code.sh <ref>` | 2 | code only (containers keep running); keeps the live `.env`, stashes local edits. Skip for an image-only update |
| `migrate-env.sh` | 3 | writes `.env.upgraded`: live values unchanged + keys new in the template, project name pinned |
| `preflight.sh <backup>` | 4 | nothing: old vs new resolved config. Same project, volumes, credentials; database major versions |
| `apply.sh` | 5 | **downtime**: pull, patient stop (databases last), `DEPLOY_CMD`, restart what's left, resume timers |
| `verify.sh <backup>` | 6 | nothing: same containers running, inventory identical, health URLs, timers |
| `rollback.sh <backup>` | - | code + `.env` back to the backup, data volumes untouched |
| `os-update.sh check\|run\|after` | OS | packages within the release, reboot if needed, everything back up |
| `db-shutdown.sh status\|stop\|start` | any | patient, ordered stop of a whole host: proxies, apps, workers, databases last; never kills |

## Why this order and these checks

- **Databases read their password only when first created.** A new value in `.env` doesn't change the
  database, it locks the app out. `preflight.sh` fails on any changed credential-like value.
- **Docker prefixes volume names with the compose project** (default: the folder name). Another folder or
  project name = new empty volumes = an app that looks empty. `migrate-env.sh` pins `COMPOSE_PROJECT_NAME`,
  `preflight.sh` checks every volume name.
- **App encryption keys** (session secrets, credential keys) must stay identical or saved secrets become
  unreadable and users get logged out: also credential-like, also checked.
- **Docker gives a container 10 s to stop** (on `compose up` recreate, on a reboot, when a Docker package upgrade
  restarts the daemon), then kills it. A busy database then starts with crash recovery. `db-shutdown.sh` stops
  apps first, dumps, then each database with its own stop signal, up to 4 times, 3 minutes apart; a database
  that still hangs is rescued (logs, processes, raw data copy) and flagged until a key is pressed.
- **A database image's major version** (postgres 16 -> 17) can't open the old data files: `preflight.sh` fails
  and the upgrade needs a dump + restore instead (the backup has the dumps).

## Traefik (or any reverse proxy routing by Docker network)

`--providers.docker.network=<name>` and the `traefik.docker.network` label need the **real Docker network name**.
Compose prefixes networks with the project (`proxy` becomes `myapp_proxy`, or `<old-folder>_proxy` on an older
install). With a name that doesn't exist Traefik only warns ("Could not find network ... Defaulting to first
available network") and routes to the container's first network. That works or not depending on network order,
and can flip at any container recreate (an upgrade, a restart): 504 days later. Give the network a fixed name and
use the same one in Traefik:

```yaml
services:
  traefik:
    command:
      - --providers.docker.network=${TRAEFIK_NETWORK:-myapp-proxy}
networks:
  proxy:
    name: ${TRAEFIK_NETWORK:-myapp-proxy}
```

`preflight.sh` fails when a Traefik network setting doesn't match a real network. Check a running install with
`docker logs traefik 2>&1 | grep "Could not find network"`.

## Set up (once per project)

```bash
cp -r ~/repos/Project_Testing/templates/upgrade ~/repos/myapp/upgrade
$EDITOR ~/repos/myapp/upgrade/upgrade.conf      # name, compose files, DEPLOY_CMD, timers, health URLs, hooks
```

Pin image versions in `docker-compose.yml` (`image: app:${APP_VERSION:-1.2.3}`): an image-only update is then
one `.env` change that `preflight.sh` and the backup can see, instead of whatever `latest` is that day.

## Run it

On the server, in the project directory, as the deploy user (with sudo). Get the scripts of the NEW version
without switching the code yet:

```bash
cd /path/to/myapp
git fetch origin
mkdir -p ~/myapp-upgrade && git archive origin/<new-branch> upgrade | tar -x -C ~/myapp-upgrade --strip-components=1
U=~/myapp-upgrade
```

**App upgrade (new code and/or images):**

```bash
bash $U/backup.sh                 # -> ~/myapp-backup-<date>; copy it off the server
B=~/myapp-backup-<date>
bash $U/switch-code.sh <new-branch>   # skip for an image-only update
bash $U/migrate-env.sh            # skip for an image-only update: edit the version in .env instead
bash $U/preflight.sh $B           # must be all green before going on
bash $U/apply.sh                  # downtime starts
bash $U/verify.sh $B
```

**OS update** (separate window, before an app upgrade, with a VM snapshot if you can):

```bash
bash $U/os-update.sh check        # pending updates, reboot needed?, Docker update?, /boot space
bash $U/backup.sh
bash $U/db-shutdown.sh stop       # whole host, patiently
bash $U/os-update.sh run          # non-interactive, keeps changed config files, says if a reboot is needed
sudo reboot                       # if it says so
bash $U/os-update.sh after        # starts what was stopped (databases first), checks everything
```

Containers stopped by `db-shutdown.sh` do not come back by themselves after a reboot (Docker treats them as
stopped by you): `os-update.sh after` (or `db-shutdown.sh start`) brings them back.

## Roll back

- **Code / config:** `bash $U/rollback.sh $B` (stops cleanly, old commit + old `.env`, starts, then `verify.sh`).
- **Data** (only if the new version damaged it): stop the stack, then per database
  `gunzip -c $B/db-<container>.sql.gz | docker exec -i <container> psql -U postgres` into a fresh volume.
  Decide this by hand: it throws away everything written since the backup.
- **OS:** only a VM snapshot undoes an OS update.

## Rehearse it first (Project_Testing)

A fresh VM from an old-OS template, the current version deployed with test data, then exactly the commands
above, unattended. In the project's `testing_files/repro.conf` (see `templates/repro.conf.example`):

```bash
REPRO_FETCH='mkdir -p ~/up && git archive "$NEW_REF" upgrade | tar -x -C ~/up --strip-components=1 && bash ~/up/backup.sh "$REPRO_STATE/backup"'
REPRO_OS_BEFORE='bash ~/up/db-shutdown.sh stop && bash ~/up/os-update.sh run'
REPRO_OS_AFTER='bash ~/up/os-update.sh after'
REPRO_UPGRADE='bash ~/up/switch-code.sh "$NEW_REF" && bash ~/up/migrate-env.sh && bash ~/up/preflight.sh "$REPRO_STATE/backup" && bash ~/up/apply.sh'
REPRO_VERIFY='bash ~/up/verify.sh "$REPRO_STATE/backup"'
```

Then `jobs.sh run repro-upgrade PROJECT_REPO=... OLD_REF=... NEW_REF=... TEMPLATE_VMID=<old-OS template> OS_UPDATE=true --follow`.
