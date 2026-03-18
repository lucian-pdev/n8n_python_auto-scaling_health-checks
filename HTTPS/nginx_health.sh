#!/bin/bash

# /usr/local/bin/nginx_health.sh

if ! systemctl is-active --quiet nginx; then
    echo "$(date): nginx down, attempting restart" >> /var/log/nginx_health.log
    systemctl restart nginx
    
    # Verify restart worked
    sleep 2
    if ! systemctl is-active --quiet nginx; then
        echo "$(date): nginx failed to restart" >> /var/log/nginx_health.log
        # Optional: alert
    fi
fi

# Also test nginx actually responds
if ! curl -sf http://localhost:80/healthz >/dev/null 2>&1; then
    echo "$(date): nginx not responding, reloading" >> /var/log/nginx_health.log
    systemctl reload nginx
fi