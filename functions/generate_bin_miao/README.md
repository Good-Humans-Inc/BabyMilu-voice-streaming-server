# Generate test.bin (Cloud Functions v2)

HTTP Cloud Function that:
- Downloads expected GIFs from a GCS folder
- Optionally crops/resizes them to 360x360
- Packs them into a single `test.bin`
- Uploads the result back to GCS

## Deploy

```bash
gcloud functions deploy generate-bin-miao \
  --gen2 \
  --region=us-central1 \
  --runtime=python311 \
  --source=. \
  --entry-point=generate_bin_miao \
  --trigger-http \
  --allow-unauthenticated
```

Consider removing `--allow-unauthenticated` and adding an invoker service account for production.

## Request

POST JSON to the function URL:

```json
{
  "source_bucket": "milu-assets",
  "source_prefix": "animations/miao/a4%3acf%3a12%3a34%3a56%3a78",
  "destination_bucket": "milu-public",
  "destination_path": "device_bin/a4%3acf%3a12%3a34%3a56%3a78/test.bin",
  "no_crop": false
}
```

Fields:
- `source_bucket`: GCS bucket containing the GIF files
- `source_prefix`: Folder/prefix where the GIFs are (no leading slash)
- `destination_bucket`: Target bucket for `test.bin`
- `destination_path`: Object path for the uploaded `test.bin`
- `no_crop` (optional): If `true`, skip crop/resize step

Expected GIF names under `source_prefix`:
```
normal.gif, embarrass.gif, fire.gif, inspiration.gif, shy.gif,
sleep.gif, happy.gif, laugh.gif, sad.gif, talk.gif,
wifi.gif, battery.gif, silence.gif
```

## Response

```json
{
  "status": "ok",
  "upload": {
    "bucket": "milu-public",
    "object": "device_bin/<mac_enc>/test.bin",
    "gsUri": "gs://milu-public/device_bin/<mac_enc>/test.bin",
    "publicUrl": "https://storage.googleapis.com/milu-public/device_bin/<mac_enc>/test.bin"
  },
  "summary": {
    "filesPacked": 12,
    "totalSize": 123456,
    "headerSize": 12,
    "fileTableSize": 528,
    "dataSectionSize": 122916,
    "checksum": "0x0123ABCD",
    "missingMainGifs": [],
    "packedFiles": [
      { "name": "normal.gif", "size": 12345, "offset": 0 }
    ]
  },
  "options": {
    "cropped": true,
    "sourceBucket": "milu-assets",
    "sourcePrefix": "animations/miao/<mac_enc>",
    "destinationBucket": "milu-public",
    "destinationPath": "device_bin/<mac_enc>/test.bin"
  }
}
```

## Notes
- Cropping uses box `(244, 219, 780, 755)` then resizes to `360x360`.
- Missing core GIFs will be listed in `summary.missingMainGifs`; packing continues with available files.
- Function writes to `/tmp` and cleans up on container recycle automatically.


