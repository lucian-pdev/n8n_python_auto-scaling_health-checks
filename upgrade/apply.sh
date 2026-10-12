#!/bin/bash
# Step 5: switch the running stack to the new version (new code and/or new images) with clean database stops.
# Only after preflight.sh passed. Downtime starts here.
#
# Usage (from the project directory): bash <upgrade-dir>/apply.sh [--no-pull]
#   1. .env.upgraded (if present) becomes .env; the old one is kept as .env.pre-upgrade
#   2. new images are pulled while the old version still runs (shorter downtime)
#   3. db-shutdown.sh stops the stack patiently: apps first, databases last, never killed
#      (Docker's own recreate gives a database only its stop_grace_period, 10 s by default)
#   4. DEPLOY_CMD starts the new version; db-shutdown.sh start brings back anything it didn't recreate
#      and resumes the paused timers
set -euo pipefail
source "$(dirname "$0")/_common.sh"
pull=1; [[ "${1:-}" == --no-pull ]] && pull=0

project="$(running_project)"; [[ -n "$project" ]] || die "the stack isn't running: nothing to apply to (use DEPLOY_CMD for a first install)"

if [[ -f .env.upgraded ]]; then
    cp -p .env .env.pre-upgrade && mv .env.upgraded .env
    ok ".env.upgraded is now .env (previous: .env.pre-upgrade)"
fi
if (( pull )); then
    say "pulling new images (old version still serving)"
    dc pull --ignore-buildable --quiet || warn "some images could not be pulled: DEPLOY_CMD will try again"
fi

say "stopping the stack cleanly (project $project)"
bash "$UPGRADE_DIR/db-shutdown.sh" stop --project "$project" --no-dump \
    || die "the stack did not stop cleanly: see above. Nothing was deployed; db-shutdown.sh start brings it back"

say "starting the new version: $DEPLOY_CMD"
bash -c "$DEPLOY_CMD" || { bad "deploy failed"; die "fix it, or roll back: bash $UPGRADE_DIR/rollback.sh"; }
bash "$UPGRADE_DIR/db-shutdown.sh" start || warn "some stopped containers/timers did not come back: see above"
say "next: bash $UPGRADE_DIR/verify.sh <backup_dir>"
