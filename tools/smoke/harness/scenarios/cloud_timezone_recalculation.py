from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from ..context import ScenarioContext
from ..firestore_api import FirestoreDataAdapter
from ..models import ScenarioResult, utc_now_iso
from ..scenario import BaseScenario
from .timezone_recalculation import (
    DAILY_CALL_PRESERVED_FIELDS,
    DEFAULT_DATABASE_ID,
    DEVELOPMENT_DATABASE_ID,
    WEEKDAY_INDEX,
    _as_utc,
    _next_daily_call_occurrence_utc,
    _next_weekly_occurrence_utc,
)


SCENARIO_NAME = "scheduled.cloud_timezone_worker_recalculation"
EXPECTED_PROJECT_ID = "composed-augury-469200-g6"
EXPECTED_REGION = "us-central1"
EXPECTED_TRIGGER_LOCATION = "nam5"
DEVELOPMENT_UID = "codex-timezone-live-smoke-20260726"
DEFAULT_PHONE_UID = "+15550003333"
CONFIRMATION_VALUE = "RUN_LIVE_TIMEZONE_WORKER_SMOKE_20260726"

DEVELOPMENT_USER_PATH = f"users/{DEVELOPMENT_UID}"
DEVELOPMENT_REMINDER_PATH = (
    f"{DEVELOPMENT_USER_PATH}/reminders/"
    "codex-timezone-live-smoke-reminder-20260726"
)
DEVELOPMENT_SCHEDULE_PATH = (
    f"{DEVELOPMENT_USER_PATH}/schedules/"
    "codex-timezone-live-smoke-schedule-20260726"
)
DEFAULT_USER_PATH = f"users/{DEFAULT_PHONE_UID}"
DEFAULT_DAILY_CALL_PATH = f"{DEFAULT_USER_PATH}/miluCall/dailyCall"

TARGET_PATHS_BY_DATABASE = {
    DEVELOPMENT_DATABASE_ID: (
        DEVELOPMENT_USER_PATH,
        DEVELOPMENT_REMINDER_PATH,
        DEVELOPMENT_SCHEDULE_PATH,
    ),
    DEFAULT_DATABASE_ID: (
        DEFAULT_USER_PATH,
        DEFAULT_DAILY_CALL_PATH,
    ),
}

WORKER_CONTRACTS = {
    DEVELOPMENT_DATABASE_ID: {
        "function": "user-timezone-schedule-worker-development",
        "runtimeServiceAccount": (
            "babymilu-tz-runtime-dev@"
            "composed-augury-469200-g6.iam.gserviceaccount.com"
        ),
        "triggerServiceAccount": (
            "babymilu-tz-trigger-dev@"
            "composed-augury-469200-g6.iam.gserviceaccount.com"
        ),
        "legacyDatabase": DEFAULT_DATABASE_ID,
    },
    DEFAULT_DATABASE_ID: {
        "function": "user-timezone-schedule-worker-default",
        "runtimeServiceAccount": (
            "babymilu-tz-runtime-default@"
            "composed-augury-469200-g6.iam.gserviceaccount.com"
        ),
        "triggerServiceAccount": (
            "babymilu-tz-trigger-default@"
            "composed-augury-469200-g6.iam.gserviceaccount.com"
        ),
        "legacyDatabase": "",
    },
}
EXPECTED_BUILD_SERVICE_ACCOUNT = (
    "projects/composed-augury-469200-g6/serviceAccounts/"
    "babymilu-tz-build@composed-augury-469200-g6.iam.gserviceaccount.com"
)


def _validate_live_cloud_environment(context: ScenarioContext) -> None:
    environment = context.environment
    args = context.args
    if (
        environment.environment_type != "cloud"
        or environment.data_mode != "live-shape"
    ):
        raise RuntimeError(
            f"{SCENARIO_NAME} requires environment_type=cloud with "
            "data_mode=live-shape"
        )
    if environment.project != EXPECTED_PROJECT_ID:
        raise RuntimeError(
            f"{SCENARIO_NAME} is pinned to project {EXPECTED_PROJECT_ID}"
        )
    if os.environ.get("FIRESTORE_EMULATOR_HOST", "").strip():
        raise RuntimeError(
            f"{SCENARIO_NAME} refuses FIRESTORE_EMULATOR_HOST"
        )
    if args.uid != DEVELOPMENT_UID:
        raise RuntimeError(
            f"{SCENARIO_NAME} requires --uid {DEVELOPMENT_UID}"
        )
    if args.confirm_live_timezone_smoke != CONFIRMATION_VALUE:
        raise RuntimeError(
            f"{SCENARIO_NAME} requires --confirm-live-timezone-smoke "
            f"{CONFIRMATION_VALUE}"
        )
    if args.keep_docs:
        raise RuntimeError(
            f"{SCENARIO_NAME} always cleans up and refuses --keep-docs"
        )
    if args.skip_preflight:
        raise RuntimeError(
            f"{SCENARIO_NAME} refuses --skip-preflight"
        )


