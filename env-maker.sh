#!/bin/bash

# Spawn an .env file from a template (default: ./env).
# Every replace_<tag> left in the template gets a fresh random value; anything
# you set by hand is copied as-is.
set -euo pipefail

# tags to use in templates: replace_ssl, replace_password, replace_user,
#                           replace_port, replace_domain, replace_email
TAGS=(ssl password pass user port domain email)   # "password" before its prefix "pass"

template="${1:-./env}"
outfile=".env"

if [[ -f "$outfile" && "${2:-}" != "--force" ]]; then
    echo "ERROR: $outfile already exists and holds N8N_ENCRYPTION_KEY."
    echo "Regenerating it makes every saved n8n credential unreadable."
    echo "Back it up first, then rerun: $0 $template --force"
    exit 1
fi

gen() {
    case "$1" in
        ssl)           openssl rand -hex 32 ;;
        password|pass) openssl rand -hex 18 ;;
        user)          echo "user_$(openssl rand -hex 4)" ;;
        port)          shuf -i 10000-65535 -n 1 ;;
        domain)        echo "n8n-$(openssl rand -hex 4).localhost" ;;   # never resolves publicly; LE refuses it
        email)         echo "admin@example.com" ;;
    esac
}

randomized=()
umask 077    # .env holds secrets: create it as 0600

while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*# ]]; then    # comments may mention tags; copy them untouched
        printf '%s\n' "$line"
        continue
    fi
    for tag in "${TAGS[@]}"; do
        # one value per occurrence, so two tags on a line get different values
        while [[ "$line" == *"replace_$tag"* ]]; do
            line="${line/replace_$tag/$(gen "$tag")}"
            [[ "$tag" == domain || "$tag" == email ]] && randomized+=("${line%%=*}")
        done
    done
    printf '%s\n' "$line"
done < "$template" > "$outfile"

echo "Wrote $outfile"

if (( ${#randomized[@]} )); then
    echo "WARNING: random value used for: ${randomized[*]}"
    echo "         Fine for testing; Traefik will serve a self-signed cert."
    echo "         For a real certificate set DOMAIN and EMAIL in $template and rerun with --force."
fi
