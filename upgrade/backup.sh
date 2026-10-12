#!/bin/bash
# Step 1: back up everything an upgrade could touch, while the CURRENT version is running. Changes nothing.
#
# Usage (from the project directory): bash <upgrade-dir>/backup.sh [backup_dir]   (default ~/<name>-backup-<date>)
set -euo pipefail
source "$(dirname "$0")/_common.sh"

OUT="${1:-$HOME/$UPGRADE_NAME-backup-$(date +%Y%m%d-%H%M)}"
mkdir -p "$OUT" && chmod 700 "$OUT"      # holds secrets
say "backing up to $OUT"
[[ -n "$(dc ps --status running -q)" ]] || die "nothing of this stack is running here: run this in the live project, before upgrading"

# 1. config + code state
[[ -f .env ]] && cp .env "$OUT/env.backup"
if git rev-parse --git-dir &>/dev/null; then
    git rev-parse HEAD > "$OUT/git-commit.txt"
    git status --porcelain > "$OUT/git-status.txt"
    git diff > "$OUT/local-changes.patch"
fi
dc config > "$OUT/compose-config-old.yml"
config_json "$OUT/compose-config-old.json"
dc ps > "$OUT/containers.txt"
dc ps --status running --format '{{.Name}}' | sort > "$OUT/running.txt"
project="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' < "$OUT/compose-config-old.json")"
echo "$project" > "$OUT/compose-project.txt"
d volume ls --format '{{.Name}}' | grep "^${project}_" > "$OUT/volumes.txt" || true
ok "config, code state, compose project '$project', $(wc -l < "$OUT/running.txt") running containers"

# 2. databases: consistent dumps (a raw copy of a running database's files is not consistent)
while read -r kind c; do
    case "$kind" in
        postgres)
            d exec "$c" sh -c 'pg_dumpall -U "${POSTGRES_USER:-postgres}"' | gzip > "$OUT/db-$c.sql.gz"
            ok "$c: pg_dumpall (data + roles) -> db-$c.sql.gz ($(du -h "$OUT/db-$c.sql.gz" | cut -f1))" ;;
        mysql)
            d exec "$c" sh -c 'exec mysqldump -uroot -p"${MYSQL_ROOT_PASSWORD:-$MARIADB_ROOT_PASSWORD}" --all-databases --single-transaction --routines --events' \
                | gzip > "$OUT/db-$c.sql.gz"
            ok "$c: mysqldump --all-databases -> db-$c.sql.gz" ;;
        redis)
            redis_cli "$c" SAVE >/dev/null && d cp "$c:/data/dump.rdb" "$OUT/db-$c.rdb" 2>/dev/null \
                && ok "$c: SAVE -> db-$c.rdb" || warn "$c: no RDB snapshot copied (append-only only?); its volume stays untouched" ;;
        mongo)
            d exec "$c" sh -c 'mongodump --archive --gzip ${MONGO_INITDB_ROOT_USERNAME:+-u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin}' \
                > "$OUT/db-$c.archive.gz" && ok "$c: mongodump -> db-$c.archive.gz" ;;
        other)
            warn "$c: no dump method known for this database: snapshot the VM or add it to backup_extra" ;;
    esac
done < <(db_containers)

# 3. app-level exports
if declare -f backup_extra >/dev/null; then backup_extra "$OUT" && ok "backup_extra (app exports)"; fi

# 4. volumes that aren't databases
for v in ${BACKUP_VOLUMES[@]+"${BACKUP_VOLUMES[@]}"}; do
    d run --rm -v "${project}_$v:/v:ro" -v "$OUT:/b" alpine tar czf "/b/volume-$v.tgz" -C /v .
    ok "volume $v -> volume-$v.tgz"
done

# 5. project files git doesn't track (local configs, synced scripts), without the data folders
if git rev-parse --git-dir &>/dev/null; then
    re='^(\.git/'; for x in ${DATA_DIRS[@]+"${DATA_DIRS[@]}"}; do re+="|${x%/}/"; done; re+=')'
    git ls-files --others --exclude-standard -z | grep -zvE "$re" | tar czf "$OUT/untracked-files.tgz" --null -T - 2>/dev/null || true
    ok "untracked project files -> untracked-files.tgz ($(tar tzf "$OUT/untracked-files.tgz" 2>/dev/null | grep -vc '/$') files)"
fi

# 6. host config the current version uses (for a rollback)
if (( ${#HOST_FILES[@]} )); then
    sudo tar czf "$OUT/host-config.tgz" --ignore-failed-read "${HOST_FILES[@]}" 2>/dev/null || true
    sudo chown "$(id -u):$(id -g)" "$OUT/host-config.tgz"
    ok "host files -> host-config.tgz"
fi

# 7. what must still be there afterwards
inventory "$OUT/inventory-before.txt"
ok "inventory -> inventory-before.txt ($(grep -vc '^#' "$OUT/inventory-before.txt") lines)"

( cd "$OUT" && sha256sum -- * > SHA256SUMS )
say "done. Copy $OUT off this server before continuing, e.g.:"
echo "    scp -r $(whoami)@$(hostname -I 2>/dev/null | awk '{print $1}'):$OUT ."
