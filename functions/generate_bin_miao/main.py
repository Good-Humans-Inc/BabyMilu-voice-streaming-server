import os
import json
import struct
import urllib.parse
from pathlib import Path
from typing import Dict, List, Tuple

import functions_framework
from PIL import Image, ImageSequence
from google.cloud import storage
from flask import Request, make_response


# Crop/Resize configuration
CROP_BOX = (244, 219, 780, 755)  # (left, top, right, bottom)
TARGET_SIZE = (360, 360)  # Final size after resize

# Animation names in order - main animations to load
ANIMATION_NAMES: List[str] = [
    "normal",
    "embarrass",
    "fire",
    "inspiration",
    "shy",
    "sleep",
    "happy",
    "laugh",
    "sad",
    "talk",
    "silence",
]

# Animations with _start/_loop variants
ANIMATIONS_WITH_START: List[str] = ["fire", "happy", "inspiration", "laugh"]

# System/status GIFs (no _loop suffix)
SYSTEM_GIFS: List[str] = ["wifi", "battery"]

# Extra GIFs (optional, not mapped yet)
EXTRA_GIFS: List[str] = ["listening_loop", "smirk_loop"]


def build_device_bin_order() -> List[str]:
    """
    Explicit order required by latest spec (20 items).
    """
    return [
        "smirk.gif",
        "smirk_start.gif",
        "heart.gif",
        "heart_start.gif",
        "blush.gif",
        "battery.gif",
        "wifi.gif",
        "silence.gif",
        "sad.gif",
        "sad_start.gif",
        "laugh.gif",
        "laugh_start.gif",
        "sleep.gif",
        "starry.gif",
        "starry_start.gif",
        "cry.gif",
        "normal.gif",
        "angry.gif",
        "angry_start.gif",
        "listening.gif",
    ]


# All expected GIF filenames (used for targeted download when not listing)
ALL_GIFS: List[str] = build_device_bin_order()


def _ok(payload: dict, status: int = 200):
    resp = make_response(json.dumps(payload, ensure_ascii=False), status)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp


def _err(message: str, status: int = 400, extra: dict | None = None):
    payload = {"error": message}
    if extra:
        payload.update(extra)
    return _ok(payload, status=status)


def _parse_gcs_like_url(url: str) -> tuple[str, str] | None:
    """
    Parse a folder-like URL into (bucket, prefix).
    Supports:
      - gs://bucket/prefix
      - https://storage.googleapis.com/bucket/prefix
      - https://storage.cloud.google.com/bucket/prefix
    Returns None if not parseable.
    """
    if not url:
        return None
    try:
        if url.startswith("gs://"):
            remainder = url[len("gs://") :]
            parts = remainder.split("/", 1)
            if len(parts) == 1:
                return parts[0], ""
            return parts[0], parts[1].rstrip("/") + "/"
        if "storage.googleapis.com/" in url or "storage.cloud.google.com/" in url:
            if "storage.googleapis.com/" in url:
                base = "storage.googleapis.com/"
            else:
                base = "storage.cloud.google.com/"
            idx = url.index(base) + len(base)
            remainder = url[idx:]
            parts = remainder.split("/", 1)
            if len(parts) == 1:
                return parts[0], ""
            return parts[0], parts[1].rstrip("/") + "/"
    except Exception:
        return None
    return None


def compute_checksum(data: bytes) -> int:
    return sum(data) & 0xFFFFFFFF


def verify_gif_format(file_path: str) -> bool:
    try:
        with open(file_path, "rb") as f:
            header = f.read(6)
            if header[:3] == b"GIF" and header[3:6] in (b"87a", b"89a"):
                return True
    except Exception:
        pass
    return False


