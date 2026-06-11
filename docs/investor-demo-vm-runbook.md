# Investor Demo VM Runbook

This runbook is for a demo-only BabyMilu voice VM that can host a small number
of devices for investor meetings while using staging Firestore and staging
Supabase.

Existing live cloud resources should not be restarted, deleted, or granted new
access until the operator gives a final explicit confirmation.

## Implemented Isolated Resources

- Project: `composed-augury-469200-g6`
- Zone: `us-central1-a`
- VM: `bm-investor-demo-vm`
- External IP: `34.46.57.140`
- Network tag: `bm-investor-demo`
- Service account: `bm-investor-demo-vm-sa@composed-augury-469200-g6.iam.gserviceaccount.com`
- Firewall rule: `bm-investor-demo-voice-ingress`
- Firewall source range: `0.0.0.0/0` for roadshow device access
- Public demo ports: `80`, `443`, `1883`, `8000`, `8003`
- Deploy root: `/srv/demo/current`
- Runtime env file: `/srv/demo/secrets/server.env`
- Runtime config override: `/srv/demo/current/data/.config.yaml`
- Systemd unit: `bm-investor-demo-compose.service`
- Containers: `bm-investor-demo-server`, `bm-investor-demo-mqtt`
- Deployed revision file: `/srv/demo/DEPLOYED_REVISION`

## Important Checkout Note

Do not deploy from a dirty local checkout. The current workspace may contain
large local deletions while `main/babymilu-server` is now the repo's canonical
production runtime. If the demo must run `main/xiaozhi-server`, first deploy a
pinned commit or branch where that tree is complete and importable.

Recommended deployment invariant:

- record the exact Git commit SHA in `/srv/demo/DEPLOYED_REVISION`
- keep one previous release at `/srv/demo/releases/<previous-sha>`
- symlink `/srv/demo/current` to the active release

## One-Time Cloud Creation

Run only after final confirmation.

```bash
PROJECT=composed-augury-469200-g6
ZONE=us-central1-a
VM=bm-investor-demo-vm
TAG=bm-investor-demo
SA=bm-investor-demo-vm-sa

gcloud config set project "$PROJECT"

gcloud iam service-accounts create "$SA" \
  --display-name="BabyMilu investor demo VM"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/datastore.user"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter"

gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/monitoring.metricWriter"

gcloud compute firewall-rules create bm-investor-demo-voice-ingress \
  --network=default \
  --target-tags="$TAG" \
  --allow=tcp:80,tcp:443,tcp:1883,tcp:8000,tcp:8003 \
  --source-ranges=0.0.0.0/0 \
  --description="Investor demo voice, OTA, and MQTT ingress"

gcloud compute instances create "$VM" \
  --zone="$ZONE" \
  --machine-type=e2-standard-4 \
  --boot-disk-size=80GB \
  --boot-disk-type=pd-balanced \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --tags="$TAG" \
  --service-account="${SA}@${PROJECT}.iam.gserviceaccount.com" \
  --scopes=https://www.googleapis.com/auth/cloud-platform \
  --metadata=enable-oslogin=TRUE
```

## VM Bootstrap

```bash
gcloud compute ssh bm-investor-demo-vm --zone=us-central1-a --command='
set -euo pipefail
sudo apt-get update
sudo apt-get install -y ca-certificates curl git jq docker.io docker-compose
sudo systemctl enable --now docker
sudo mkdir -p /srv/demo/releases /srv/demo/secrets
sudo chown -R "$USER":"$USER" /srv/demo
'
```

Create `/srv/demo/secrets/server.env` on the VM with staging-only values:

```bash
GOOGLE_CLOUD_PROJECT=composed-augury-469200-g6
CHAT_STORE_BACKEND=supabase
SUPABASE_URL=https://<staging-project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<staging-service-role-key>
SUPABASE_TIMEOUT_SECONDS=5
SUPABASE_MAX_RETRIES=1
SUPABASE_RETRY_BACKOFF_SECONDS=0.25
FISH_AUDIO_API_KEY=<demo-or-staging-fish-key>
OPENAI_API_KEY=<staging-or-demo-openai-key>
```

