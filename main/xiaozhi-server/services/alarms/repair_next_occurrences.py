from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from config.settings import get_gcp_credentials_path
from services.alarms import firestore_client, models, reminder_advancement, scheduler
from services.logging import setup_logging

TAG = __name__
logger = setup_logging()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ONE_TIME_REPEATS = {"", "none", "once", "one_time", "one-time", "no_repeat"}


@dataclass
class RepairDecision:
    doc_path: str
    doc_id: str
    user_id: str
    kind: str
    label: Optional[str]
    current_next_occurrence_utc: Optional[str]
    repairable: bool
    reason: str
    update: Dict[str, Any]

    def to_result(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "docPath": self.doc_path,
            "id": self.doc_id,
            "userId": self.user_id,
            "kind": self.kind,
            "label": self.label,
            "currentNextOccurrenceUTC": self.current_next_occurrence_utc,
            "repairable": self.repairable,
            "reason": self.reason,
        }
        if self.update:
            result["update"] = dict(self.update)
        return result


def repair_stale_next_occurrences(
    *,
    now: Optional[datetime] = None,
    execute: bool = False,
    client: Optional[firestore.Client] = None,
    uid: Optional[str] = None,
    limit: Optional[int] = None,
    scan_mode: str = "due",
) -> Dict[str, Any]:
    """Repair active reminder/alarm docs whose scheduler cursor is stale or missing.

    Dry-run is the default. In execute mode this only updates scheduler cursor
    fields: it does not set delivery markers, outcomes, or lastProcessedUTC.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    client = client or _build_client()
    user_tz_cache: Dict[str, Optional[str]] = {}

    scanned = 0
    impacted = 0
    repairable = 0
    updated = 0
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for doc in _iter_candidate_docs(client, now=now, scan_mode=scan_mode):
        if limit is not None and scanned >= limit:
            break
        scanned += 1
        try:
            data = doc.to_dict() or {}
            doc_user_id = _resolve_user_id(doc)
            if uid and doc_user_id != uid:
                continue
            current = _parse_datetime(data.get("nextOccurrenceUTC"))
            if current and current > now:
                continue

            impacted += 1
            decision = _build_decision(
                doc=doc,
                data=data,
                user_id=doc_user_id,
                current=current,
                now=now,
                user_tz_cache=user_tz_cache,
                client=client,
            )
            if decision.repairable:
                repairable += 1
                if execute:
                    client.document(decision.doc_path).set(decision.update, merge=True)
                    updated += 1
            results.append(decision.to_result())
        except Exception as exc:
            errors.append(
                {
                    "docPath": getattr(getattr(doc, "reference", None), "path", None),
                    "error": str(exc),
                }
            )

    return {
        "ok": not errors,
        "execute": execute,
        "scanMode": scan_mode,
        "nowUTC": _format_datetime(now),
        "scanned": scanned,
        "impacted": impacted,
        "repairable": repairable,
        "updated": updated,
        "results": results,
        "errors": errors,
    }


def _build_decision(
    *,
    doc,
    data: Dict[str, Any],
    user_id: str,
    current: Optional[datetime],
    now: datetime,
    user_tz_cache: Dict[str, Optional[str]],
    client: firestore.Client,
) -> RepairDecision:
    kind = _doc_kind(data)
    current_iso = _format_datetime(current) if current else None
    timezone_name = _resolve_timezone(doc, data, user_id, user_tz_cache)
    if not timezone_name:
        return _skip(doc, data, user_id, kind, current_iso, "missing_user_timezone")

    schedule = _normalized_schedule(data.get("schedule") or {})
    if not isinstance(schedule, dict) or not schedule.get("timeLocal"):
        return _skip(doc, data, user_id, kind, current_iso, "invalid_schedule")

    if kind == "alarm":
        next_occurrence = _next_alarm_occurrence(
            doc=doc,
            data=data,
            schedule_payload=schedule,
            user_id=user_id,
            timezone_name=timezone_name,
            current=current,
            now=now,
            client=client,
        )
        if next_occurrence is None:
            return _skip(doc, data, user_id, kind, current_iso, "no_future_occurrence")
        update = {
            "nextOccurrenceUTC": _format_datetime(next_occurrence),
            "updatedAt": _format_datetime(now),
        }
        return _repair(
            doc, data, user_id, kind, current_iso, "repair_alarm_cursor", update
        )

    next_occurrence = _next_reminder_occurrence(schedule, timezone_name, now)
    if next_occurrence is None:
        return _skip(doc, data, user_id, kind, current_iso, "no_future_occurrence")
    next_trigger = reminder_advancement.get_trigger_time(
        next_occurrence, from_time=now
    )
    update = {
        "nextOccurrenceUTC": _format_datetime(next_occurrence),
        "nextTriggerUTC": _format_datetime(next_trigger),
        "updatedAt": _format_datetime(now),
    }
    return _repair(doc, data, user_id, kind, current_iso, "repair_reminder_cursor", update)


def _next_alarm_occurrence(
    *,
    doc,
    data: Dict[str, Any],
    schedule_payload: Dict[str, Any],
    user_id: str,
    timezone_name: str,
    current: Optional[datetime],
    now: datetime,
    client: firestore.Client,
) -> Optional[datetime]:
    if _is_one_time_schedule(schedule_payload):
        return _one_time_occurrence(schedule_payload, timezone_name, now)
    try:
        schedule_doc = firestore_client._build_schedule(schedule_payload)
    except Exception:
        return None
    alarm = models.AlarmDoc(
        alarm_id=doc.id,
        user_id=user_id,
        uid=data.get("uid") or user_id,
        label=data.get("label"),
        schedule=schedule_doc,
        status=models.AlarmStatus.ON,
        next_occurrence_utc=current or now,
        targets=[],
        raw={**data, "user": {"timezone": timezone_name}},
        doc_path=doc.reference.path,
        user_timezone=timezone_name,
    )
    try:
        return scheduler.compute_next_occurrence(alarm, now=now)
    except Exception:
        # scheduler.compute_next_occurrence can refetch timezone; keep repair local.
        return _next_recurring_occurrence(schedule_payload, timezone_name, now)


def _next_reminder_occurrence(
    schedule_payload: Dict[str, Any],
    timezone_name: str,
    now: datetime,
) -> Optional[datetime]:
    if _is_one_time_schedule(schedule_payload):
        return _one_time_occurrence(schedule_payload, timezone_name, now)
    return _next_recurring_occurrence(schedule_payload, timezone_name, now)


def _next_recurring_occurrence(
    schedule_payload: Dict[str, Any],
    timezone_name: str,
    now: datetime,
) -> Optional[datetime]:
    try:
        next_occurrence = reminder_advancement.get_next_occurrence_utc(
            schedule_payload, timezone_name, from_date=now
        )
    except Exception:
        return None
    return _as_utc(next_occurrence) if next_occurrence else None


def _one_time_occurrence(
    schedule_payload: Dict[str, Any],
    timezone_name: str,
    now: datetime,
) -> Optional[datetime]:
    date_local = schedule_payload.get("dateLocal")
    if not date_local:
        days = schedule_payload.get("days") or []
        if len(days) == 1 and isinstance(days[0], str) and _DATE_RE.match(days[0]):
            date_local = days[0]
    if not date_local:
        return None
    try:
        hour, minute = reminder_advancement.parse_time_local(
            schedule_payload["timeLocal"]
        )
        year_s, month_s, day_s = str(date_local).split("-")
        timezone_info = ZoneInfo(str(timezone_name).strip() or "UTC")
        local_dt = datetime(
            int(year_s),
            int(month_s),
            int(day_s),
            hour,
            minute,
            tzinfo=timezone_info,
        )
    except Exception:
        return None
    next_occurrence = local_dt.astimezone(timezone.utc)
    if next_occurrence <= now:
        return None
    return next_occurrence


def _normalized_schedule(schedule_payload: Dict[str, Any]) -> Dict[str, Any]:
    schedule = dict(schedule_payload or {})
    repeat = str(schedule.get("repeat", "")).strip().lower()
    if repeat in {"once", "one_time", "one-time", "no_repeat"}:
        schedule["repeat"] = "none"
    return schedule


def _is_one_time_schedule(schedule_payload: Dict[str, Any]) -> bool:
    repeat = str(schedule_payload.get("repeat", "")).strip().lower()
    if repeat in _ONE_TIME_REPEATS:
        days = schedule_payload.get("days") or []
        if not days:
            return True
        return len(days) == 1 and isinstance(days[0], str) and _DATE_RE.match(days[0])
    return False


def _skip(
    doc,
    data: Dict[str, Any],
    user_id: str,
    kind: str,
    current_iso: Optional[str],
    reason: str,
) -> RepairDecision:
    return RepairDecision(
        doc_path=doc.reference.path,
        doc_id=doc.id,
        user_id=user_id,
        kind=kind,
        label=data.get("label"),
        current_next_occurrence_utc=current_iso,
        repairable=False,
        reason=reason,
        update={},
    )


def _repair(
    doc,
    data: Dict[str, Any],
    user_id: str,
    kind: str,
    current_iso: Optional[str],
    reason: str,
    update: Dict[str, Any],
) -> RepairDecision:
    return RepairDecision(
        doc_path=doc.reference.path,
        doc_id=doc.id,
        user_id=user_id,
        kind=kind,
        label=data.get("label"),
        current_next_occurrence_utc=current_iso,
        repairable=True,
        reason=reason,
        update=update,
    )


def _build_client(project: Optional[str] = None) -> firestore.Client:
    creds_path = get_gcp_credentials_path()
    if creds_path:
        import os

        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
    return firestore.Client(project=project) if project else firestore.Client()


def _iter_candidate_docs(
    client: firestore.Client, *, now: datetime, scan_mode: str
) -> Iterable[Any]:
    if scan_mode == "due":
        return _iter_due_reminder_docs(client, now=now)
    if scan_mode == "all-active":
        return _iter_active_reminder_docs(client)
    raise ValueError("scan_mode must be one of: due, all-active")


def _iter_due_reminder_docs(client: firestore.Client, *, now: datetime) -> Iterable[Any]:
    return (
        client.collection_group("reminders")
        .where(filter=FieldFilter("status", "==", "on"))
        .where(filter=FieldFilter("nextOccurrenceUTC", "<=", _format_datetime(now)))
        .stream()
    )


def _iter_active_reminder_docs(client: firestore.Client) -> Iterable[Any]:
    for user_doc in client.collection("users").stream():
        reminders = (
            user_doc.reference.collection("reminders")
            .where(filter=FieldFilter("status", "==", "on"))
            .stream()
        )
        yield from reminders


def _doc_kind(data: Dict[str, Any]) -> str:
    return "alarm" if data.get("typeHint") == "alarm" else "reminder"


def _resolve_user_id(doc) -> str:
    parent = doc.reference.parent.parent
    return parent.id if parent else ""


def _resolve_timezone(
    doc,
    data: Dict[str, Any],
    user_id: str,
    cache: Dict[str, Optional[str]],
) -> Optional[str]:
    user_block = data.get("user")
    if isinstance(user_block, dict):
        tz = _extract_timezone(user_block)
        if tz:
            return tz
    if not user_id:
        return None
    if user_id in cache:
        return cache[user_id]
    parent = doc.reference.parent.parent
    if parent is None:
        cache[user_id] = None
        return None
    try:
        snap = parent.get()
        if not snap.exists:
            cache[user_id] = None
            return None
        cache[user_id] = _extract_timezone(snap.to_dict() or {})
        return cache[user_id]
    except Exception:
        cache[user_id] = None
        return None


def _extract_timezone(payload: Dict[str, Any]) -> Optional[str]:
    value = (
        payload.get("timezone")
        or payload.get("timeZone")
        or payload.get("timezoneId")
        or payload.get("userTimezone")
    )
    if not value:
        return None
    timezone_name = str(value).strip()
    return timezone_name or None


def _parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _as_utc(parsed)
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair stale or missing nextOccurrenceUTC fields."
    )
    parser.add_argument("--project", help="Firestore project id")
    parser.add_argument("--uid", help="Restrict repair scan to one users/{uid}")
    parser.add_argument("--limit", type=int, help="Maximum active docs to scan")
    parser.add_argument(
        "--scan-mode",
        choices=("due", "all-active"),
        default="due",
        help=(
            "due uses the indexed scheduler query; all-active walks users/*/reminders "
            "and can also find missing nextOccurrenceUTC docs"
        ),
    )
    parser.add_argument("--execute", action="store_true", help="Write repair patches")
    parser.add_argument(
        "--confirm-project",
        help="Required with --execute; must match --project when provided",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    if args.execute:
        if not args.confirm_project:
            raise SystemExit("--execute requires --confirm-project")
        if args.project and args.confirm_project != args.project:
            raise SystemExit("--confirm-project must match --project")
    client = _build_client(project=args.project)
    result = repair_stale_next_occurrences(
        execute=args.execute,
        client=client,
        uid=args.uid,
        limit=args.limit,
        scan_mode=args.scan_mode,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
