#!/bin/bash
# Roll back code + config to what backup.sh recorded, keeping the data volumes as they are now.
# Restoring DATA is a separate, manual decision (README "Roll back"): only when the new version damaged it.
#
# Usage (from the project directory): bash <upgrade-dir>/rollback.sh <backup_dir>
set -euo pipefail
source "$(dirname "$0")/_common.sh"
bdir="${1:?usage: rollback.sh <backup_dir>}"
[[ -f "$bdir/git-commit.txt" && -f "$bdir/env.backup" ]] || die "$bdir has no git-commit.txt / env.backup"

project="$(running_project)"
if [[ -n "$project" ]]; then
    say "stopping the new version cleanly"
    bash "$UPGRADE_DIR/db-shutdown.sh" stop --project "$project" --no-dump || die "did not stop cleanly: see above"
fi
git -c user.name=upgrade -c user.email=upgrade@localhost stash push -m "rollback $(date +%F)" >/dev/null 2>&1 || true
git checkout --quiet "$(cat "$bdir/git-commit.txt")"
ok "code back at $(git rev-parse --short HEAD) (detached; git checkout <branch> later)"
[[ -f .env ]] && cp -p .env .env.rolled-back
cp -p "$bdir/env.backup" .env && ok ".env restored (the new one kept as .env.rolled-back)"
[[ -s "$bdir/local-changes.patch" ]] && { git apply "$bdir/local-changes.patch" && ok "local edits re-applied" || warn "local edits did not apply cleanly: $bdir/local-changes.patch"; }

say "starting the old version: $DEPLOY_CMD"
bash -c "$DEPLOY_CMD"
bash "$UPGRADE_DIR/db-shutdown.sh" start || true
say "now: bash $UPGRADE_DIR/verify.sh $bdir"
