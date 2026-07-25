from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..context import ScenarioContext
from ..firestore_api import FirestoreDataAdapter
from ..models import ScenarioResult, utc_now_iso
from ..scenario import BaseScenario


WEEKDAY_INDEX = {
    "Mon": 0,
    "Tue": 1,
    "Wed": 2,
    "Thu": 3,
    "Fri": 4,
    "Sat": 5,
    "Sun": 6,
}
DAILY_CALL_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEDULE_DATABASE_ID = "development"
DAILY_CALL_DATABASE_ID = SCHEDULE_DATABASE_ID
DAILY_CALL_PRESERVED_FIELDS = (
    "times",
    "billing",
    "lastConsumedLocalDate",
    "characterId",
    "characterSource",
    "nickname",
    "relationship",
    "purpose",
    "callPreference",
    "retryAt",
    "retryCount",
    "compensationStatus",
)


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(
            f"Expected an ISO timestamp or datetime, got {type(value).__name__}"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _next_weekly_occurrence_utc(
    schedule: dict,
    timezone_name: str,
    *,
    after_utc: datetime,
) -> datetime:
    zone = ZoneInfo(timezone_name)
    time_local = str(schedule.get("timeLocal") or "")
    hour, minute = (int(part) for part in time_local.split(":", 1))
    day_codes = schedule.get("days") or []
    weekdays = {WEEKDAY_INDEX[code] for code in day_codes}
    if not weekdays:
        raise ValueError("Weekly smoke schedules need at least one weekday")

    local_after = after_utc.astimezone(zone)
    for offset in range(0, 8):
        candidate_date: date = local_after.date() + timedelta(days=offset)
        if candidate_date.weekday() not in weekdays:
            continue
        candidate = datetime.combine(
            candidate_date,
            time(hour=hour, minute=minute),
            tzinfo=zone,
        ).astimezone(timezone.utc)
        if candidate > after_utc:
            return candidate
    raise ValueError("Could not find a weekly occurrence in the next seven days")


def _next_daily_call_occurrence_utc(
    times: dict[str, str],
    timezone_name: str,
    *,
    after_utc: datetime,
) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_after = after_utc.astimezone(zone)
    candidates = []
    for offset in range(0, 8):
        candidate_date = local_after.date() + timedelta(days=offset)
        value = times.get(DAILY_CALL_DAY_KEYS[candidate_date.weekday()])
        if not value:
            continue
        hour, minute = (int(part) for part in value.split(":", 1))
        candidate = datetime.combine(
            candidate_date,
            time(hour=hour, minute=minute),
            tzinfo=zone,
        ).astimezone(timezone.utc)
        if candidate > after_utc:
            candidates.append(candidate)
    if not candidates:
        raise ValueError("Could not find a Daily Call occurrence in seven days")
    return min(candidates)


def _recalculation_state(
    adapter: FirestoreDataAdapter,
    expected_by_path: dict[str, datetime],
    expected_schedules: dict[str, dict] | None = None,
    expected_triggers: dict[str, datetime] | None = None,
) -> dict | None:
    final_docs = {}
    for path, expected in expected_by_path.items():
        doc = adapter.get_document(path)
        if not doc:
            return None
        if doc.get("status") != "on":
            return None
        if doc.get("lastProcessedUTC") or doc.get("lastDelivered"):
            return None
        if expected_schedules is not None:
            actual_schedule = doc.get("schedule") or {}
            expected_schedule = expected_schedules[path]
            for field in ("repeat", "timeLocal", "days", "dateLocal"):
                if actual_schedule.get(field) != expected_schedule.get(field):
                    return None
        try:
            actual = _as_utc(doc.get("nextOccurrenceUTC"))
        except (TypeError, ValueError):
            return None
        if actual != expected:
            return None
        if expected_triggers is not None:
            try:
                actual_trigger = _as_utc(doc.get("nextTriggerUTC"))
            except (TypeError, ValueError):
                return None
            if actual_trigger != expected_triggers[path]:
                return None
        final_docs[path] = doc
    return final_docs


def _validate_local_isolated_environment(context: ScenarioContext) -> None:
    environment = context.environment
    if (
        environment.environment_type != "local-compose"
        or environment.data_mode != "isolated"
    ):
        raise RuntimeError(
            "scheduled.timezone_recalculation is local-only and requires "
            "environment_type=local-compose with data_mode=isolated"
        )
    emulator_host = os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip()
    if not emulator_host:
        raise RuntimeError(
            "scheduled.timezone_recalculation requires FIRESTORE_EMULATOR_HOST; "
            "it will not run against a cloud Firestore database"
        )
    if not environment.project.startswith("demo-"):
        raise RuntimeError(
            "scheduled.timezone_recalculation requires a demo-* project ID as an "
            "additional guard against cloud writes"
        )


async def _wait_for_recalculation(
    adapter: FirestoreDataAdapter,
    expected_by_path: dict[str, datetime],
    expected_schedules: dict[str, dict],
    expected_triggers: dict[str, datetime],
    *,
    timeout_seconds: int,
) -> dict | None:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    while datetime.now(timezone.utc) < deadline:
        state = _recalculation_state(
            adapter,
            expected_by_path,
            expected_schedules,
            expected_triggers,
        )
        if state:
            return state
        await asyncio.sleep(0.25)
    return None


async def _wait_for_daily_call_recalculation(
    adapter: FirestoreDataAdapter,
    path: str,
    expected: datetime,
    preserved_fields: dict,
    *,
    timeout_seconds: int,
) -> dict | None:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    while datetime.now(timezone.utc) < deadline:
        document = adapter.get_document(path)
        if document:
            try:
                actual = _as_utc(document.get("nextOccurrenceUTC"))
            except (TypeError, ValueError):
                actual = None
            if (
                actual == expected
                and document.get("status") == "on"
                and all(
                    document.get(field) == value
                    for field, value in preserved_fields.items()
                )
                and not document.get("lastClaimedOccurrenceUTC")
                and not document.get("lastMissedOccurrenceUTC")
                and not document.get("lastBlockedDuplicateOccurrenceUTC")
                and not document.get("dispatchedAt")
            ):
                return document
        await asyncio.sleep(0.25)
    return None


class ScheduledTimezoneRecalculationScenario(BaseScenario):
    name = "scheduled.timezone_recalculation"
    description = (
        "Change an emulator user's timezone and verify recurring reminder/alarm "
        "UTC cursors are rebased while their local wall-clock schedules stay "
        "unchanged"
    )

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        _validate_local_isolated_environment(context)
        started = utc_now_iso()
        args = context.args

        try:
            old_zone = ZoneInfo(args.from_timezone)
            ZoneInfo(args.to_timezone)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(f"Unknown IANA timezone: {exc.args[0]}") from exc
        if args.from_timezone == args.to_timezone:
            raise RuntimeError(
                "--from-timezone and --to-timezone must be different"
            )

        adapter = FirestoreDataAdapter(
            context.firestore_for(SCHEDULE_DATABASE_ID),
        )
        previous_user = adapter.get_user(args.uid)
        if previous_user is not None:
            raise RuntimeError(
                "Timezone smoke requires a fresh synthetic UID in the isolated "
                "emulator; choose an unused --uid"
            )
        timezone_was_present = bool(
            previous_user is not None and "timezone" in previous_user
        )
        previous_timezone = (
            previous_user.get("timezone") if previous_user else None
        )
        label = args.label or (
            f"timezone recalculation smoke {datetime.now().strftime('%H%M%S')}"
        )

        now_utc = datetime.now(timezone.utc)
        target_date = now_utc.astimezone(old_zone).date() + timedelta(days=2)
        target_local = datetime.combine(
            target_date,
            time(hour=9, minute=17),
            tzinfo=old_zone,
        )
        initial_due_utc = target_local.astimezone(timezone.utc)
        created_docs = []

        try:
            adapter.set_user_timezone(args.uid, args.from_timezone)
            reminder = adapter.create_reminder(
                uid=args.uid,
                device_id=None,
                label=f"{label} reminder",
                due_utc=initial_due_utc,
                repeat="weekly",
                user_timezone=args.from_timezone,
                channel="app",
            )
            created_docs.append(reminder)
            alarm = adapter.create_alarm(
                uid=args.uid,
                device_id=args.device_id or "smoke-emulator-device",
                label=f"{label} alarm",
                due_utc=initial_due_utc,
                repeat="weekly",
                user_timezone=args.from_timezone,
            )
            created_docs.append(alarm)

            changed_at = datetime.now(timezone.utc)
            expected_by_path = {
                created.path: _next_weekly_occurrence_utc(
                    created.payload["schedule"],
                    args.to_timezone,
                    after_utc=changed_at,
                )
                for created in created_docs
            }
            expected_schedules = {
                created.path: created.payload["schedule"]
                for created in created_docs
            }
            expected_triggers = {
                path: occurrence - timedelta(minutes=30)
                for path, occurrence in expected_by_path.items()
            }
            if any(
                expected == _as_utc(created.payload["nextOccurrenceUTC"])
                for created, expected in (
                    (created, expected_by_path[created.path])
                    for created in created_docs
                )
            ):
                raise RuntimeError(
                    "Chosen timezone pair does not change the seeded UTC cursor; "
                    "use zones with different offsets"
                )

            adapter.set_user_timezone(args.uid, args.to_timezone)
            final_docs = await _wait_for_recalculation(
                adapter,
                expected_by_path,
                expected_schedules,
                expected_triggers,
                timeout_seconds=args.timeout_seconds,
            )

            details = {
                "environment": {
                    "environmentType": context.environment.environment_type,
                    "dataMode": context.environment.data_mode,
                    "project": context.environment.project,
                    "database": SCHEDULE_DATABASE_ID,
                    "firestoreEmulatorHost": os.environ.get(
                        "FIRESTORE_EMULATOR_HOST"
                    ),
                },
                "user": {
                    "uid": args.uid,
                    "fromTimezone": args.from_timezone,
                    "toTimezone": args.to_timezone,
                },
                "timezoneChangedAt": changed_at.isoformat(),
                "documents": [
                    {
                        "path": created.path,
                        "schedule": created.payload["schedule"],
                        "initialNextOccurrenceUTC": (
                            created.payload["nextOccurrenceUTC"]
                        ),
                        "initialNextTriggerUTC": (
                            created.payload["nextTriggerUTC"]
                        ),
                        "expectedNextOccurrenceUTC": (
                            expected_by_path[created.path].isoformat()
                        ),
                        "finalSchedule": (
                            final_docs[created.path].get("schedule")
                            if final_docs
                            else (
                                adapter.get_document(created.path) or {}
                            ).get("schedule")
                        ),
                        "finalNextOccurrenceUTC": (
                            final_docs[created.path].get("nextOccurrenceUTC")
                            if final_docs
                            else (
                                adapter.get_document(created.path) or {}
                            ).get("nextOccurrenceUTC")
                        ),
                        "expectedNextTriggerUTC": (
                            expected_triggers[created.path].isoformat()
                        ),
                        "finalNextTriggerUTC": (
                            final_docs[created.path].get("nextTriggerUTC")
                            if final_docs
                            else (
                                adapter.get_document(created.path) or {}
                            ).get("nextTriggerUTC")
                        ),
                    }
                    for created in created_docs
                ],
            }
            context.artifact_writer.write_json(
                "scenario-details.json",
                details,
                context.artifact_dir,
            )

            if not final_docs:
                raise RuntimeError(
                    "Timezone worker did not rebase both recurring UTC cursors "
                    "before timeout"
                )

            return ScenarioResult(
                name=self.name,
                success=True,
                started_at=started,
                finished_at=utc_now_iso(),
                summary=(
                    "Recurring reminder and alarm UTC cursors were recalculated "
                    f"after {args.from_timezone} -> {args.to_timezone}"
                ),
                artifact_dir=str(context.artifact_dir),
                details=details,
            )
        finally:
            if not args.keep_docs:
                for created in created_docs:
                    adapter.delete_path(created.path)
                adapter.restore_user_timezone(
                    args.uid,
                    user_existed=previous_user is not None,
                    timezone_was_present=timezone_was_present,
                    timezone_value=previous_timezone,
                )


class ScheduledDailyCallTimezoneRecalculationScenario(BaseScenario):
    name = "scheduled.daily_call_timezone_recalculation"
    description = (
        "Change a phone-keyed emulator user's timezone and verify the Daily Call "
        "UTC cursor is rebased without consuming or dispatching a call"
    )

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        _validate_local_isolated_environment(context)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", context.args.uid):
            raise RuntimeError(
                "Daily Call timezone smoke requires an E.164 --uid"
            )

        started = utc_now_iso()
        args = context.args
        try:
            ZoneInfo(args.from_timezone)
            ZoneInfo(args.to_timezone)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(f"Unknown IANA timezone: {exc.args[0]}") from exc
        if args.from_timezone == args.to_timezone:
            raise RuntimeError(
                "--from-timezone and --to-timezone must be different"
            )

        adapter = FirestoreDataAdapter(
            context.firestore_for(DAILY_CALL_DATABASE_ID),
        )
        previous_user = adapter.get_user(args.uid)
        timezone_was_present = bool(
            previous_user is not None and "timezone" in previous_user
        )
        previous_timezone = (
            previous_user.get("timezone") if previous_user else None
        )
        times = {day: "09:17" for day in DAILY_CALL_DAY_KEYS}
        now_utc = datetime.now(timezone.utc)
        initial_due = _next_daily_call_occurrence_utc(
            times,
            args.from_timezone,
            after_utc=now_utc,
        )
        created = None
        daily_call_path = f"users/{args.uid}/miluCall/dailyCall"
        previous_daily_call = adapter.get_document(daily_call_path)
        if previous_user is not None or previous_daily_call is not None:
            raise RuntimeError(
                "Daily Call timezone smoke requires a fresh synthetic E.164 "
                "fixture; choose an unused --uid"
            )

        try:
            adapter.set_user_timezone(args.uid, args.from_timezone)
            created = adapter.create_daily_call(
                phone_number=args.uid,
                due_utc=initial_due,
                times=times,
            )
            preserved_fields = {
                field: created.payload[field]
                for field in DAILY_CALL_PRESERVED_FIELDS
            }
            changed_at = datetime.now(timezone.utc)
            expected = _next_daily_call_occurrence_utc(
                times,
                args.to_timezone,
                after_utc=changed_at,
            )
            if expected == initial_due:
                raise RuntimeError(
                    "Chosen timezone pair does not change the Daily Call UTC cursor"
                )

            adapter.set_user_timezone(args.uid, args.to_timezone)
            final_document = await _wait_for_daily_call_recalculation(
                adapter,
                created.path,
                expected,
                preserved_fields,
                timeout_seconds=args.timeout_seconds,
            )
            details = {
                "environment": {
                    "environmentType": context.environment.environment_type,
                    "dataMode": context.environment.data_mode,
                    "project": context.environment.project,
                    "database": DAILY_CALL_DATABASE_ID,
                    "firestoreEmulatorHost": os.environ.get(
                        "FIRESTORE_EMULATOR_HOST"
                    ),
                },
                "user": {
                    "phoneNumber": args.uid,
                    "fromTimezone": args.from_timezone,
                    "toTimezone": args.to_timezone,
                },
                "document": {
                    "path": created.path,
                    "times": times,
                    "preservedFields": preserved_fields,
                    "initialNextOccurrenceUTC": initial_due.isoformat(),
                    "expectedNextOccurrenceUTC": expected.isoformat(),
                    "finalNextOccurrenceUTC": (
                        final_document.get("nextOccurrenceUTC")
                        if final_document
                        else (
                            adapter.get_document(created.path) or {}
                        ).get("nextOccurrenceUTC")
                    ),
                },
            }
            context.artifact_writer.write_json(
                "scenario-details.json",
                details,
                context.artifact_dir,
            )
            if not final_document:
                raise RuntimeError(
                    "Timezone worker did not rebase Daily Call before timeout"
                )
            return ScenarioResult(
                name=self.name,
                success=True,
                started_at=started,
                finished_at=utc_now_iso(),
                summary=(
                    "Daily Call UTC cursor was recalculated without a claim or "
                    f"dispatch after {args.from_timezone} -> {args.to_timezone}"
                ),
                artifact_dir=str(context.artifact_dir),
                details=details,
            )
        finally:
            if not args.keep_docs:
                adapter.restore_document(
                    daily_call_path,
                    previous_daily_call,
                )
                adapter.restore_user_timezone(
                    args.uid,
                    user_existed=previous_user is not None,
                    timezone_was_present=timezone_was_present,
                    timezone_value=previous_timezone,
                )