def crop_gif(input_path: Path, output_path: Path, target_size: tuple[int, int] | None = None, keep_alpha: bool = True) -> bool:
    try:
        gif = Image.open(str(input_path))
        frames: List[Image.Image] = []
        durations: List[int] = []
        for frame in ImageSequence.Iterator(gif):
            cropped_frame = frame.crop(CROP_BOX)
            size = target_size if target_size else TARGET_SIZE
            resized_frame = cropped_frame.resize(size, Image.Resampling.LANCZOS)
            if not keep_alpha:
                # Flatten transparency by converting to RGB
                if resized_frame.mode in ("P", "RGBA"):
                    resized_frame = resized_frame.convert("RGB")
            frames.append(resized_frame.copy())
            if "duration" in frame.info:
                durations.append(frame.info["duration"])
            else:
                durations.append(100)
        if not frames:
            return False
        save_kwargs = {
            "save_all": True,
            "append_images": frames[1:],
            "duration": durations,
            "loop": gif.info.get("loop", 0),
        }
        if keep_alpha:
            if "palette" in gif.info:
                save_kwargs["palette"] = gif.info["palette"]
            if "transparency" in gif.info:
                save_kwargs["transparency"] = gif.info["transparency"]
        frames[0].save(str(output_path), **save_kwargs)
        return True
    except Exception:
        return False


def crop_all_gifs(gif_folder: Path, target_size: tuple[int, int] | None = None, keep_alpha: bool = True) -> bool:
    gif_files = [f for f in os.listdir(gif_folder) if f.lower().endswith(".gif")]
    if not gif_files:
        return False
    success = True
    for gif_file in sorted(gif_files):
        in_path = gif_folder / gif_file
        out_path = gif_folder / gif_file  # overwrite
        ok = crop_gif(in_path, out_path, target_size=target_size, keep_alpha=keep_alpha)
        success = success and ok
    return success


def pack_gif_file(file_name: str, file_path: str, offset: int, max_name_len: int = 32) -> Tuple[bytearray, bytearray, int]:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"GIF file not found: {file_path}")
    if not verify_gif_format(file_path):
        raise ValueError(f"File is not a valid GIF: {file_path}")
    with open(file_path, "rb") as f:
        file_data = f.read()
    file_size = len(file_data)
    name_padded = file_name[:max_name_len].ljust(max_name_len, "\0")
    table_entry = bytearray()
    table_entry.extend(name_padded.encode("utf-8"))
    table_entry.extend(struct.pack("<I", file_size))
    table_entry.extend(struct.pack("<I", offset))
    table_entry.extend(struct.pack("<H", 0))  # width (unused for GIF)
    table_entry.extend(struct.pack("<H", 0))  # height (unused for GIF)
    data_entry = bytearray()
    data_entry.extend(b"\x5A\x5A")  # magic bytes
    data_entry.extend(file_data)
    return table_entry, data_entry, file_size


def create_test_bin(gif_folder: Path, output_path: Path) -> dict:
    present_files = {p.name: p for p in gif_folder.iterdir() if p.is_file() and p.suffix.lower() == ".gif"}
    pack_order = build_device_bin_order()
    ordered = [name for name in pack_order if name in present_files]
    missing_required = [name for name in pack_order if name not in present_files]
    if not ordered:
        raise FileNotFoundError("No GIF files found matching expected names")
    file_table = bytearray()
    data_section = bytearray()
    current_offset = 0
    file_info_list: List[Tuple[str, int, int]] = []
    for fname in ordered:
        path = str(present_files[fname])
        entry, data, size = pack_gif_file(fname, path, current_offset)
        file_table.extend(entry)
        data_section.extend(data)
        file_info_list.append((fname, size, current_offset))
        current_offset += len(data)
    combined = file_table + data_section
    checksum = compute_checksum(combined)
    header = struct.pack("<I", len(file_info_list))
    header += struct.pack("<I", checksum)
    header += struct.pack("<I", len(combined))
    final_data = header + combined
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(final_data)
    return {
        "filesPacked": len(file_info_list),
        "totalSize": len(final_data),
        "headerSize": 12,
        "fileTableSize": len(file_table),
        "dataSectionSize": len(data_section),
        "checksum": f"0x{checksum:08X}",
        "missingMainGifs": missing_required,
        "packedFiles": [{"name": n, "size": s, "offset": o} for (n, s, o) in file_info_list],
        "packOrder": pack_order,
    }


