#!/bin/bash
# Patient, clean shutdown of the containers on this host before an OS upgrade, a reboot or a
# redeploy: proxies first, then apps, then workers, databases last. Nothing is ever killed:
# each container gets its own stop signal, up to 4 times, 3 minutes apart. A database that
# still hangs is rescued (logs, processes, raw copy of its data) and flagged until you press a key.
#
# Usage:
#   db-shutdown.sh status [filters]            show what would be stopped, in which order (changes nothing)
#   db-shutdown.sh stop   [filters] [--no-dump]  dump every Postgres + save Redis, then stop everything
#   db-shutdown.sh start                       start what 'stop' stopped: databases first, then apps
# Filters:
#   --project <compose project>   only that project (repeatable); default: every running container
#   --exclude <container>         leave this one running (repeatable)
#
# Containers stopped this way do NOT come back by themselves after a reboot (Docker treats them as
# stopped by you): run 'start', or os-update.sh after (which runs it for you).
# Standalone: works on any Docker host, with or without the rest of the upgrade scripts.
set -uo pipefail

TRIES="${STOP_TRIES:-4}"     # stop calls per container
WAIT="${STOP_WAIT:-180}"     # seconds between them
STATE_DIR="$HOME/.db-shutdown"
STATE="$STATE_DIR/stopped.txt"          # "<tier> <container>" in stop order, "timer <unit>" for paused timers
# timers that run `docker compose up` or restart services: they would bring stopped containers back.
# From upgrade.conf (TIMERS) when it sits next to this script; STOP_TIMERS="a.timer b.timer" overrides.
TIMERS=()
[[ -f "$(dirname "$0")/upgrade.conf" ]] && TIMERS=($(bash -c 'source "$1" >/dev/null 2>&1; echo "${TIMERS[@]:-}"' _ "$(dirname "$0")/upgrade.conf"))
[[ -n "${STOP_TIMERS:-}" ]] && TIMERS=($STOP_TIMERS)

say()   { printf '\033[1;34m[shutdown]\033[0m %s\n' "$*"; }
ok()    { printf '  \033[1;32mOK\033[0m    %s\n' "$*"; }
warn()  { printf '  \033[1;33mWARN\033[0m  %s\n' "$*"; }
bad()   { printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; }
D=(docker); docker info &>/dev/null || D=(sudo docker)
d() { "${D[@]}" "$@"; }

# --- classification --------------------------------------------------------------------------
# tier: 1 proxy (stop first: no new traffic) | 2 app | 3 worker/cron | 4 database (stop last)
DB_RE='^(postgres|postgresql|postgis|timescaledb.*|pgvector|mysql|mariadb|percona.*|mongo|mongodb.*|redis|redis-stack.*|valkey|keydb|dragonfly|clickhouse.*|cassandra|scylla|elasticsearch|opensearch|influxdb|couchdb|couchbase.*|neo4j|questdb|cockroach|prometheus|victoria-metrics|memcached)$'
classify() {      # classify <image> <name> -> "tier kind"   kind: postgres | redis | db | app
    local image="$1" name="$2" base
    base="${image%%@*}"; base="${base##*/}"; base="${base%%:*}"
    if [[ "$image" == *openproject/openproject* && "$image" != *slim* ]]; then echo "4 postgres-aio"; return; fi  # all-in-one: Postgres inside
    if [[ "$base" =~ $DB_RE ]]; then
        case "$base" in
            postgres*|postgis|timescaledb*|pgvector) echo "4 postgres" ;;
            redis*|valkey|keydb|dragonfly) echo "4 redis" ;;
            *) echo "4 db" ;;
        esac; return
    fi
    case "$base $name" in
        *traefik*|*nginx*|*caddy*|*haproxy*|*proxy*) echo "1 app" ;;
        *worker*|*cron*|*scheduler*) echo "3 app" ;;
        *) echo "2 app" ;;
    esac
}

projects=() excludes=() dump=1      # --project - : containers outside any compose project
parse_filters() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --project) projects+=("$2"); shift ;;
            --exclude) excludes+=("$2"); shift ;;
            --no-dump) dump=0 ;;
            *) sed -n '2,18p' "$0"; exit 1 ;;
        esac
        shift
    done
}