Keep service-role keys server-side only. Do not put them into app, firmware, or
public web config.

## Runtime Config Override

Use `/srv/demo/current/data/.config.yaml` for demo-only overrides. Keep it
minimal so it does not fork the product defaults.

```yaml
log:
  log_level: INFO

firestore:
  project_id: composed-augury-469200-g6
  devices_collection: devices

TTS:
  FishAudio:
    model: s2-pro
    latency: balanced
    sample_rate: 16000
    chunk_length: 100
    connect_timeout_seconds: 8
    total_timeout_seconds: 60
```

## Deploy Pattern

```bash
REV=<pinned-git-sha>
REPO=https://github.com/<org>/BabyMilu-voice-streaming-server.git

gcloud compute ssh bm-investor-demo-vm --zone=us-central1-a --command="
set -euo pipefail
cd /srv/demo/releases
git clone --depth 1 '$REPO' '$REV'
cd '$REV'
git fetch --depth 1 origin '$REV'
git checkout '$REV'
mkdir -p data
ln -sfn /srv/demo/secrets/server.env .env
ln -sfn /srv/demo/releases/'$REV' /srv/demo/current
echo '$REV' > /srv/demo/DEPLOYED_REVISION
"
```

If running the legacy `main/xiaozhi-server`, use the repo's known-good Docker
or compose entrypoint from the pinned revision. If running the canonical
`main/babymilu-server`, build with `Dockerfile-babymilu-server`.

## Health And Smoke

Basic health:

```bash
curl -fsS http://<demo-vm-ip>:8000/health || curl -fsS http://<demo-vm-ip>:8000/
```

Shared smoke harness using environment variables:

```bash
export BABYMILU_SMOKE_PROJECT=composed-augury-469200-g6
export BABYMILU_SMOKE_ENVIRONMENT_TYPE=external-dev
export BABYMILU_SMOKE_DATA_MODE=live-shape
export BABYMILU_SMOKE_SCHEDULER_TRIGGER=manual
export BABYMILU_SMOKE_WS_URL=ws://<demo-vm-ip>:8000/xiaozhi/v1/
export BABYMILU_SMOKE_MQTT_HOST=<demo-vm-ip>
export BABYMILU_SMOKE_NOTES="investor-demo"

/Users/yan/Desktop/BabyMilu/.venv/bin/python tools/smoke/run.py preflight --env investor-demo
/Users/yan/Desktop/BabyMilu/.venv/bin/python tools/smoke/run.py list-scenarios --env investor-demo
```

Run at least one plushie websocket scenario against a dedicated test user and
demo device before pointing real investor-demo devices at the VM.

Latest validation:

- HTTP root: `http://34.46.57.140:8000/` returned `Server is running`.
- TCP: ports `1883`, `8000`, and `8003` accepted connections.
- Containers: `bm-investor-demo-server` and `bm-investor-demo-mqtt` were running with zero restarts.
- Firestore: server container created a client for `composed-augury-469200-g6` and read the `devices` collection.
- Supabase: server container received `200` from a staging `users` REST HEAD probe in about `0.17s`.
- Smoke preflight: passed for `external-dev` + `live-shape` with manual scheduler trigger.

## Meeting-Day Checklist

- Verify the VM is `RUNNING`.
- Verify `/health` or root HTTP returns success.
- Verify websocket path from a laptop on the same network the devices will use.
- Verify staging Supabase writes a new session and turn.
- Verify Firestore device profile lookup for each demo device.
- Keep `gcloud compute ssh ... --command="sudo docker logs -f --tail=200 bm-investor-demo-server"` ready.
- Keep a rollback release SHA recorded.
- Keep production devices pointed away from the demo VM.
- When the roadshow is over, restrict or delete `bm-investor-demo-voice-ingress`.
