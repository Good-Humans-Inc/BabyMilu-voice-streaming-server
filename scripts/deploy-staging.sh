#!/bin/bash
set -e

VM="your-staging-vm-name"

gcloud compute ssh $VM --command "
  cd /srv/staging/current &&
  sudo systemctl stop bm-staging-compose.service &&
  git fetch origin &&
  git reset --hard origin/staging &&
  sudo systemctl start bm-staging-compose.service
"
