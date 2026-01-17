import os
import json
import urllib.parse
import requests
import functions_framework


def _log(msg: str):
    print(f"[auto_update_trigger] {msg}")


def _decode_mac_from_path(object_name: str) -> str | None:
    # Expected: device_bin/<MAC_ENC>/mega.bin
    parts = object_name.split("/")
    if len(parts) != 3:
        return None
    if parts[0] != "device_bin" or parts[2] != "mega.bin":
        return None
    mac_enc = parts[1]
    # Decode percent-encoding (handles both %3a and %3A)
    return urllib.parse.unquote(mac_enc)


@functions_framework.cloud_event
def on_finalize(cloud_event):
    """
    CloudEvent trigger for GCS finalize on mega.bin.
    Environment vars:
      XIAOZHI_BASE: http://<xiaozhi-host>:8003
      MQTT_URL: mqtt://<broker-host>:1883
    """
    data = cloud_event.data
    bucket = data.get("bucket")
    name = data.get("name")  # object path

    if not name:
        _log("No object name in event, ignoring")
        return "ignored"

    mac = _decode_mac_from_path(name)
    if not mac:
        _log(f"Ignored object: {name}")
        return "ignored"

    xiaozhi_base = os.environ.get("XIAOZHI_BASE")
    mqtt_url = os.environ.get("MQTT_URL")
    if not xiaozhi_base or not mqtt_url:
        _log("Missing XIAOZHI_BASE or MQTT_URL env; aborting")
        return "misconfigured"

    # Build public URL to the object
    public_url = f"https://storage.googleapis.com/{bucket}/{name}"
    payload = {
        "deviceId": mac,
        "url": public_url,
        "broker": mqtt_url,
    }
    _log(f"Triggering auto_update for {mac} -> {public_url}")

    try:
        r = requests.post(
            f"{xiaozhi_base}/animation/auto_updates",
            json=payload,
            timeout=5,
        )
        _log(f"xiaozhi response: {r.status_code} {r.text}")
        r.raise_for_status()
        return "ok"
    except Exception as e:
        _log(f"Failed to call xiaozhi-server: {e}")
        return "error"