def _download_gifs_from_gcs(
    client: storage.Client,
    bucket_name: str,
    source_prefix: str,
    dest_dir: Path,
) -> Dict[str, Path]:
    bucket = client.bucket(bucket_name)
    prefix = source_prefix if source_prefix.endswith("/") else f"{source_prefix}/"
    # Map required files to their expected object names under the prefix
    expected: Dict[str, str] = {gif: f"{prefix}{gif}" for gif in ALL_GIFS}
    out: Dict[str, Path] = {}
    for gif_name, object_name in expected.items():
        blob = bucket.blob(object_name)
        if blob.exists():
            local_path = dest_dir / gif_name
            local_path.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(local_path))
            out[gif_name] = local_path
    return out


def _list_all_gifs_from_gcs(client: storage.Client, bucket_name: str, source_prefix: str) -> List[dict]:
    """
    List all .gif files under the given prefix.
    Returns list of dicts: { name, size, updated, object }
    """
    bucket = client.bucket(bucket_name)
    prefix = source_prefix if source_prefix.endswith("/") else f"{source_prefix}/"
    blobs = client.list_blobs(bucket, prefix=prefix)
    results: List[dict] = []
    plen = len(prefix)
    for blob in blobs:
        if not blob.name.lower().endswith(".gif"):
            continue
        # Derive simple filename under the prefix
        name = blob.name[plen:] if blob.name.startswith(prefix) else blob.name
        if "/" in name:
            # Skip nested subfolders; only direct children
            continue
        results.append(
            {
                "name": name,
                "size": blob.size,
                "updated": blob.updated.isoformat() if getattr(blob, "updated", None) else None,
                "object": blob.name,
            }
        )
    # Sort by filename for deterministic packing
    results.sort(key=lambda x: x["name"])
    return results


def _download_all_gifs_from_gcs(
    client: storage.Client,
    bucket_name: str,
    source_prefix: str,
    dest_dir: Path,
) -> Dict[str, Path]:
    """
    Download every .gif directly under source_prefix.
    """
    files = _list_all_gifs_from_gcs(client, bucket_name, source_prefix)
    out: Dict[str, Path] = {}
    if not files:
        return out
    bucket = client.bucket(bucket_name)
    for item in files:
        name = item["name"]
        object_name = item["object"]
        blob = bucket.blob(object_name)
        local_path = dest_dir / name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(local_path))
        out[name] = local_path
    return out


def _upload_to_gcs(client: storage.Client, bucket_name: str, object_name: str, local_path: Path) -> dict:
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    blob.upload_from_filename(str(local_path), content_type="application/octet-stream")
    public_url = f"https://storage.googleapis.com/{bucket_name}/{urllib.parse.quote(object_name)}"
    return {
        "bucket": bucket_name,
        "object": object_name,
        "gsUri": f"gs://{bucket_name}/{object_name}",
        "publicUrl": public_url,
    }


