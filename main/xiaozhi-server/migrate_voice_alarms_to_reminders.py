"""
One-time migration: normalize reminder documents and, when explicitly safe,
move legacy reminder-like voice docs from alarms to reminders.

This script now avoids migrating real wake-up alarms by requiring a legacy
reminder marker: a one-time voice doc whose label/context exists and whose
target mode is not already a dedicated reminder mode. It also normalizes
existing reminders so all of them carry deliveryChannel and the canonical
one-time schedule shape used by backend reminder docs:

  schedule.repeat   = "none"
  schedule.dateLocal = "YYYY-MM-DD"
  targets[].mode    = "reminder"
  deliveryChannel   = ["plushie"] if missing

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


def _normalize_schedule(data: dict) -> dict:
    schedule = dict(data.get("schedule") or {})
    repeat = str(schedule.get("repeat", "")).strip().lower()
    if repeat in {"once", "none", "one_time", "one-time", "no_repeat"}:
        schedule["repeat"] = "none"
        if not schedule.get("dateLocal"):
            days = schedule.get("days")
            if isinstance(days, list) and days:
                schedule["dateLocal"] = days[0]
    return schedule


def _normalize_targets(data: dict) -> list:
    targets = []
    for target in data.get("targets") or []:
        if not isinstance(target, dict):
            continue
        normalized = dict(target)
        normalized["mode"] = "reminder"
        targets.append(normalized)
    return targets


def _build_normalized_reminder_doc(data: dict) -> dict:
    normalized = dict(data)
    normalized["deliveryChannel"] = data.get("deliveryChannel") or ["plushie"]
    normalized["schedule"] = _normalize_schedule(data)
    normalized["targets"] = _normalize_targets(data)
    return normalized


def _looks_like_legacy_reminder_alarm(data: dict) -> bool:
    if (data.get("source") or "").strip().lower() != "voice":
        return False
    schedule = _normalize_schedule(data)
    if schedule.get("repeat") != "none":
        return False
    targets = data.get("targets") or []
    if not isinstance(targets, list) or not targets:
        return False
    return True


def migrate():
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"\n{'='*60}")
    print(f"  Migration: alarms → reminders  [{mode}]")
    print(f"{'='*60}\n")

    client = build_client()

    print("Fetching reminder docs for normalization...")
    reminder_docs = list(client.collection_group("reminders").stream())
    print(f"Existing reminder docs fetched: {len(reminder_docs)}")

    print("Fetching alarm docs to look for legacy reminder-like voice records...")
    all_alarm_docs = list(client.collection_group("alarms").stream())
    alarm_docs = [d for d in all_alarm_docs if _looks_like_legacy_reminder_alarm(d.to_dict() or {})]
    print(f"Alarm docs fetched: {len(all_alarm_docs)}  |  legacy reminder-like alarms: {len(alarm_docs)}\n")

    if not reminder_docs and not alarm_docs:
        print("Nothing to normalize or migrate. Exiting.")
        return

    migrated = 0
    skipped = 0
    errors = 0

    for doc in reminder_docs:
        data = doc.to_dict() or {}
        normalized = _build_normalized_reminder_doc(data)
        if normalized == data:
            skipped += 1
            continue

        print(
            f"  {'[DRY RUN] ' if DRY_RUN else ''}"
            f"normalize {doc.reference.path}\n"
            f"    deliveryChannel={normalized.get('deliveryChannel')}\n"
            f"    schedule={normalized.get('schedule')}\n"
        )

        if not DRY_RUN:
            try:
                doc.reference.set(normalized, merge=True)
                migrated += 1
            except Exception as e:
                print(f"    ❌ ERROR: {e}\n")
                errors += 1
        else:
            migrated += 1

    for doc in alarm_docs:
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

        new_data = _build_normalized_reminder_doc(data)

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
