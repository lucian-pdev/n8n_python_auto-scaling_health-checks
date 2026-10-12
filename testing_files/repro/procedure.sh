#!/bin/bash
# Rehearsal driver: the upgrade/README.md commands, unattended. Run by the hooks in testing_files/repro.conf.
#   procedure.sh deploy-old | fetch | os-before | os-after | upgrade | verify
set -euo pipefail
cd "${REPRO_DIR:-$PWD}"
S="${REPRO_STATE:-$HOME/repro-state}"; U="$S/up"; mkdir -p "$S"
# repro.conf is read by repro.sh on the runner, not here: take the n8n versions from the checkout's copy
eval "$(grep -E '^REPRO_N8N_(OLD|NEW)=' testing_files/repro.conf)"
: "${REPRO_N8N_OLD:?missing in testing_files/repro.conf}" "${REPRO_N8N_NEW:?missing in testing_files/repro.conf}"
ref() { git rev-parse -q --verify "origin/$1^{commit}" >/dev/null && echo "origin/$1" || echo "$1"; }
step() { printf '\n\033[1;35m=== %s\033[0m\n' "$*"; }

case "${1:?usage: procedure.sh deploy-old|fetch|os-before|os-after|upgrade|verify}" in
    deploy-old)
        step "deploy the current version with n8n ${REPRO_N8N_OLD}"
        ./env-maker.sh testing_files/env
        sed -i "s/^N8N_VERSION=.*/N8N_VERSION=${REPRO_N8N_OLD}/" .env
        mkdir -p scripts job-output
        bash ./deploy.sh testing_files/env
        sudo docker compose exec -T n8n-main n8n --version ;;
    fetch)
        step "get the upgrade scripts of ${NEW_REF} (code not switched), first backup"
        rm -rf "$U"; mkdir -p "$U"
        git fetch -q origin 2>/dev/null || true
        git archive "$(ref "$NEW_REF")" upgrade | tar -x -C "$U" --strip-components=1
        bash "$U/backup.sh" "$S/backup-os" ;;
    os-before)
        step "OS update: patient stop of the whole host, then packages"
        bash "$U/os-update.sh" check
        bash "$U/db-shutdown.sh" stop
        bash "$U/os-update.sh" run ;;
    os-after)
        step "OS update: after the reboot"
        bash "$U/os-update.sh" after
        bash "$U/verify.sh" "$S/backup-os" ;;
    upgrade)
        step "app upgrade: backup, switch-code, migrate-env, n8n ${REPRO_N8N_OLD} -> ${REPRO_N8N_NEW}, preflight, apply"
        bash "$U/backup.sh" "$S/backup"
        bash "$U/switch-code.sh" "$NEW_REF"
        bash "$U/migrate-env.sh"
        sed -i "s/^N8N_VERSION=.*/N8N_VERSION=${REPRO_N8N_NEW}/" .env.upgraded
        bash "$U/preflight.sh" "$S/backup"
        bash "$U/apply.sh" ;;
    verify)
        step "verify against the backup"
        bash "$U/verify.sh" "$S/backup"
        v="$(sudo docker compose exec -T n8n-main n8n --version | tr -d '\r')"
        [[ "$v" == "$REPRO_N8N_NEW" ]] && echo "  PASS  n8n is now $v" || { echo "  FAIL  n8n is $v, expected $REPRO_N8N_NEW"; exit 1; } ;;
esac
