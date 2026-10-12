#!/bin/bash
# Step 6: check the upgraded stack against the backup: same containers running, same data, health URLs,
# timers back. Also fine after an OS update.
#
# Usage (from the project directory): bash <upgrade-dir>/verify.sh <backup_dir>
set -uo pipefail
source "$(dirname "$0")/_common.sh"
bdir="${1:?usage: verify.sh <backup_dir>}"
fails=0
fail() { bad "$*"; fails=$((fails + 1)); }

say "containers (compared with the backup)"
now="$(mktemp)"; trap 'rm -f "$now" /tmp/inventory-after.$$' EXIT
for _ in $(seq 1 24); do       # give slow starters a moment
    dc ps --status running --format '{{.Name}}' | sort > "$now"
    comm -23 "$bdir/running.txt" "$now" | grep -q . || break
    sleep 5
done
missing="$(comm -23 "$bdir/running.txt" "$now" | tr '\n' ' ')"
[[ -z "$missing" ]] && ok "all $(wc -l < "$bdir/running.txt") containers that ran before are running" \
    || fail "not running: $missing (dc ps -a; docker logs <name>)"
new="$(comm -13 "$bdir/running.txt" "$now" | tr '\n' ' ')"; [[ -n "$new" ]] && ok "new in this version: $new"
while read -r c; do      # health checks still "starting" right after a start: wait up to ~3 min
    for _ in $(seq 1 36); do
        h="$(d inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null)"
        [[ "$h" == starting ]] || break
        sleep 5
    done
    [[ -z "$h" || "$h" == healthy ]] || fail "$c is $h"
done < "$now"

say "data (compared with the backup inventory)"
inventory /tmp/inventory-after.$$
if diff -q "$bdir/inventory-before.txt" /tmp/inventory-after.$$ >/dev/null; then
    ok "every database, table row count, view, function and role: identical"
else
    warn "differences (row counts move if the app was used in between):"
    diff "$bdir/inventory-before.txt" /tmp/inventory-after.$$ | sed 's/^/        /'
    grep -q 'unavailable' /tmp/inventory-after.$$ && fail "a database did not answer"
fi

say "health"
health_urls || fails=$((fails + $?))
for t in ${TIMERS[@]+"${TIMERS[@]}"}; do
    systemctl is-active --quiet "$t" && ok "$t active" || fail "$t not active (sudo systemctl start $t)"
done
if declare -f verify_extra >/dev/null; then verify_extra; fi

echo
if (( fails )); then
    printf '\033[1;31m%d check(s) failed: see README "If something fails" / "Roll back".\033[0m\n' "$fails"; exit 1
fi
printf '\033[1;32mUpgrade verified. Check by hand what scripts cannot (log in, a real request).\033[0m\n'
