"""
Cloud Function: Build per-image SPIFFS .bin files from PNG URLs and write batch manifest

Trigger: HTTP (POST)
Input JSON:
{
  "characterId": "ch_xxx",
  "deviceId": "AA:AA:...",
  "frames": ["https://storage.googleapis.com/.../normal1.png", ...],
  "fmt": "rgb565",                 # optional, default rgb565 (or rgb565a8 when alpha detected)
  "width": 256,                    # optional, default 256
  "height": 256,                   # optional, default 256
  "bucket": "milu-public",        # target bucket for outputs
  "prefix": "device_bin",         # optional GCS prefix, default device_bin
  "source_prefix": "characters/ch_xxx/device/",  # optional; if frames missing, list PNGs under this prefix
  "keep_alpha": false                       # optional; default false → force RGB565
}

Outputs:
  - gs://{bucket}/{prefix}/{deviceId}/{characterId}/{basename}.bin
  - gs://{bucket}/{prefix}/{deviceId}/{characterId}/manifest.json (batch)
  - Update Firestore devices/{deviceId}.animation with latest batch pointer

Note: This is a scaffold intended to be deployed to Cloud Functions (Python 3.11) or Cloud Run.
      Replace placeholder handling and harden error checks for production.
"""

import base64
import io
import json
import hashlib
import time
import zlib
from datetime import datetime, timezone
from typing import List, Tuple
import os

import functions_framework
import requests
from PIL import Image
from google.cloud import storage, firestore


def _now_version() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _download_png(url: str, retries: int = 3, backoff: float = 0.5) -> Image.Image:
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content))
        except Exception as e:
            last_err = e
            time.sleep(backoff * (2 ** i))
    raise last_err


def _to_pixels_bytes(img: Image.Image, target_w: int, target_h: int, keep_alpha: bool) -> tuple[bytes, str]:
    if img.size != (target_w, target_h):
        # Use LANCZOS when available
        try:
            img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)  # Pillow>=9
        except Exception:
            img = img.resize((target_w, target_h), Image.BILINEAR)

    has_alpha = (img.mode in ("RGBA", "LA")) or ("transparency" in img.info)
    if has_alpha and keep_alpha:
        img = img.convert("RGBA")
        width, height = img.size
        out = bytearray()
        for y in range(height):
            for x in range(width):
                r, g, b, a = img.getpixel((x, y))
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                # little-endian: low byte first, then high byte, then alpha
                out.append(rgb565 & 0xFF)
                out.append((rgb565 >> 8) & 0xFF)
                out.append(a)
        return bytes(out), "rgb565a8"
    else:
        img = img.convert("RGB")
        width, height = img.size
        out = bytearray()
        for y in range(height):
            for x in range(width):
                r, g, b = img.getpixel((x, y))
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                # little-endian: low byte first, then high byte
                out.append(rgb565 & 0xFF)
                out.append((rgb565 >> 8) & 0xFF)
        return bytes(out), "rgb565"


def _pack_anim(frames: List[bytes], width: int, height: int, fps: int, loop: int, fmt: str) -> bytes:
    # Simple header: magic(4) version_len(1) version bytes, fmt_len(1) fmt bytes, width(2), height(2), fps(1), loop(1), frame_count(2)
    version = _now_version().encode("utf-8")
    header = bytearray()
    header += b"BMAS"
    header += bytes([len(version)]) + version
    header += bytes([len(fmt)]) + fmt.encode("utf-8")
    header += width.to_bytes(2, "big") + height.to_bytes(2, "big")
    header += bytes([fps & 0xFF, loop & 0xFF])
    header += len(frames).to_bytes(2, "big")

    # Frame table: for each frame, offset(4), length(4)
    table = bytearray()
    offset = 0
    payload = bytearray()
    for f in frames:
        compressed = zlib.compress(f)
        table += offset.to_bytes(4, "big")
        table += len(compressed).to_bytes(4, "big")
        payload += compressed
        offset += len(compressed)

    return bytes(header + table + payload)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upload_blob(bucket: storage.Bucket, blob_path: str, data: bytes, content_type: str):
    blob = bucket.blob(blob_path)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{bucket.name}/{blob_path}"