def _run_gcloud_json(arguments: list[str]) -> dict:
    command = ["gcloud", *arguments, "--format=json"]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode:
        stderr = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"Cloud contract check failed for {' '.join(command[:5])}: "
            f"{stderr[:500]}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Cloud contract check returned invalid JSON: {command[:5]}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Cloud contract check returned a non-object: {command[:5]}"
        )
    return payload


def _event_filter_value(function: dict, attribute: str) -> str:
    filters = (function.get("eventTrigger") or {}).get("eventFilters") or []
    if isinstance(filters, dict):
        return str(filters.get(attribute) or "")
    for item in filters:
        if (
            isinstance(item, dict)
            and item.get("attribute") == attribute
        ):
            return str(item.get("value") or "")
    return ""


def _invoker_members(policy: dict) -> set[str]:
    members: set[str] = set()
    for binding in policy.get("bindings") or []:
        if (
            isinstance(binding, dict)
            and binding.get("role") == "roles/run.invoker"
        ):
            members.update(
                str(member)
                for member in binding.get("members") or []
            )
    return members


def _probe_unauthenticated(uri: str) -> int:
    response = requests.post(
        uri,
        json={},
        timeout=20,
        allow_redirects=False,
    )
    return response.status_code


def _validate_worker_contracts() -> dict[str, dict]:
    summaries: dict[str, dict] = {}
    project_policy = _run_gcloud_json(
        [
            "projects",
            "get-iam-policy",
            EXPECTED_PROJECT_ID,
        ]
    )
    project_invokers = _invoker_members(project_policy)
    public_project_invokers = {
        "allUsers",
        "allAuthenticatedUsers",
    } & project_invokers
    if public_project_invokers:
        raise RuntimeError(
            "Public project-level roles/run.invoker is forbidden for the "
            "timezone worker rollout; "
            f"observed={sorted(public_project_invokers)}"
        )
    for database_id, expected in WORKER_CONTRACTS.items():
        function_name = expected["function"]
        function = _run_gcloud_json(
            [
                "functions",
                "describe",
                function_name,
                "--gen2",
                f"--project={EXPECTED_PROJECT_ID}",
                f"--region={EXPECTED_REGION}",
            ]
        )
        service_config = function.get("serviceConfig") or {}
        build_config = function.get("buildConfig") or {}
        event_trigger = function.get("eventTrigger") or {}
        environment_variables = (
            service_config.get("environmentVariables") or {}
        )
        assertions = {
            "state": function.get("state") == "ACTIVE",
            "runtimeServiceAccount": (
                service_config.get("serviceAccountEmail")
                == expected["runtimeServiceAccount"]
            ),
            "buildServiceAccount": (
                build_config.get("serviceAccount")
                == EXPECTED_BUILD_SERVICE_ACCOUNT
            ),
            "triggerServiceAccount": (
                event_trigger.get("serviceAccountEmail")
                == expected["triggerServiceAccount"]
            ),
            "triggerRegion": (
                event_trigger.get("triggerRegion")
                == EXPECTED_TRIGGER_LOCATION
            ),
            "eventType": (
                event_trigger.get("eventType")
                == "google.cloud.firestore.document.v1.updated"
            ),
            "databaseFilter": (
                _event_filter_value(function, "database") == database_id
            ),
            "documentFilter": (
                _event_filter_value(function, "document") == "users/{uid}"
            ),
            "runtimeProject": (
                environment_variables.get("FIRESTORE_PROJECT_ID")
                == EXPECTED_PROJECT_ID
            ),
            "runtimeDatabase": (
                environment_variables.get("FIRESTORE_DATABASE_ID")
                == database_id
            ),
            "legacyDatabase": (
                environment_variables.get(
                    "LEGACY_DAILY_CALL_DATABASE_ID",
                    "",
                )
                == expected["legacyDatabase"]
            ),
        }
        failed = sorted(
            name for name, passed in assertions.items() if not passed
        )
        if failed:
            raise RuntimeError(
                f"{function_name} failed deployed contract checks: "
                + ", ".join(failed)
            )

        policy = _run_gcloud_json(
            [
                "run",
                "services",
                "get-iam-policy",
                function_name,
                f"--project={EXPECTED_PROJECT_ID}",
                f"--region={EXPECTED_REGION}",
            ]
        )
        expected_invoker = (
            f"serviceAccount:{expected['triggerServiceAccount']}"
        )
        invokers = _invoker_members(policy)
        if invokers != {expected_invoker}:
            raise RuntimeError(
                f"{function_name} must have exactly one Run invoker: "
                f"{expected_invoker}; observed={sorted(invokers)}"
            )
        service_uri = str(service_config.get("uri") or "").strip()
        if not service_uri:
            raise RuntimeError(
                f"{function_name} has no deployed service URI"
            )
        unauthenticated_status = _probe_unauthenticated(service_uri)
        if unauthenticated_status not in {401, 403}:
            raise RuntimeError(
                f"{function_name} accepted or unexpectedly handled an "
                "unauthenticated HTTP probe; "
                f"status={unauthenticated_status}"
            )

        summaries[database_id] = {
            "function": function_name,
            "state": function.get("state"),
            "runtimeServiceAccount": service_config.get(
                "serviceAccountEmail"
            ),
            "buildServiceAccount": build_config.get("serviceAccount"),
            "triggerServiceAccount": event_trigger.get(
                "serviceAccountEmail"
            ),
            "triggerRegion": event_trigger.get("triggerRegion"),
            "eventType": event_trigger.get("eventType"),
            "databaseFilter": _event_filter_value(function, "database"),
            "documentFilter": _event_filter_value(function, "document"),
            "runInvokers": sorted(invokers),
            "projectRunInvokers": sorted(project_invokers),
            "additionalProjectNamedInvokersPresent": bool(
                project_invokers
            ),
            "publicInvokerPresent": bool(
                {"allUsers", "allAuthenticatedUsers"} & invokers
            ),
            "unauthenticatedHttpStatus": unauthenticated_status,
            "legacyDatabase": environment_variables.get(
                "LEGACY_DAILY_CALL_DATABASE_ID",
                "",
            ),
        }
    return summaries