plan() {          # running containers -> "tier kind name image project", stop order
    d ps --format '{{.Names}}|{{.Image}}|{{.Label "com.docker.compose.project"}}' | while IFS='|' read -r name image proj; do
        proj="${proj:--}"
        [[ ${#projects[@]} -gt 0 && ! " ${projects[*]} " =~ " $proj " ]] && continue
        [[ " ${excludes[*]:-} " =~ " $name " ]] && continue
        echo "$(classify "$image" "$name") $name $image $proj"
    done | sort -k1,1n -k3,3
}

redis_cli() {     # redis_cli <name> <args...>: password from the container's configured command
    local name="$1"; shift; local pass    # (redis-server rewrites its own process title, so not from ps)
    pass="$(d inspect -f '{{range .Config.Entrypoint}}{{.}} {{end}}{{range .Config.Cmd}}{{.}} {{end}}' "$name" \
        | sed -n 's/.*--requirepass[ =]\([^ "]*\).*/\1/p' | head -1)"
    [[ -n "$pass" ]] || pass="$(d exec "$name" sh -c 'echo "${REDIS_PASSWORD:-}"' 2>/dev/null)"
    if [[ -n "$pass" ]]; then d exec -e REDISCLI_AUTH="$pass" "$name" redis-cli "$@"; else d exec "$name" redis-cli "$@"; fi
}

# --- stopping without killing -----------------------------------------------------------------
running() { [[ "$(d inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == true ]]; }

patient_stop() {  # patient_stop <name> [kind]: 0 stopped, 1 still running after TRIES x WAIT
    local name="$1" kind="${2:-app}" sig try t
    sig="$(d inspect -f '{{.Config.StopSignal}}' "$name")"; sig="${sig:-SIGTERM}"
    for ((try = 1; try <= TRIES; try++)); do
        # The signal also marks the container as stopped by you, so its restart policy won't bring it back
        d kill --signal="$sig" "$name" &>/dev/null
        # Redis: a shell wrapper as PID 1 may swallow the signal; its own SHUTDOWN SAVE always works
        # (must come after the signal: on its own, Docker would treat the exit as a crash and restart it)
        if [[ "$kind" == redis ]]; then sleep 1; running "$name" && redis_cli "$name" SHUTDOWN SAVE &>/dev/null; fi
        for ((t = 0; t < WAIT; t += 5)); do
            running "$name" || return 0
            (( t > 0 && t % 60 == 0 )) && printf '        %s still shutting down (call %d/%d, %ds)\n' "$name" "$try" "$TRIES" "$t"
            sleep 5
        done
        warn "$name did not stop within ${WAIT}s after $sig (call $try/$TRIES)"
    done
    return 1
}

clean_exit() {    # clean_exit <name> <kind>: exit code 0 and the engine's own "shut down cleanly" line
    local name="$1" kind="$2" code logs
    code="$(d inspect -f '{{.State.ExitCode}}' "$name")"
    logs="$(d logs --tail 50 "$name" 2>&1)"
    case "$kind" in
        postgres*) grep -q 'database system is shut down' <<< "$logs" || { echo "no 'database system is shut down' in its log (exit $code)"; return 1; } ;;
        redis)     grep -qiE 'ready to exit|bye bye' <<< "$logs" || { echo "no 'ready to exit' in its log (exit $code)"; return 1; } ;;
    esac
    [[ "$code" == 0 || "$kind" == app && "$code" =~ ^(0|143|130)$ ]] || { echo "exit code $code"; return 1; }
}

# --- saving ----------------------------------------------------------------------------------
pg() {            # pg <name> <kind> <sql/cmd...>: run as the database superuser inside the container
    local name="$1" kind="$2"; shift 2
    if [[ "$kind" == postgres-aio ]]; then d exec "$name" su postgres -c "$*"
    else d exec "$name" sh -c "PGUSER=\"\${POSTGRES_USER:-postgres}\" $*"; fi
}

save_db() {       # save_db <name> <kind> <outdir>
    local name="$1" kind="$2" out="$3"
    case "$kind" in
        postgres*)
            if pg "$name" "$kind" pg_dumpall 2>"$out/$name.dump.err" | gzip > "$out/$name.sql.gz" && [[ ${PIPESTATUS[0]} -eq 0 ]]; then
                ok "$name: full dump -> $out/$name.sql.gz ($(du -h "$out/$name.sql.gz" | cut -f1))"; rm -f "$out/$name.dump.err"
            else warn "$name: dump failed (see $out/$name.dump.err); the shutdown itself still saves its data"; fi
            pg "$name" "$kind" "psql -d postgres -qc CHECKPOINT" &>/dev/null && ok "$name: checkpoint written (faster, cleaner shutdown)" ;;
        redis)
            if redis_cli "$name" SAVE 2>/dev/null | grep -q OK; then ok "$name: snapshot saved (SAVE)"
            else warn "$name: SAVE did not answer OK; shutdown still flushes its append-only file"; fi ;;
    esac
}

