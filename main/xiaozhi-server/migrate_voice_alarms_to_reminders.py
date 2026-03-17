"""
One-time migration: move voice-created alarm docs from
  users/{uid}/alarms/{id}   (where source == "voice")
to
  users/{uid}/reminders/{id}
adding deliveryChannel: ["plushie"] in the process.

Usage:
  # Dry-run (preview only, no writes):
  python migrate_voice_alarms_to_reminders.py --dry-run

  # Live run:
  python migrate_voice_alarms_to_reminders.py

Credentials: resolved automatically via get_gcp_credentials_path()
(same mechanism the server uses — looks for data/.gcp/sa.json,
 /opt/secrets/gcp/sa.json, or GOOGLE_APPLICATION_CREDENTIALS env var).
"""
from __future__ import annotations

import os
import sys

# Allow running from the xiaozhi-server directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import get_gcp_credentials_path
from google.cloud import firestore

DRY_RUN = "--dry-run" in sys.argv


def build_client() -> firestore.Client:
    creds_path = get_gcp_credentials_path()
    if creds_path:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
        print(f"✅ Using credentials: {creds_path}")
    else:
        print("⚠️  No explicit credentials file found — falling back to application default credentials.")
    return firestore.Client()


def migrate():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"\n{'='*60}")
    print(f"  Migration: alarms → reminders  [{mode}]")
    print(f"{'='*60}\n")

    client = build_client()

    # Fetch ALL docs from the alarms collection group — no server-side filter on
    # "source" because that would require a collection-group index that doesn't exist.
    # We filter client-side below instead.
    print("Fetching all docs from alarms collection group (client-side filter by source='voice')...")
    all_docs = list(client.collection_group("alarms").stream())
    docs = [d for d in all_docs if (d.to_dict() or {}).get("source") == "voice"]
    print(f"Total alarms docs fetched: {len(all_docs)}  |  voice-created: {len(docs)}\n")

    if not docs:
        print("Nothing to migrate. Exiting.")
        return

    migrated = 0
    skipped = 0
    errors = 0

    for doc in docs:
        data = doc.to_dict() or {}
        parent = doc.reference.parent.parent
        if parent is None:
            print(f"  SKIP  {doc.reference.path}  (cannot resolve parent user)")
            skipped += 1
            continue

        uid = parent.id
        reminder_id = doc.id
        src_path = doc.reference.path
        dest_path = f"users/{uid}/reminders/{reminder_id}"

        # Build destination doc: copy everything, ensure deliveryChannel is set
        new_data = {
            **data,
            "deliveryChannel": data.get("deliveryChannel") or ["plushie"],
        }

        status = data.get("status", "?")
        label = data.get("label", "(no label)")
        next_occ = data.get("nextOccurrenceUTC", "?")
        print(
            f"  {'[DRY RUN] ' if DRY_RUN else ''}"
            f"{src_path}\n"
            f"    → {dest_path}\n"
            f"    status={status}  label='{label}'  nextOccurrenceUTC={next_occ}\n"
            f"    deliveryChannel={new_data['deliveryChannel']}\n"
        )

        if not DRY_RUN:
            try:
                dest_ref = (
                    client.collection("users")
                    .document(uid)
                    .collection("reminders")
                    .document(reminder_id)
                )
                dest_ref.set(new_data)
                doc.reference.delete()
                migrated += 1
            except Exception as e:
                print(f"    ❌ ERROR: {e}\n")
                errors += 1
        else:
            migrated += 1

    print(f"\n{'='*60}")
    print(f"  {'Would migrate' if DRY_RUN else 'Migrated'}: {migrated}")
    print(f"  Skipped:  {skipped}")
    print(f"  Errors:   {errors}")
    if DRY_RUN:
        print("\n  ⚠️  DRY RUN — no changes were written.")
        print("  Re-run without --dry-run to apply.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    migrate()