def _document_digest(document: dict) -> str:
    encoded = json.dumps(
        document,
        default=str,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _fixture_marker(run_id: str) -> dict:
    return {
        "scenario": SCENARIO_NAME,
        "fixtureVersion": 1,
        "runId": run_id,
        "disposable": True,
    }


def _is_smoke_owned(document: dict) -> bool:
    marker = document.get("_smokeFixture")
    return bool(
        isinstance(marker, dict)
        and marker.get("scenario") == SCENARIO_NAME
        and marker.get("fixtureVersion") == 1
        and marker.get("disposable") is True
    )


def _capture_fixture_state(
    adapters: dict[str, FirestoreDataAdapter],
) -> dict[str, dict]:
    state: dict[str, dict] = {}
    identities = {
        DEVELOPMENT_DATABASE_ID: (
            DEVELOPMENT_UID,
            DEVELOPMENT_USER_PATH,
        ),
        DEFAULT_DATABASE_ID: (
            DEFAULT_PHONE_UID,
            DEFAULT_USER_PATH,
        ),
    }
    for database_id, (uid, user_path) in identities.items():
        adapter = adapters[database_id]
        user = adapter.get_document(user_path)
        descendants = adapter.list_descendant_documents(user_path)
        legacy_documents = adapter.find_legacy_schedule_documents(uid)
        state[database_id] = {
            "user": user,
            "descendants": descendants,
            "legacyDocuments": legacy_documents,
        }
    return state


def _snapshot_summary(state: dict[str, dict]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for database_id, database_state in state.items():
        user = database_state["user"]
        descendants = database_state["descendants"]
        legacy_documents = database_state["legacyDocuments"]
        summary[database_id] = {
            "user": {
                "exists": user is not None,
                "smokeOwned": bool(user and _is_smoke_owned(user)),
                "fields": sorted(user) if user else [],
                "sha256": _document_digest(user) if user else None,
            },
            "descendants": {
                path: {
                    "smokeOwned": _is_smoke_owned(document),
                    "fields": sorted(document),
                    "sha256": _document_digest(document),
                }
                for path, document in sorted(descendants.items())
            },
            "legacyDocuments": {
                path: {
                    "smokeOwned": _is_smoke_owned(document),
                    "fields": sorted(document),
                    "sha256": _document_digest(document),
                }
                for path, document in sorted(legacy_documents.items())
            },
        }
    return summary


def _assert_fixture_unoccupied(state: dict[str, dict]) -> None:
    occupied: list[tuple[str, str, bool]] = []
    for database_id, database_state in state.items():
        user = database_state["user"]
        if user is not None:
            occupied.append(
                (
                    database_id,
                    TARGET_PATHS_BY_DATABASE[database_id][0],
                    _is_smoke_owned(user),
                )
            )
        for group_name in ("descendants", "legacyDocuments"):
            for path, document in database_state[group_name].items():
                occupied.append(
                    (database_id, path, _is_smoke_owned(document))
                )
    if not occupied:
        return
    non_smoke = [
        f"{database}:{path}"
        for database, path, smoke_owned in occupied
        if not smoke_owned
    ]
    if non_smoke:
        raise RuntimeError(
            "Refusing live timezone smoke because pre-existing non-smoke "
            "data could be touched: "
            + ", ".join(sorted(non_smoke))
        )
    stale = [
        f"{database}:{path}"
        for database, path, _ in occupied
    ]
    raise RuntimeError(
        "Refusing live timezone smoke because a stale smoke fixture already "
        "occupies the exact allowlist: "
        + ", ".join(sorted(stale))
    )


def _assert_timezone_pair(from_timezone: str, to_timezone: str) -> None:
    try:
        ZoneInfo(from_timezone)
        ZoneInfo(to_timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown IANA timezone: {exc.args[0]}") from exc
    if from_timezone == to_timezone:
        raise RuntimeError("Timezone smoke requires distinct IANA timezones")


def _schedule_payload(
    *,
    label: str,
    schedule: dict,
    initial_due: datetime,
    timezone_name: str,
    marker: dict,
) -> dict:
    return {
        "_smokeFixture": marker,
        "label": label,
        "uid": DEVELOPMENT_UID,
        "status": "on",
        "deliveryChannel": ["app"],
        "targets": [],
        "schedule": schedule,
        "timezoneAtCalculation": timezone_name,
        "nextOccurrenceUTC": initial_due.isoformat(),
        "nextTriggerUTC": (initial_due - timedelta(minutes=30)).isoformat(),
        "createdAt": datetime.now(timezone.utc),
        "updatedAt": datetime.now(timezone.utc),
    }


def _daily_call_payload(
    *,
    times: dict[str, str],
    initial_due: datetime,
    timezone_name: str,
    marker: dict,
) -> dict:
    return {
        "_smokeFixture": marker,
        "schemaVersion": 1,
        "kind": "daily_call",
        "status": "on",
        "times": dict(times),
        "nextOccurrenceUTC": initial_due.isoformat(),
        "timezoneAtCalculation": timezone_name,
        "billing": {
            "paidCallsRemaining": 3,
            "paidCallsConsumed": 2,
        },
        "lastConsumedLocalDate": "2026-01-01",
        "characterId": "smoke-character",
        "characterSource": "preset",
        "nickname": "Smoke Friend",
        "relationship": "friend",
        "purpose": "A deterministic live timezone worker smoke",
        "callPreference": "gentle",
        "retryCount": 0,
        "compensationStatus": "resolved",
        "createdAt": datetime.now(timezone.utc),
        "updatedAt": datetime.now(timezone.utc),
    }


def _schedule_document_matches(
    document: dict | None,
    *,
    expected_occurrence: datetime,
    expected_trigger: datetime,
    expected_schedule: dict,
    from_timezone: str,
    to_timezone: str,
    changed_after: datetime,
) -> bool:
    if not document or not _is_smoke_owned(document):
        return False
    if document.get("status") != "on":
        return False
    if any(
        document.get(field)
        for field in (
            "lastProcessedUTC",
            "lastDelivered",
            "lastDeliveredUTC",
            "dispatchedAt",
        )
    ):
        return False
    if document.get("schedule") != expected_schedule:
        return False
    try:
        if _as_utc(document.get("nextOccurrenceUTC")) != expected_occurrence:
            return False
        if _as_utc(document.get("nextTriggerUTC")) != expected_trigger:
            return False
    except (TypeError, ValueError):
        return False
    if document.get("timezoneAtCalculation") != to_timezone:
        return False
    metadata = document.get("timezoneRecalculation")
    if not isinstance(metadata, dict):
        return False
    if not all(
        (
            metadata.get("schemaVersion") == 1,
            bool(str(metadata.get("eventId") or "").strip()),
            metadata.get("status") == "recalculated",
            metadata.get("fromTimezone") == from_timezone,
            metadata.get("toTimezone") == to_timezone,
            bool(str(metadata.get("sourceUpdateTime") or "").strip()),
        )
    ):
        return False
    try:
        return _as_utc(metadata.get("recalculatedAt")) >= changed_after
    except (TypeError, ValueError):
        return False


def _bridge_document_matches(
    document: dict | None,
    *,
    expected_timezone: str,
    changed_after: datetime,
) -> bool:
    if not document or not _is_smoke_owned(document):
        return False
    if document.get("uid") != DEVELOPMENT_UID:
        return False
    if document.get("timezone") != expected_timezone:
        return False
    metadata = document.get("timezoneScheduleSync")
    if not isinstance(metadata, dict):
        return False
    if not all(
        (
            metadata.get("schemaVersion") == 1,
            bool(str(metadata.get("sourceEventId") or "").strip()),
            bool(str(metadata.get("sourceUpdateTime") or "").strip()),
            metadata.get("sourceUid") == DEVELOPMENT_UID,
            metadata.get("sourceDatabase") == DEVELOPMENT_DATABASE_ID,
        )
    ):
        return False
    try:
        return _as_utc(metadata.get("syncedAt")) >= changed_after
    except (TypeError, ValueError):
        return False


def _daily_call_document_matches(
    document: dict | None,
    *,
    expected_occurrence: datetime,
    expected_preserved_fields: dict,
    from_timezone: str,
    to_timezone: str,
    changed_after: datetime,
    previous_event_id: str = "",
) -> bool:
    if not document or not _is_smoke_owned(document):
        return False
    if document.get("status") != "on":
        return False
    if any(
        document.get(field)
        for field in (
            "lastClaimedOccurrenceUTC",
            "lastMissedOccurrenceUTC",
            "lastBlockedDuplicateOccurrenceUTC",
            "dispatchedAt",
            "lastDelivered",
        )
    ):
        return False
    if any(
        document.get(field) != value
        for field, value in expected_preserved_fields.items()
    ):
        return False
    try:
        if _as_utc(document.get("nextOccurrenceUTC")) != expected_occurrence:
            return False
    except (TypeError, ValueError):
        return False
    if document.get("timezoneAtCalculation") != to_timezone:
        return False
    metadata = document.get("timezoneRecalculation")
    if not isinstance(metadata, dict):
        return False
    event_id = str(metadata.get("eventId") or "").strip()
    if not all(
        (
            metadata.get("schemaVersion") == 1,
            bool(event_id),
            event_id != previous_event_id,
            metadata.get("oldTimezone") == from_timezone,
            metadata.get("newTimezone") == to_timezone,
            metadata.get("scheduleType") == "daily_call",
            metadata.get("outcome") == "recalculated",
            bool(str(metadata.get("sourceUpdateTime") or "").strip()),
        )
    ):
        return False
    try:
        return _as_utc(metadata.get("recalculatedAt")) >= changed_after
    except (TypeError, ValueError):
        return False


async def _wait_for_bridge_flow(
    *,
    development: FirestoreDataAdapter,
    default: FirestoreDataAdapter,
    expected_schedules: dict[str, dict],
    expected_occurrences: dict[str, datetime],
    expected_triggers: dict[str, datetime],
    expected_daily_call_occurrence: datetime,
    expected_daily_call_preserved_fields: dict,
    from_timezone: str,
    to_timezone: str,
    changed_after: datetime,
    timeout_seconds: int,
) -> dict | None:
    deadline = datetime.now(timezone.utc) + timedelta(
        seconds=timeout_seconds
    )
    while datetime.now(timezone.utc) < deadline:
        development_docs = {
            path: development.get_document(path)
            for path in expected_schedules
        }
        default_user = default.get_document(DEFAULT_USER_PATH)
        daily_call = default.get_document(DEFAULT_DAILY_CALL_PATH)
        schedules_match = all(
            _schedule_document_matches(
                development_docs[path],
                expected_occurrence=expected_occurrences[path],
                expected_trigger=expected_triggers[path],
                expected_schedule=expected_schedules[path],
                from_timezone=from_timezone,
                to_timezone=to_timezone,
                changed_after=changed_after,
            )
            for path in expected_schedules
        )
        if (
            schedules_match
            and _bridge_document_matches(
                default_user,
                expected_timezone=to_timezone,
                changed_after=changed_after,
            )
            and _daily_call_document_matches(
                daily_call,
                expected_occurrence=expected_daily_call_occurrence,
                expected_preserved_fields=(
                    expected_daily_call_preserved_fields
                ),
                from_timezone=from_timezone,
                to_timezone=to_timezone,
                changed_after=changed_after,
            )
        ):
            return {
                "developmentDocuments": development_docs,
                "defaultUser": default_user,
                "dailyCall": daily_call,
            }
        await asyncio.sleep(1)
    return None


async def _wait_for_direct_default_flow(
    *,
    default: FirestoreDataAdapter,
    expected_occurrence: datetime,
    expected_preserved_fields: dict,
    from_timezone: str,
    to_timezone: str,
    changed_after: datetime,
    previous_event_id: str,
    timeout_seconds: int,
) -> dict | None:
    deadline = datetime.now(timezone.utc) + timedelta(
        seconds=timeout_seconds
    )
    while datetime.now(timezone.utc) < deadline:
        daily_call = default.get_document(DEFAULT_DAILY_CALL_PATH)
        default_user = default.get_document(DEFAULT_USER_PATH)
        if (
            default_user
            and default_user.get("timezone") == to_timezone
            and _daily_call_document_matches(
                daily_call,
                expected_occurrence=expected_occurrence,
                expected_preserved_fields=expected_preserved_fields,
                from_timezone=from_timezone,
                to_timezone=to_timezone,
                changed_after=changed_after,
                previous_event_id=previous_event_id,
            )
        ):
            return {
                "defaultUser": default_user,
                "dailyCall": daily_call,
            }
        await asyncio.sleep(1)
    return None


def _compact_schedule_result(document: dict) -> dict:
    return {
        "pathMarker": document.get("_smokeFixture"),
        "status": document.get("status"),
        "schedule": document.get("schedule"),
        "nextOccurrenceUTC": document.get("nextOccurrenceUTC"),
        "nextTriggerUTC": document.get("nextTriggerUTC"),
        "timezoneAtCalculation": document.get("timezoneAtCalculation"),
        "timezoneRecalculation": document.get("timezoneRecalculation"),
        "deliveryMarkers": {
            field: document.get(field)
            for field in (
                "lastProcessedUTC",
                "lastDelivered",
                "lastDeliveredUTC",
                "dispatchedAt",
            )
        },
    }


def _compact_daily_call_result(document: dict) -> dict:
    return {
        "pathMarker": document.get("_smokeFixture"),
        "status": document.get("status"),
        "times": document.get("times"),
        "nextOccurrenceUTC": document.get("nextOccurrenceUTC"),
        "timezoneAtCalculation": document.get("timezoneAtCalculation"),
        "timezoneRecalculation": document.get("timezoneRecalculation"),
        "deliveryMarkers": {
            field: document.get(field)
            for field in (
                "lastClaimedOccurrenceUTC",
                "lastMissedOccurrenceUTC",
                "lastBlockedDuplicateOccurrenceUTC",
                "dispatchedAt",
                "lastDelivered",
            )
        },
    }


async def _cleanup_and_verify(
    adapters: dict[str, FirestoreDataAdapter],
    *,
    marker: dict,
    timeout_seconds: int = 20,
) -> dict:
    deletion_errors: list[str] = []
    deletion_results: dict[str, str] = {}
    for database_id, paths in TARGET_PATHS_BY_DATABASE.items():
        adapter = adapters[database_id]
        for path in reversed(paths):
            try:
                result = adapter.delete_document_if_marker(
                    path,
                    marker=marker,
                )
                deletion_results[f"{database_id}:{path}"] = result
                if result == "ownership_changed":
                    deletion_errors.append(
                        f"{database_id}:{path}:ownership_changed"
                    )
            except Exception as exc:
                deletion_errors.append(
                    f"{database_id}:{path}:{type(exc).__name__}:{exc}"
                )

    deadline = datetime.now(timezone.utc) + timedelta(
        seconds=timeout_seconds
    )
    last_state: dict[str, dict] | None = None
    verification_errors: list[str] = []
    while datetime.now(timezone.utc) < deadline:
        try:
            last_state = _capture_fixture_state(adapters)
        except Exception as exc:
            verification_errors.append(
                f"{type(exc).__name__}:{exc}"
            )
            await asyncio.sleep(1)
            continue
        if all(
            database_state["user"] is None
            and not database_state["descendants"]
            and not database_state["legacyDocuments"]
            for database_state in last_state.values()
        ):
            return {
                "success": True,
                "verifiedAt": utc_now_iso(),
                "deletionResults": deletion_results,
                "deletionErrors": deletion_errors,
                "verificationErrors": verification_errors,
                "remaining": _snapshot_summary(last_state),
            }
        await asyncio.sleep(1)
    return {
        "success": False,
        "verifiedAt": utc_now_iso(),
        "deletionResults": deletion_results,
        "deletionErrors": deletion_errors,
        "verificationErrors": verification_errors,
        "remaining": (
            _snapshot_summary(last_state)
            if last_state is not None
            else None
        ),
    }


class ScheduledCloudTimezoneWorkerRecalculationScenario(BaseScenario):
    name = SCENARIO_NAME
    description = (
        "Against the deployed cloud workers, verify development schedule "
        "recalculation, the guarded development-to-default identity bridge, "
        "and direct default Daily Call recalculation using an exact disposable "
        "allowlist"
    )

    async def run(self, context: ScenarioContext) -> ScenarioResult:
        _validate_live_cloud_environment(context)
        started = utc_now_iso()
        args = context.args
        from_timezone = args.from_timezone
        bridge_timezone = args.to_timezone
        direct_default_timezone = args.direct_default_timezone
        _assert_timezone_pair(from_timezone, bridge_timezone)
        _assert_timezone_pair(bridge_timezone, direct_default_timezone)

        adapters = {
            DEVELOPMENT_DATABASE_ID: FirestoreDataAdapter(
                context.firestore_for(DEVELOPMENT_DATABASE_ID)
            ),
            DEFAULT_DATABASE_ID: FirestoreDataAdapter(
                context.firestore_for(DEFAULT_DATABASE_ID)
            ),
        }
        details: dict[str, Any] = {
            "environment": {
                "environmentType": context.environment.environment_type,
                "dataMode": context.environment.data_mode,
                "project": context.environment.project,
                "databases": [
                    DEVELOPMENT_DATABASE_ID,
                    DEFAULT_DATABASE_ID,
                ],
                "firestoreEmulatorHost": os.environ.get(
                    "FIRESTORE_EMULATOR_HOST"
                ),
            },
            "allowlist": {
                database: list(paths)
                for database, paths in TARGET_PATHS_BY_DATABASE.items()
            },
            "topLevelLegacyQueryGuard": {
                DEVELOPMENT_DATABASE_ID: DEVELOPMENT_UID,
                DEFAULT_DATABASE_ID: DEFAULT_PHONE_UID,
            },
        }

        deployed_contracts = _validate_worker_contracts()
        details["deployedContracts"] = deployed_contracts

        snapshots = _capture_fixture_state(adapters)
        snapshot_summary = _snapshot_summary(snapshots)
        details["preRunSnapshot"] = snapshot_summary
        context.artifact_writer.write_json(
            "pre-run-snapshot.json",
            snapshot_summary,
            context.artifact_dir,
        )
        _assert_fixture_unoccupied(snapshots)

        run_id = uuid.uuid4().hex
        marker = _fixture_marker(run_id)
        development = adapters[DEVELOPMENT_DATABASE_ID]
        default = adapters[DEFAULT_DATABASE_ID]

        old_zone = ZoneInfo(from_timezone)
        now_utc = datetime.now(timezone.utc)
        target_date = (
            now_utc.astimezone(old_zone).date() + timedelta(days=3)
        )
        target_local = datetime.combine(
            target_date,
            time(hour=9, minute=17),
            tzinfo=old_zone,
        )
        initial_schedule_due = target_local.astimezone(timezone.utc)
        day_code = next(
            code
            for code, weekday in WEEKDAY_INDEX.items()
            if weekday == target_date.weekday()
        )
        schedule = {
            "timeLocal": "09:17",
            "repeat": "weekly",
            "days": [day_code],
            "timeBasis": "wall_clock",
        }
        daily_call_times = {day_code.lower(): "09:17"}
        initial_daily_call_due = _next_daily_call_occurrence_utc(
            daily_call_times,
            from_timezone,
            after_utc=now_utc,
        )
        daily_call_payload = _daily_call_payload(
            times=daily_call_times,
            initial_due=initial_daily_call_due,
            timezone_name=from_timezone,
            marker=marker,
        )
        preserved_daily_call_fields = {
            field: daily_call_payload[field]
            for field in DAILY_CALL_PRESERVED_FIELDS
            if field in daily_call_payload
        }

        cleanup_details: dict | None = None
        try:
            development.create_document(
                DEVELOPMENT_USER_PATH,
                {
                    "_smokeFixture": marker,
                    "uid": DEVELOPMENT_UID,
                    "timezone": from_timezone,
                    "phoneNumber": DEFAULT_PHONE_UID,
                    "legacyPhoneUserDocId": DEFAULT_PHONE_UID,
                    "createdAt": datetime.now(timezone.utc),
                    "updatedAt": datetime.now(timezone.utc),
                },
            )
            default.create_document(
                DEFAULT_USER_PATH,
                {
                    "_smokeFixture": marker,
                    "uid": DEVELOPMENT_UID,
                    "timezone": from_timezone,
                    "createdAt": datetime.now(timezone.utc),
                    "updatedAt": datetime.now(timezone.utc),
                },
            )
            development.create_document(
                DEVELOPMENT_REMINDER_PATH,
                _schedule_payload(
                    label="Codex live timezone reminder smoke",
                    schedule=schedule,
                    initial_due=initial_schedule_due,
                    timezone_name=from_timezone,
                    marker=marker,
                ),
            )
            development.create_document(
                DEVELOPMENT_SCHEDULE_PATH,
                _schedule_payload(
                    label="Codex live timezone schedule smoke",
                    schedule=schedule,
                    initial_due=initial_schedule_due,
                    timezone_name=from_timezone,
                    marker=marker,
                ),
            )
            default.create_document(
                DEFAULT_DAILY_CALL_PATH,
                daily_call_payload,
            )

            bridge_changed_at = datetime.now(timezone.utc)
            expected_schedule_occurrences = {
                path: _next_weekly_occurrence_utc(
                    schedule,
                    bridge_timezone,
                    after_utc=bridge_changed_at,
                )
                for path in (
                    DEVELOPMENT_REMINDER_PATH,
                    DEVELOPMENT_SCHEDULE_PATH,
                )
            }
            expected_schedule_triggers = {
                path: occurrence - timedelta(minutes=30)
                for path, occurrence in (
                    expected_schedule_occurrences.items()
                )
            }
            expected_daily_call_after_bridge = (
                _next_daily_call_occurrence_utc(
                    daily_call_times,
                    bridge_timezone,
                    after_utc=bridge_changed_at,
                )
            )
            development.update_document_if_marker(
                DEVELOPMENT_USER_PATH,
                {
                    "timezone": bridge_timezone,
                    "updatedAt": datetime.now(timezone.utc),
                },
                marker=marker,
            )
            bridge_result = await _wait_for_bridge_flow(
                development=development,
                default=default,
                expected_schedules={
                    DEVELOPMENT_REMINDER_PATH: schedule,
                    DEVELOPMENT_SCHEDULE_PATH: schedule,
                },
                expected_occurrences=expected_schedule_occurrences,
                expected_triggers=expected_schedule_triggers,
                expected_daily_call_occurrence=(
                    expected_daily_call_after_bridge
                ),
                expected_daily_call_preserved_fields=(
                    preserved_daily_call_fields
                ),
                from_timezone=from_timezone,
                to_timezone=bridge_timezone,
                changed_after=bridge_changed_at,
                timeout_seconds=args.timeout_seconds,
            )
            if not bridge_result:
                raise RuntimeError(
                    "Cloud workers did not complete the development update, "
                    "guarded default bridge, and bridged Daily Call "
                    "recalculation before timeout"
                )

            bridged_daily_call_metadata = (
                bridge_result["dailyCall"]["timezoneRecalculation"]
            )
            direct_changed_at = datetime.now(timezone.utc)
            expected_daily_call_after_direct_update = (
                _next_daily_call_occurrence_utc(
                    daily_call_times,
                    direct_default_timezone,
                    after_utc=direct_changed_at,
                )
            )
            default.update_document_if_marker(
                DEFAULT_USER_PATH,
                {
                    "timezone": direct_default_timezone,
                    "updatedAt": datetime.now(timezone.utc),
                },
                marker=marker,
            )
            direct_result = await _wait_for_direct_default_flow(
                default=default,
                expected_occurrence=expected_daily_call_after_direct_update,
                expected_preserved_fields=preserved_daily_call_fields,
                from_timezone=bridge_timezone,
                to_timezone=direct_default_timezone,
                changed_after=direct_changed_at,
                previous_event_id=str(
                    bridged_daily_call_metadata.get("eventId") or ""
                ),
                timeout_seconds=args.timeout_seconds,
            )
            if not direct_result:
                raise RuntimeError(
                    "Default worker did not complete the direct timezone "
                    "recalculation before timeout"
                )

            details["runId"] = run_id
            details["bridgeFlow"] = {
                "changedAt": bridge_changed_at.isoformat(),
                "fromTimezone": from_timezone,
                "toTimezone": bridge_timezone,
                "developmentDocuments": {
                    path: _compact_schedule_result(document)
                    for path, document in bridge_result[
                        "developmentDocuments"
                    ].items()
                },
                "defaultUser": {
                    "timezone": bridge_result["defaultUser"].get("timezone"),
                    "uid": bridge_result["defaultUser"].get("uid"),
                    "timezoneScheduleSync": bridge_result[
                        "defaultUser"
                    ].get("timezoneScheduleSync"),
                },
                "dailyCall": _compact_daily_call_result(
                    bridge_result["dailyCall"]
                ),
            }
            details["directDefaultFlow"] = {
                "changedAt": direct_changed_at.isoformat(),
                "fromTimezone": bridge_timezone,
                "toTimezone": direct_default_timezone,
                "defaultUser": {
                    "timezone": direct_result["defaultUser"].get("timezone"),
                    "uid": direct_result["defaultUser"].get("uid"),
                    "timezoneScheduleSync": direct_result[
                        "defaultUser"
                    ].get("timezoneScheduleSync"),
                },
                "dailyCall": _compact_daily_call_result(
                    direct_result["dailyCall"]
                ),
            }
        except Exception as exc:
            details["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            raise
        finally:
            cleanup_details = await _cleanup_and_verify(
                adapters,
                marker=marker,
            )
            details["cleanup"] = cleanup_details
            context.artifact_writer.write_json(
                "scenario-details.json",
                details,
                context.artifact_dir,
            )
            if not cleanup_details["success"]:
                raise RuntimeError(
                    "Live timezone smoke cleanup verification failed; "
                    "inspect scenario-details.json before any retry"
                )

        return ScenarioResult(
            name=self.name,
            success=True,
            started_at=started,
            finished_at=utc_now_iso(),
            summary=(
                "Deployed development/default timezone workers passed "
                "schedule, guarded bridge, direct default, private IAM, "
                "audit metadata, no-delivery, and cleanup checks"
            ),
            artifact_dir=str(context.artifact_dir),
            details=details,
        )
