#!/bin/bash
# Shared helpers for the upgrade scripts. Sourced, not run.
#
# The scripts act on the project directory you run them from (the live install), wherever the scripts
# themselves were extracted. Settings: upgrade.conf next to the scripts.

PROJECT_ROOT="$PWD"
UPGRADE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$PROJECT_ROOT/docker-compose.yml" || -f "$PROJECT_ROOT/compose.yml" ]] \
    || { echo "run this from the project directory (where docker-compose.yml is)" >&2; exit 1; }

UPGRADE_NAME="$(basename "$PROJECT_ROOT")"
COMPOSE_FILES=() DEPLOY_CMD="docker compose up -d --build" ENV_TEMPLATE=env.example ENV_RENAMES=()
TIMERS=() HOST_FILES=() BACKUP_VOLUMES=() DATA_DIRS=() HEALTH_URLS=() PREFLIGHT_ALLOW_CHANGE=()
# shellcheck source=/dev/null
[[ -f "$UPGRADE_DIR/upgrade.conf" ]] && source "$UPGRADE_DIR/upgrade.conf"

say()  { printf '\033[1;34m[upgrade]\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; }
warn() { printf '  \033[1;33mWARN\033[0m  %s\n' "$*"; }
die()  { printf '\033[1;31m[upgrade] %s\033[0m\n' "$*" >&2; exit 1; }

# docker without sudo when the user is in the docker group
DOCKER=(docker); docker info &>/dev/null || DOCKER=(sudo docker)
d()  { "${DOCKER[@]}" "$@"; }
dc() { local f a=(); for f in "${COMPOSE_FILES[@]}"; do [[ -f "$PROJECT_ROOT/$f" ]] && a+=(-f "$f"); done
       d compose ${a[@]+"${a[@]}"} "$@"; }

# config_json <out.json> [env_file]: resolved compose config of the whole stack as JSON
config_json() { dc ${2:+--env-file "$2"} config --format json > "$1"; }

# compose project of the running stack (data volumes are prefixed with it)
running_project() { dc ps --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null | head -1; }

# --- databases ---------------------------------------------------------------------------------
# db_containers: running containers of this project that hold a database -> "<kind> <container>"
#   kind: postgres | mysql | redis | mongo | other
db_containers() {
    local name image base
    dc ps --format '{{.Name}}|{{.Image}}' 2>/dev/null | while IFS='|' read -r name image; do
        base="${image%%@*}"; base="${base##*/}"; base="${base%%:*}"
        case "$base" in
            postgres*|postgis|timescaledb*|pgvector) echo "postgres $name" ;;
            mysql|mariadb|percona*) echo "mysql $name" ;;
            redis*|valkey|keydb|dragonfly) echo "redis $name" ;;
            mongo*) echo "mongo $name" ;;
            clickhouse*|influxdb|couchdb|elasticsearch|opensearch|cassandra|neo4j) echo "other $name" ;;
        esac
    done
}

# psql_super <container> <psql args...>: psql as the image's superuser (POSTGRES_USER, default postgres)
psql_super() { local c="$1"; shift; d exec -i "$c" sh -c 'exec psql -U "${POSTGRES_USER:-postgres}" "$@"' psql "$@"; }

# inventory <file>: what must survive, as plain diffable text. Per Postgres container: every database,
# every table with its exact row count, views, functions, roles. Redis: key count. Plus inventory_extra.
inventory() {
    local kind c db
    {
        while read -r kind c; do
            case "$kind" in
                postgres)
                    for db in $(psql_super "$c" -d postgres -Atc "select datname from pg_database where datallowconn and not datistemplate order by 1" 2>/dev/null); do
                        echo "# $c / $db: tables (exact row counts)"
                        psql_super "$c" -d "$db" -At -F " " -c "
                            select table_schema || '.' || table_name,
                                   (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
                            from information_schema.tables
                            where table_schema not in ('pg_catalog', 'information_schema') and table_type = 'BASE TABLE'
                            order by 1" 2>/dev/null || echo "unavailable"
                        echo "# $c / $db: views, functions"
                        psql_super "$c" -d "$db" -At -c "
                            select 'view ' || table_schema || '.' || table_name from information_schema.views
                                where table_schema not in ('pg_catalog', 'information_schema')
                            union all select 'function ' || n.nspname || '.' || p.proname from pg_proc p
                                join pg_namespace n on n.oid = p.pronamespace where n.nspname not in ('pg_catalog', 'information_schema')
                            order by 1" 2>/dev/null || echo "unavailable"
                    done
                    echo "# $c: roles"
                    psql_super "$c" -d postgres -Atc "select 'role ' || rolname from pg_roles where rolname !~ '^pg_' order by 1" 2>/dev/null || echo "unavailable" ;;
                redis)
                    echo "# $c: keys"
                    redis_cli "$c" DBSIZE 2>/dev/null | sed 's/^/dbsize /' || echo "unavailable" ;;
            esac
        done < <(db_containers)
        if declare -f inventory_extra >/dev/null; then echo "# app"; inventory_extra 2>/dev/null || echo "app inventory unavailable"; fi
    } > "$1"
}

# redis_cli <container> <args...>: password from the container's command line or REDIS_PASSWORD
redis_cli() {
    local c="$1"; shift; local pass
    pass="$(d inspect -f '{{range .Config.Entrypoint}}{{.}} {{end}}{{range .Config.Cmd}}{{.}} {{end}}' "$c" \
        | sed -n 's/.*--requirepass[ =]\([^ "]*\).*/\1/p' | head -1)"
    [[ -n "$pass" ]] || pass="$(d exec "$c" sh -c 'echo "${REDIS_PASSWORD:-}"' 2>/dev/null)"
    if [[ -n "$pass" ]]; then d exec -e REDISCLI_AUTH="$pass" "$c" redis-cli "$@"; else d exec "$c" redis-cli "$@"; fi
}

# health_urls: every HEALTH_URLS entry answers 2xx/3xx (retried ~3 min: apps answer 502 while starting)
health_urls() {
    local u code fails=0
    for u in ${HEALTH_URLS[@]+"${HEALTH_URLS[@]}"}; do
        for _ in $(seq 1 36); do
            code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$u")"
            [[ "$code" =~ ^[23] ]] && break
            sleep 5
        done
        [[ "$code" =~ ^[23] ]] && ok "$u answers ($code)" || { bad "$u -> $code"; fails=$((fails + 1)); }
    done
    return "$fails"
}