rescue() {        # rescue <name> <kind> <outdir>: grab what we can from a hung database, leave it running
    local name="$1" kind="$2" out="$3/rescue-$1"
    mkdir -p "$out"
    d logs --tail 1000 "$name" > "$out/container.log" 2>&1
    d top "$name" > "$out/processes.txt" 2>&1
    d inspect "$name" > "$out/inspect.json" 2>&1
    [[ "$kind" == postgres* ]] && pg "$name" "$kind" "psql -d postgres -Atc \"select pid, state, wait_event, now()-query_start, left(query,200) from pg_stat_activity\"" > "$out/pg_stat_activity.txt" 2>&1
    d inspect -f '{{range .Mounts}}{{.Type}}|{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}|{{.Destination}}{{"\n"}}{{end}}' "$name" \
        | while IFS='|' read -r type src dst; do
            [[ -z "$src" || "$type" == tmpfs ]] && continue
            d run --rm -v "$src:/v:ro" -v "$out:/out" alpine tar czf "/out/raw-$(tr '/' '_' <<< "$dst").tgz" -C /v . 2>/dev/null \
                && echo "raw copy of $dst ($src)" >> "$out/README.txt"
        done
    echo "$out"
}

alarm() {         # alarm <message lines...>: flag until a key is pressed, then release the terminal
    local key
    if [[ ! -t 0 ]]; then      # no terminal (cron, CI): print once, the exit code carries the failure
        printf '\033[1;41;37m SHUTDOWN PROBLEM \033[0m\n'; printf '\033[1;31m  %s\033[0m\n' "$@"; return
    fi
    while :; do
        printf '\a\n\033[1;41;37m %s \033[0m\n' "SHUTDOWN PROBLEM: press any key to acknowledge"
        printf '\033[1;31m  %s\033[0m\n' "$@"
        read -rsn1 -t 10 key && break
    done
    printf '\n\033[1;33mAcknowledged. Summary:\033[0m\n'; printf '  %s\n' "$@"
}

# --- commands --------------------------------------------------------------------------------
cmd_status() {
    say "would stop, in this order (tier 1 proxies -> 2 apps -> 3 workers -> 4 databases):"
    plan | awk '{printf "  %s %-14s %-28s %s  (project %s)\n", $1, $2, $3, $4, $5}'
}

