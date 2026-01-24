#!/bin/bash

# Test command for generate-device-bin-miao Cloud Function
# Minimal required fields: deviceId, source_folder_url, destination_folder_url

URL="https://us-central1-composed-augury-469200-g6.cloudfunctions.net/generate-device-bin-miao"

curl -sS -X POST "$URL" \
  -H "Content-Type: application/json" \
  --data-raw '{
    "deviceId": "90:e5:b1:a9:57:60",
    "source_folder_url": "https://storage.googleapis.com/milu-public/users/+11111111111/characters/ch_3b4d5cd8d07746f3/",
    "destination_folder_url": "gs://milu-public/device_bin/"
  }' | jq .

# Extract just the publish info:
# curl ... | jq .publish
