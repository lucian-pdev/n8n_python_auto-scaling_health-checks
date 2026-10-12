#!/bin/bash

sudo ufw default deny incoming
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP → HTTPS redirect
sudo ufw allow 443/tcp     # HTTPS
sudo ufw --force enable    # --force: no "may disrupt ssh" prompt (22 is allowed above)