def _write_device_animation(fs: firestore.Client, device_id: str, emotion: str, doc: dict):
    devices = fs.collection("devices").document(device_id)
    devices.set({"animation": {emotion: doc}, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)


@functions_framework.http
def generate_device_bin(request):
    if request.method != "POST":
        return ("Method Not Allowed", 405)

    try:
        body = request.get_json(force=True)
        device_id = body["deviceId"]
        character_id = body.get("characterId")
        frames_urls: List[str] | None = body.get("frames")
        width = int(body.get("width", 256))
        height = int(body.get("height", 256))
        bucket_name = body["bucket"]
        prefix = body.get("prefix", "device_bin").strip("/")
        store_under_device = bool(body.get("store_under_device", True))
        keep_alpha = bool(body.get("keep_alpha", False))
        default_src = f"characters/{character_id}/device/" if character_id else ""
        source_prefix = body.get("source_prefix", default_src).strip("/")
        if source_prefix:
            source_prefix += "/"

        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)

        # Build list of sources
        outputs = []
        sources: List[tuple[str, Image.Image]] = []
        if frames_urls and len(frames_urls) > 0:
            for url in frames_urls:
                img = _download_png(url)
                sources.append((url.split("/")[-1], img))
        elif source_prefix:
            for blob in storage_client.list_blobs(bucket_name, prefix=source_prefix):
                name_lower = blob.name.lower()
                if not (name_lower.endswith(".png") or name_lower.endswith(".jpg") or name_lower.endswith(".jpeg")):
                    continue
                data = blob.download_as_bytes()
                img = Image.open(io.BytesIO(data))
                sources.append((os.path.basename(blob.name), img))
        else:
            raise ValueError("either provide frames or source_prefix or characterId")
        if not sources:
            raise ValueError("no source images found (frames empty and no PNGs under source_prefix)")

        # Process each source -> one .bin
        for base_name, img in sources:
            pixels, fmt = _to_pixels_bytes(img, width, height, keep_alpha)

            # Build LVGL-like header (simplified): magic, color_format, flags(0), w, h, stride
            magic = 0x4C56474C  # "LVGL" little-endian
            cf_map = {"rgb565": 0x12, "rgb565a8": 0x14}
            cf_value = cf_map.get(fmt, 0x12)
            stride = width * (3 if fmt == "rgb565a8" else 2)
            header = (
                magic.to_bytes(4, "little")
                + cf_value.to_bytes(4, "little")
                + (0).to_bytes(4, "little")
                + width.to_bytes(4, "little")
                + height.to_bytes(4, "little")
                + stride.to_bytes(4, "little")
            )
            bin_bytes = header + pixels

            # Derive basename
            name_wo_ext = base_name.rsplit(".", 1)[0]
            if store_under_device or not character_id:
                bin_rel_path = f"{prefix}/{device_id}/{name_wo_ext}.bin"
            else:
                bin_rel_path = f"{prefix}/{device_id}/{character_id}/{name_wo_ext}.bin"
            _upload_blob(bucket, bin_rel_path, bin_bytes, "application/octet-stream")
            outputs.append({
                "name": name_wo_ext,
                "fmt": fmt,
                "width": width,
                "height": height,
                "size": len(bin_bytes),
                "url": f"https://storage.googleapis.com/{bucket_name}/{bin_rel_path}",
            })

        # Batch manifest
        version = _now_version()
        base_path = f"{prefix}/{device_id}/" if (store_under_device or not character_id) else f"{prefix}/{device_id}/{character_id}/"
        manifest = {
            "device_id": device_id,
            "character_id": character_id,
            "version": version,
            "count": len(outputs),
            "base_path": base_path,
            "bucket": bucket_name,
            "items": outputs,
        }
        manifest_rel = f"{base_path}manifest.json"
        _upload_blob(bucket, manifest_rel, json.dumps(manifest, separators=(",", ":")), "application/json")

        # Firestore pointer to latest batch
        fs = firestore.Client()
        fs.collection("devices").document(device_id).set(
            {
                "animation": {
                    "version": version,
                    "bucket": bucket_name,
                    "prefix": prefix,
                    "base_path": base_path,
                    "count": len(outputs),
                },
                "updated_at": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )

        return (
            json.dumps({"success": True, "manifest": manifest}, separators=(",", ":")),
            200,
            {"Content-Type": "application/json"},
        )
    except Exception as e:
        return (json.dumps({"success": False, "message": str(e)}), 500, {"Content-Type": "application/json"})
