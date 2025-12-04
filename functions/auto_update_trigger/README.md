# Auto Update GCS Trigger (Cloud Functions v2)

Deploy a finalize trigger that calls the xiaozhi-server to publish MQTT auto_update when a per-device `mega.bin` lands in GCS.

Prereqs:
- gcloud configured for your project and region
- Bucket with objects at `device_bin/<mac_enc>/mega.bin`
  - mac_enc format: lowercase MAC with `:` percent-encoded (e.g. `a4%3acf%3a12%3a34%3a56%3a78`)

Deploy:

```bash
gcloud functions deploy auto-update-on-new-mega \
  --gen2 \
  --region=us-central1 \
  --runtime=python311 \
  --source=. \
  --entry-point=on_finalize \
  --trigger-event-filters="type=google.cloud.storage.object.v1.finalized" \
  --trigger-event-filters="bucket=milu-public" \
  --set-env-vars="XIAOZHI_BASE=http://<xiaozhi-host>:8003,MQTT_URL=mqtt://35.188.112.96:1883"
```

Local layout (run deploy from this folder):
- `main.py` (the function)
- `requirements.txt`

Notes:
- Any object not matching `device_bin/<mac_enc>/mega.bin` is ignored.
- The function posts to `/animation/auto_updates` on xiaozhi-server with `{ deviceId, url, broker }`.