cmd_stop() {
    mkdir -p "$STATE_DIR"
    [[ -s "$STATE" ]] && { warn "a previous stop was not followed by 'start' ($STATE): merging"; }
    local out="$HOME/db-shutdown-$(date +%Y%m%d-%H%M)"; mkdir -p "$out" && chmod 700 "$out"
    local issues=() app_issues=() tier kind name image proj why
    mapfile -t rows < <(plan)
    [[ ${#rows[@]} -gt 0 ]] || { say "nothing running (with these filters)"; return 0; }
    cmd_status
    say "saving to $out; this can take long, that's fine"
    for t in "${TIMERS[@]}"; do               # first: nothing may restart what we stop
        if systemctl is-active --quiet "$t" 2>/dev/null; then
            sudo systemctl stop "$t" && echo "timer $t" >> "$STATE" && ok "paused $t (it runs docker compose / restarts services)"
        fi
    done

    for row in "${rows[@]}"; do             # apps: tiers 1-3, in order
        read -r tier kind name image proj <<< "$row"; [[ "$tier" == 4 ]] && continue
        say "stopping $name"
        echo "$tier $name" >> "$STATE"
        if patient_stop "$name"; then
            why="$(clean_exit "$name" app)" && ok "$name stopped" || warn "$name stopped, $why (apps hold no data: not flagged)"
        else
            app_issues+=("$name (app) ignored $TRIES stop signals: forced to stop so the databases can close cleanly")
            d stop -t 30 "$name" &>/dev/null; warn "$name forced to stop"
        fi
    done

    if (( dump )); then                     # apps are down: dumps hold the final state
        for row in "${rows[@]}"; do read -r tier kind name image proj <<< "$row"; [[ "$tier" == 4 ]] && save_db "$name" "$kind" "$out"; done
    fi

    for row in "${rows[@]}"; do             # databases
        read -r tier kind name image proj <<< "$row"; [[ "$tier" == 4 ]] || continue
        say "stopping database $name ($kind)"
        echo "$tier $name" >> "$STATE"
        if patient_stop "$name" "$kind"; then
            why="$(clean_exit "$name" "$kind")" && ok "$name shut down cleanly" \
                || { bad "$name stopped, but $why"; issues+=("$name ($kind) stopped UNCLEANLY: $why. It will run crash recovery on next start; logs: docker logs $name"); }
        else
            bad "$name still running after $TRIES stop signals, $((TRIES * WAIT / 60)) minutes: rescuing"
            local r; r="$(rescue "$name" "$kind" "$out")"
            issues+=("$name ($kind) HUNG: still running after $TRIES stop signals ${WAIT}s apart. Left running, not killed."
                     "   rescued to $r (logs, processes, pg_stat_activity, raw data copy)$([[ -f "$out/$name.sql.gz" ]] && echo "; dump from before the stop: $out/$name.sql.gz")"
                     "   to force it: docker kill $name (it will run crash recovery on next start)")
        fi
    done

    echo
    # databases acting up: alarm until acknowledged, exit 2. Apps (no data): a warning, not a stop.
    if (( ${#app_issues[@]} )); then printf '\033[1;33m[shutdown] apps that needed forcing (no data lost):\033[0m\n'; printf '  %s\n' "${app_issues[@]}"; fi
    if (( ${#issues[@]} )); then alarm "${issues[@]}"; return 2; fi
    say "everything stopped cleanly. Dumps/logs: $out"
    say "bring it back with: $0 start   (needed after a reboot too)"
}

cmd_start() {
    [[ -s "$STATE" ]] || { say "nothing recorded to start ($STATE)"; return 0; }
    local tier name t h failed=0
    say "starting databases first, then apps, workers, and proxies last (traffic arrives when everything is ready)"
    for tier in 4 2 3 1; do
        while read -r ti name; do
            [[ "$ti" == "$tier" ]] || continue
            d inspect "$name" &>/dev/null || { warn "$name no longer exists (recreated by a deploy?): skipped"; continue; }
            running "$name" && { ok "$name already running"; continue; }
            d start "$name" >/dev/null || { bad "$name failed to start"; failed=1; continue; }
            for ((t = 0; t < 300; t += 5)); do     # wait for healthy (or just running if it has no healthcheck)
                h="$(d inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name")"
                running "$name" && [[ "$h" == healthy || "$h" == none ]] && break
                sleep 5
            done
            running "$name" && ok "$name running${h:+ ($h)}" || { bad "$name not running: docker logs $name"; failed=1; }
        done < <(sort -u "$STATE")
    done
    for t in $(awk '$1 == "timer" {print $2}' "$STATE" | sort -u); do     # last: timers resume
        if [[ -z "$(systemctl list-unit-files "$t" --no-legend 2>/dev/null)" ]]; then warn "$t no longer exists (removed by an upgrade?): skipped"; continue; fi
        sudo systemctl start "$t" && ok "resumed $t" || { bad "$t failed to resume"; failed=1; }
    done
    (( failed )) && return 1
    rm -f "$STATE"; say "all started"
}

case "${1:-}" in
    status) shift; parse_filters "$@"; cmd_status ;;
    stop)   shift; parse_filters "$@"; cmd_stop ;;
    start)  cmd_start ;;
    *) sed -n '2,18p' "$0"; exit 1 ;;
esac
