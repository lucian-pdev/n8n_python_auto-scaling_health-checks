#!/bin/bash
# OS update (on its own, or before an app upgrade in a separate window): bring Ubuntu/Debian packages up to
# date and reboot if needed, without hurting the databases. Only package updates within the current release:
# a release upgrade (do-release-upgrade) is a separate project.
#
# Usage (from the project directory):
#   bash <upgrade-dir>/os-update.sh check    what's pending, reboot needed?, /boot space, blockers. Changes nothing
#   bash <upgrade-dir>/os-update.sh run      records what's running, upgrades non-interactively (keeps your
#                                           config files), cleans old kernels, says whether to reboot
#   sudo reboot                              if it says so
#   bash <upgrade-dir>/os-update.sh after    after the reboot: Docker, every container that ran before, HEALTH_URLS, TIMERS
set -uo pipefail
source "$(dirname "$0")/_common.sh"
STATE="$HOME/.$UPGRADE_NAME-os-update-running.txt"     # containers running before the update
DBSTATE="$HOME/.db-shutdown/stopped.txt"          # written by db-shutdown.sh stop

reboot_needed() {   # Ubuntu flags it in /var/run/reboot-required; Debian may not, so also compare kernels
    [[ -f /var/run/reboot-required ]] && return 0
    local newest; newest="$(ls /boot/vmlinuz-* 2>/dev/null | sort -V | tail -1)"
    [[ -n "$newest" && "$newest" != "/boot/vmlinuz-$(uname -r)" ]]
}

apt_wait() {      # unattended-upgrades may hold the apt lock
    local i
    for ((i = 0; i < 60; i++)); do
        sudo fuser /var/lib/dpkg/lock-frontend /var/lib/apt/lists/lock &>/dev/null || return 0
        ((i == 0)) && say "waiting for another apt process (unattended-upgrades?) to finish"
        sleep 5
    done
    die "apt is still locked after 5 min: check 'ps aux | grep -E \"apt|dpkg\"'"
}

check() {
    say "system"
    . /etc/os-release; ok "$PRETTY_NAME, kernel $(uname -r), $(uptime -p 2>/dev/null || uptime)"
    case "$VERSION_ID" in
        18.04|20.04) warn "$PRETTY_NAME is past standard support: plan a release upgrade separately (not part of this)" ;;
    esac
    apt_wait
    sudo apt-get update -qq
    local n; n="$(apt list --upgradable 2>/dev/null | grep -c upgradable)"
    ok "$n packages to upgrade"
    apt list --upgradable 2>/dev/null | grep -E '^(linux-image|linux-generic|docker-ce|containerd|docker-compose|openssh|systemd|libc6)' \
        | cut -d/ -f1 | sed 's/^/        notable: /'
    apt list --upgradable 2>/dev/null | grep -qE '^(linux-image[^/]*|linux-generic[^/]*|linux-virtual|linux-headers[^/]*|libc6|systemd)/' \
        && warn "kernel / libc / systemd update: a reboot will be needed"
    apt list --upgradable 2>/dev/null | grep -qE '^(docker-ce|containerd)' \
        && warn "Docker update: the Docker daemon restarts during the upgrade, so all containers restart (seconds)"
    reboot_needed && warn "a reboot is already pending from earlier updates (newer kernel installed than running)"
    local held; held="$(apt-mark showhold)"; [[ -n "$held" ]] && warn "held packages (won't upgrade): $held"
    local boot; boot="$(df --output=avail -m /boot 2>/dev/null | tail -1 | tr -d ' ')"
    [[ -n "$boot" && "$boot" -lt 200 ]] && warn "/boot has only ${boot} MB free: run 'sudo apt autoremove --purge' first"
    local root; root="$(df --output=avail -m / | tail -1 | tr -d ' ')"
    (( root < 2048 )) && warn "/ has only ${root} MB free"
    systemctl is-enabled --quiet docker && ok "Docker starts at boot" || bad "Docker is NOT enabled at boot: sudo systemctl enable docker"
    grep -rqs 'Automatic-Reboot "true"' /etc/apt/apt.conf.d/ \
        && warn "unattended-upgrades may reboot on its own: fine, but don't be surprised"
    dc ps --format '{{.Service}} {{.State}}' | awk '$2 != "running" {print "        not running now: " $1}'
}

run() {
    if [[ -s "$DBSTATE" ]]; then      # stack already stopped cleanly by db-shutdown.sh: that's what must come back
        awk '$1 != "timer" {print $2}' "$DBSTATE" | sort -u > "$STATE"
    else
        "${DOCKER[@]}" ps --format '{{.Names}}' | sort > "$STATE"
    fi
    ok "recorded $(wc -l < "$STATE") running containers in $STATE"
    say "upgrading packages (config files you changed are kept)"
    apt_wait
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y \
        -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade || die "apt full-upgrade failed (see above); nothing rebooted"
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y autoremove --purge
    if reboot_needed; then
        warn "reboot required (new kernel or core libraries): $(sort -u /var/run/reboot-required.pkgs 2>/dev/null | tr '\n' ' ')"
        if [[ -s "$DBSTATE" ]]; then say "reboot now: sudo reboot   (then run: os-update.sh after, which starts the stopped containers)"
        else say "reboot now: sudo reboot   (containers come back on their own; then run: os-update.sh after)"; fi
    else
        ok "no reboot needed"
        say "run: os-update.sh after"
    fi
}

after() {
    local fails=0
    if [[ -s "$DBSTATE" ]]; then
        say "starting what db-shutdown.sh stopped (databases first)"
        bash "$(dirname "$0")/db-shutdown.sh" start || fails=1
    fi
    systemctl is-active --quiet docker && ok "Docker running ($(docker --version 2>/dev/null | cut -d, -f1))" || { bad "Docker not running: sudo systemctl start docker"; fails=1; }
    # give containers a moment after boot (databases first, n8n after)
    for _ in $(seq 1 24); do
        [[ -f "$STATE" ]] && comm -23 "$STATE" <("${DOCKER[@]}" ps --format '{{.Names}}' | sort) | grep -q . || break
        sleep 5
    done
    if [[ -f "$STATE" ]]; then
        missing="$(comm -23 "$STATE" <("${DOCKER[@]}" ps --format '{{.Names}}' | sort))"
        if [[ -z "$missing" ]]; then ok "all $(wc -l < "$STATE") containers that ran before are running"
        else bad "not running again: $missing   (docker compose up -d; docker compose logs <name>)"; fails=1; fi
    fi
    for t in ${TIMERS[@]+"${TIMERS[@]}"}; do
        systemctl is-active --quiet "$t" && ok "$t active" || warn "$t not active"
    done
    health_urls || fails=1
    reboot_needed && warn "a reboot is still pending (running kernel $(uname -r) is not the newest installed)"
    ((fails)) && die "fix the above before continuing with the app upgrade"
    say "OS is up to date and the app runs normally"
}

case "${1:-}" in
    check) check ;;
    run)   run ;;
    after) after ;;
    *) sed -n '2,13p' "$0"; exit 1 ;;
esac