@functions_framework.http
def generate_bin_miao(request: Request):
    """
    HTTP Cloud Function (Gen2) to generate test.bin from GIFs stored in GCS.

    Request JSON body:
      Accepts either explicit bucket/prefix fields or folder URLs:
      - source_bucket + source_prefix OR source_folder_url (gs:// or https URL)
      - destination_bucket + destination_path OR destination_folder_url (folder; test.bin will be appended)
      - no_crop: optional bool, if true skip crop/resize step
      - pack_all: optional bool, if true pack all .gif files under prefix (default true when using source_folder_url)
      - list_only: optional bool, if true only list GIFs without packing/upload
    """
    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        return _err("Invalid JSON body", 400)

    source_bucket = body.get("source_bucket")
    source_prefix = body.get("source_prefix")
    destination_bucket = body.get("destination_bucket")
    destination_path = body.get("destination_path")
    source_folder_url = (body.get("source_folder_url") or "").strip()
    destination_folder_url = (body.get("destination_folder_url") or "").strip()
    no_crop = bool(body.get("no_crop", False))
    list_only = bool(body.get("list_only", False))
    pack_all = bool(body.get("pack_all", False))

    # Compatibility fields (curl-friendly)
    character_id = body.get("characterId") or body.get("character_id")
    device_id = body.get("deviceId") or body.get("device_id")
    compat_bucket = body.get("bucket")
    compat_prefix = body.get("prefix")
    output_filename = (body.get("output_filename") or body.get("mega_filename") or "test.bin").strip() or "test.bin"
    # Resize/alpha options
    target_width = body.get("width")
    target_height = body.get("height")
    if isinstance(target_width, str) and target_width.isdigit():
        target_width = int(target_width)
    if isinstance(target_height, str) and target_height.isdigit():
        target_height = int(target_height)
    keep_alpha = body.get("keep_alpha")
    if keep_alpha is None:
        keep_alpha = True
    else:
        keep_alpha = bool(keep_alpha)

    # Resolve source from folder URL if provided
    if source_folder_url:
        parsed = _parse_gcs_like_url(source_folder_url)
        if not parsed:
            return _err("source_folder_url not parseable", 400)
        source_bucket, source_prefix = parsed[0], parsed[1]
        # If using folder URL, default to pack all
        if "pack_all" not in body:
            pack_all = True

    # Derive source from characterId if provided and no explicit source_prefix
    if not source_prefix and character_id:
        source_prefix = f"characters/{character_id}/device/"

    # Default source bucket to compat bucket if not provided
    if not source_bucket and compat_bucket:
        source_bucket = compat_bucket

    # Resolve destination from folder URL if provided
    if destination_folder_url:
        parsed = _parse_gcs_like_url(destination_folder_url)
        if not parsed:
            return _err("destination_folder_url not parseable", 400)
        destination_bucket = parsed[0]
        # Append test.bin under provided folder
        dest_prefix = parsed[1]
        # Default to test.bin when using folder URL and no explicit output name was provided
        if ("output_filename" not in body) and ("mega_filename" not in body):
            output_filename = "test.bin"
        if device_id:
            # Store under device subfolder: <dest_prefix>/<deviceId>/device.bin
            base = dest_prefix.rstrip("/")
            destination_path = f"{base}/{device_id}/{output_filename}" if base else f"{device_id}/{output_filename}"
        else:
            destination_path = f"{dest_prefix.rstrip('/')}/{output_filename}" if dest_prefix else output_filename

    # Resolve destination from compat fields if provided (bucket + prefix)
    if compat_bucket and compat_prefix:
        if not destination_bucket:
            destination_bucket = compat_bucket
        # Only set destination_path if absent
        if not destination_path:
            p = compat_prefix.rstrip("/")
            destination_path = f"{p}/{output_filename}" if p else output_filename

    # If all source fields are blank, return a no-op response (user wants fields optional for now)
    if not source_bucket or not source_prefix:
        return _ok(
            {
                "status": "noop",
                "message": "No source provided. Provide source_bucket+source_prefix or source_folder_url to proceed.",
                "expected": {
                    "source_folder_url": "gs://bucket/path/to/gifs/",
                    "destination_folder_url": "gs://bucket/path/to/output/",
                },
            },
            200,
        )

    tmp_root = Path("/tmp/generate_bin_miao")
    gifs_dir = tmp_root / "gifs"
    out_path = tmp_root / "out" / "test.bin"

    client = storage.Client()

    # 1) Discover + Download GIFs
    if pack_all:
        discovered = _list_all_gifs_from_gcs(client, source_bucket, source_prefix)
        if list_only:
            return _ok({"status": "ok", "listOnly": True, "gifs": discovered}, 200)
        downloaded = _download_all_gifs_from_gcs(client, source_bucket, source_prefix, gifs_dir)
    else:
        if list_only:
            # List known filenames that exist
            discovered = []
            for name in ALL_GIFS:
                object_name = f"{source_prefix.rstrip('/')}/{name}"
                blob = client.bucket(source_bucket).blob(object_name)
                if blob.exists():
                    discovered.append(
                        {
                            "name": name,
                            "size": blob.size,
                            "updated": blob.updated.isoformat() if getattr(blob, "updated", None) else None,
                            "object": object_name,
                        }
                    )
            return _ok({"status": "ok", "listOnly": True, "gifs": discovered}, 200)
        downloaded = _download_gifs_from_gcs(client, source_bucket, source_prefix, gifs_dir)

    if not downloaded:
        return _err("No GIFs found at source_prefix", 404, {"hint": "Ensure .gif files exist under the specified folder"})

    # 2) Crop/resize (unless skipped)
    if not no_crop:
        ts = None
        if isinstance(target_width, int) and isinstance(target_height, int) and target_width > 0 and target_height > 0:
            ts = (target_width, target_height)
        crop_all_gifs(gifs_dir, target_size=ts, keep_alpha=keep_alpha)

    # 3) Pack into device.bin/test.bin
    try:
        if pack_all:
            # Use standardized device.bin order
            present_files = {p.name: p for p in gifs_dir.iterdir() if p.is_file() and p.suffix.lower() == ".gif"}
            pack_order = build_device_bin_order()
            ordered = [name for name in pack_order if name in present_files]
            missing_required = [name for name in pack_order if name not in present_files]
            if not ordered:
                return _err("No required GIFs found to pack", 404, {"required": pack_order})
            file_table = bytearray()
            data_section = bytearray()
            current_offset = 0
            file_info_list: List[Tuple[str, int, int]] = []
            for fname in ordered:
                path = str(present_files[fname])
                entry, data, size = pack_gif_file(fname, path, current_offset)
                file_table.extend(entry)
                data_section.extend(data)
                file_info_list.append((fname, size, current_offset))
                current_offset += len(data)
            combined = file_table + data_section
            checksum = compute_checksum(combined)
            header = struct.pack("<I", len(file_info_list))
            header += struct.pack("<I", checksum)
            header += struct.pack("<I", len(combined))
            final_data = header + combined
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(final_data)
            summary = {
                "filesPacked": len(file_info_list),
                "totalSize": len(final_data),
                "headerSize": 12,
                "fileTableSize": len(file_table),
                "dataSectionSize": len(data_section),
                "checksum": f"0x{checksum:08X}",
                "missingMainGifs": missing_required,
                "packedFiles": [{"name": n, "size": s, "offset": o} for (n, s, o) in file_info_list],
                "packOrder": pack_order,
            }
        else:
            summary = create_test_bin(gifs_dir, out_path)
    except Exception as e:
        return _err(f"Failed to create test.bin: {e}", 500)

    # 4) Upload (optional if destination provided)
    upload_info = None
    if destination_bucket and destination_path:
        try:
            upload_info = _upload_to_gcs(client, destination_bucket, destination_path, out_path)
        except Exception as e:
            return _err(f"Upload failed: {e}", 500)

    result = {
        "status": "ok",
        "upload": upload_info,
        "summary": summary,
        "options": {
            "cropped": not no_crop,
            "sourceBucket": source_bucket,
            "sourcePrefix": source_prefix,
            "destinationBucket": destination_bucket,
            "destinationPath": destination_path,
            "packAll": pack_all,
            "listOnly": list_only,
            "sourceFolderUrl": source_folder_url or None,
            "destinationFolderUrl": destination_folder_url or None,
            "deviceId": device_id or None,
        },
    }
    return _ok(result, 200)


