#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Move ASR temp wav files into an archive tree with a JSONL manifest."
    )
    parser.add_argument(
        "--source-dir",
        default="tmp",
        help="Directory containing asr_base_*.wav files.",
    )
    parser.add_argument(
        "--archive-root",
        required=True,
        help="Root directory where archived audio and manifest should be stored.",
    )
    parser.add_argument(
        "--manifest-name",
        default="manifest.jsonl",
        help="Manifest filename written inside the archive root.",
    )
    parser.add_argument(
        "--prefix",
        default="imported",
        help="Subdirectory prefix used under the archive root.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    source_dir = Path(args.source_dir).resolve()
    archive_root = Path(args.archive_root).resolve()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    batch_dir = archive_root / args.prefix / timestamp / "raw"
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = archive_root / args.manifest_name
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    bytes_total = 0
    with manifest_path.open("a", encoding="utf-8") as manifest:
        for path in sorted(source_dir.glob("asr_base_*.wav")):
            target = batch_dir / path.name
            size = path.stat().st_size
            os.replace(path, target)
            manifest.write(
                json.dumps(
                    {
                        "archivedAt": datetime.now(timezone.utc).isoformat(),
                        "sourcePath": str(path),
                        "archivedPath": str(target),
                        "sizeBytes": size,
                        "batch": timestamp,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            count += 1
            bytes_total += size

    print(
        json.dumps(
            {
                "archiveRoot": str(archive_root),
                "batch": timestamp,
                "count": count,
                "bytes": bytes_total,
            }
        )
    )


if __name__ == "__main__":
    main()
