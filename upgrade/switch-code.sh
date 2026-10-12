#!/bin/bash
# Step 2 (code upgrades only; skip it for an image-only update): switch the checkout to the new version
# without losing the live .env or local edits. Running containers keep running; nothing is restarted.
#
# Usage (from the project directory): bash <upgrade-dir>/switch-code.sh <branch|tag>
set -euo pipefail
source "$(dirname "$0")/_common.sh"
ref="${1:?usage: switch-code.sh <branch|tag>}"
[[ -f .env ]] || die "no .env here: are you in the live project directory?"

# 1. Timers that run `docker compose up` would start the NEW code with the OLD config halfway through
for t in ${TIMERS[@]+"${TIMERS[@]}"}; do
    sudo systemctl stop "$t" 2>/dev/null && ok "paused $t (resumed by verify / os-update after)" || true
done

# 2. A tracked .env would be replaced by the checkout: keep the live one aside (untracked)
[[ -e .env.live ]] || cp -p .env .env.live
cmp -s .env .env.live || die ".env.live exists and differs from .env: check which one is the live config"
ok "live config kept as .env.live"

# 3. Local edits to tracked files block the checkout: stash them (also in the backup as local-changes.patch)
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git -c user.name=upgrade -c user.email=upgrade@localhost stash push -m "pre-upgrade local edits $(date +%F)" >/dev/null
    ok "local edits stashed (git stash list)"
fi

# 4. Switch
git fetch --all --tags --quiet || echo "  (some remotes could not be fetched; using what is already fetched)"
if git show-ref --verify --quiet "refs/heads/$ref" || git show-ref --verify --quiet "refs/tags/$ref"; then
    git checkout --quiet "$ref"
else      # first remote that has it (avoids "matched multiple remotes")
    r="$(git for-each-ref --format='%(refname:short)' "refs/remotes/*/$ref" | head -1)"
    [[ -n "$r" ]] || die "no branch or tag named $ref (git branch -a; git tag)"
    git checkout --quiet -b "$ref" --track "$r"
fi
[[ -f .env ]] || cp -p .env.live .env       # the old version tracked .env and the new one doesn't
ok "code is now $ref ($(git rev-parse --short HEAD)); containers untouched"
say "next: bash $(dirname "$0")/migrate-env.sh"
