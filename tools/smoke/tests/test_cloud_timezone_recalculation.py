from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


SMOKE_ROOT = Path(__file__).resolve().parents[1]
if str(SMOKE_ROOT) not in sys.path:
    sys.path.insert(0, str(SMOKE_ROOT))

from harness.registry import make_scenario  # noqa: E402
from harness.scenarios.cloud_timezone_recalculation import (  # noqa: E402
    CONFIRMATION_VALUE,
    DEFAULT_DAILY_CALL_PATH,
    DEFAULT_PHONE_UID,
    DEFAULT_USER_PATH,
    DEVELOPMENT_REMINDER_PATH,
    DEVELOPMENT_SCHEDULE_PATH,
    DEVELOPMENT_UID,
    DEVELOPMENT_USER_PATH,
    EXPECTED_BUILD_SERVICE_ACCOUNT,
    EXPECTED_PROJECT_ID,
    EXPECTED_TRIGGER_LOCATION,
    SCENARIO_NAME,
    TARGET_PATHS_BY_DATABASE,
    WORKER_CONTRACTS,
    ScheduledCloudTimezoneWorkerRecalculationScenario,
    _assert_fixture_unoccupied,
    _bridge_document_matches,
    _cleanup_and_verify,
    _daily_call_document_matches,
    _schedule_document_matches,
    _validate_live_cloud_environment,
    _validate_worker_contracts,
)


def _context(**overrides):
    environment = SimpleNamespace(
        environment_type="cloud",
        data_mode="live-shape",
        project=EXPECTED_PROJECT_ID,
    )
    args = SimpleNamespace(
        uid=DEVELOPMENT_UID,
        confirm_live_timezone_smoke=CONFIRMATION_VALUE,
        keep_docs=False,
        skip_preflight=False,
    )
    for key, value in overrides.items():
        if hasattr(environment, key):
            setattr(environment, key, value)
        else:
            setattr(args, key, value)
    return SimpleNamespace(environment=environment, args=args)


def _empty_fixture_state() -> dict:
    return {
        "development": {
            "user": None,
            "descendants": {},
            "legacyDocuments": {},
        },
        "(default)": {
            "user": None,
            "descendants": {},
            "legacyDocuments": {},
        },
    }


def _marker() -> dict:
    return {
        "scenario": SCENARIO_NAME,
        "fixtureVersion": 1,
        "runId": "unit-test",
        "disposable": True,
    }


def test_live_scenario_is_registered_with_exact_allowlist() -> None:
    assert isinstance(
        make_scenario(SCENARIO_NAME),
        ScheduledCloudTimezoneWorkerRecalculationScenario,
    )
    assert TARGET_PATHS_BY_DATABASE == {
        "development": (
            DEVELOPMENT_USER_PATH,
            DEVELOPMENT_REMINDER_PATH,
            DEVELOPMENT_SCHEDULE_PATH,
        ),
        "(default)": (
            DEFAULT_USER_PATH,
            DEFAULT_DAILY_CALL_PATH,
        ),
    }
    assert DEVELOPMENT_USER_PATH == (
        "users/codex-timezone-live-smoke-20260726"
    )
    assert DEFAULT_USER_PATH == "users/+15550003333"
    assert DEFAULT_PHONE_UID == "+15550003333"


def test_live_environment_accepts_only_exact_cloud_shape(monkeypatch) -> None:
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    _validate_live_cloud_environment(_context())


@pytest.mark.parametrize(
    ("override", "value", "message"),
    (
        ("environment_type", "local-compose", "environment_type=cloud"),
        ("data_mode", "isolated", "data_mode=live-shape"),
        ("project", "another-project", EXPECTED_PROJECT_ID),
        ("uid", "another-user", DEVELOPMENT_UID),
        (
            "confirm_live_timezone_smoke",
            "",
            "--confirm-live-timezone-smoke",
        ),
        ("keep_docs", True, "--keep-docs"),
        ("skip_preflight", True, "--skip-preflight"),
    ),
)
def test_live_environment_refuses_wrong_scope(
    monkeypatch,
    override: str,
    value,
    message: str,
) -> None:
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    with pytest.raises(RuntimeError, match=message):
        _validate_live_cloud_environment(_context(**{override: value}))


def test_live_environment_refuses_emulator(monkeypatch) -> None:
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
    with pytest.raises(RuntimeError, match="refuses FIRESTORE_EMULATOR_HOST"):
        _validate_live_cloud_environment(_context())


def test_fixture_guard_accepts_only_complete_absence() -> None:
    _assert_fixture_unoccupied(_empty_fixture_state())


def test_fixture_guard_refuses_preexisting_non_smoke_data() -> None:
    state = _empty_fixture_state()
    state["development"]["user"] = {"timezone": "America/New_York"}
    with pytest.raises(RuntimeError, match="pre-existing non-smoke"):
        _assert_fixture_unoccupied(state)


def test_fixture_guard_refuses_stale_smoke_data() -> None:
    state = _empty_fixture_state()
    state["(default)"]["descendants"][DEFAULT_DAILY_CALL_PATH] = {
        "_smokeFixture": _marker()
    }
    with pytest.raises(RuntimeError, match="stale smoke fixture"):
        _assert_fixture_unoccupied(state)


def _function_payload(database_id: str) -> dict:
    contract = WORKER_CONTRACTS[database_id]
    return {
        "state": "ACTIVE",
        "buildConfig": {
            "serviceAccount": EXPECTED_BUILD_SERVICE_ACCOUNT,
        },
        "serviceConfig": {
            "uri": (
                "https://"
                + WORKER_CONTRACTS[database_id]["function"]
                + ".example.test"
            ),
            "serviceAccountEmail": contract["runtimeServiceAccount"],
            "environmentVariables": {
                "FIRESTORE_PROJECT_ID": EXPECTED_PROJECT_ID,
                "FIRESTORE_DATABASE_ID": database_id,
                **(
                    {
                        "LEGACY_DAILY_CALL_DATABASE_ID": (
                            contract["legacyDatabase"]
                        )
                    }
                    if contract["legacyDatabase"]
                    else {}
                ),
            },
        },
        "eventTrigger": {
            "serviceAccountEmail": contract["triggerServiceAccount"],
            "triggerRegion": EXPECTED_TRIGGER_LOCATION,
            "eventType": "google.cloud.firestore.document.v1.updated",
            "eventFilters": [
                {"attribute": "database", "value": database_id},
                {"attribute": "document", "value": "users/{uid}"},
            ],
        },
    }


def test_deployed_contract_check_requires_private_exact_invoker(
    monkeypatch,
) -> None:
    def fake_gcloud(arguments: list[str]) -> dict:
        if arguments[:2] == ["projects", "get-iam-policy"]:
            return {"bindings": []}
        function_name = (
            arguments[2]
            if arguments[:2] == ["functions", "describe"]
            else arguments[3]
        )
        database_id = next(
            database
            for database, contract in WORKER_CONTRACTS.items()
            if contract["function"] == function_name
        )
        if arguments[:2] == ["functions", "describe"]:
            return _function_payload(database_id)
        return {
            "bindings": [
                {
                    "role": "roles/run.invoker",
                    "members": [
                        "serviceAccount:"
                        + WORKER_CONTRACTS[database_id][
                            "triggerServiceAccount"
                        ]
                    ],
                }
            ]
        }

    monkeypatch.setattr(
        "harness.scenarios.cloud_timezone_recalculation._run_gcloud_json",
        fake_gcloud,
    )
    monkeypatch.setattr(
        "harness.scenarios.cloud_timezone_recalculation._probe_unauthenticated",
        lambda uri: 403,
    )
    summaries = _validate_worker_contracts()
    assert set(summaries) == {"development", "(default)"}
    assert all(
        summary["publicInvokerPresent"] is False
        for summary in summaries.values()
    )


def test_deployed_contract_check_rejects_public_invoker(monkeypatch) -> None:
    def fake_gcloud(arguments: list[str]) -> dict:
        if arguments[:2] == ["projects", "get-iam-policy"]:
            return {"bindings": []}
        function_name = (
            arguments[2]
            if arguments[:2] == ["functions", "describe"]
            else arguments[3]
        )
        database_id = next(
            database
            for database, contract in WORKER_CONTRACTS.items()
            if contract["function"] == function_name
        )
        if arguments[:2] == ["functions", "describe"]:
            return _function_payload(database_id)
        return {
            "bindings": [
                {
                    "role": "roles/run.invoker",
                    "members": [
                        "serviceAccount:"
                        + WORKER_CONTRACTS[database_id][
                            "triggerServiceAccount"
                        ],
                        "allUsers",
                    ],
                }
            ]
        }

    monkeypatch.setattr(
        "harness.scenarios.cloud_timezone_recalculation._run_gcloud_json",
        fake_gcloud,
    )
    monkeypatch.setattr(
        "harness.scenarios.cloud_timezone_recalculation._probe_unauthenticated",
        lambda uri: 403,
    )
    with pytest.raises(RuntimeError, match="exactly one Run invoker"):
        _validate_worker_contracts()


def test_schedule_match_requires_cursor_audit_and_no_delivery() -> None:
    changed_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    occurrence = changed_at + timedelta(days=2)
    trigger = occurrence - timedelta(minutes=30)
    schedule = {
        "timeLocal": "09:17",
        "repeat": "weekly",
        "days": ["Tue"],
        "timeBasis": "wall_clock",
    }
    document = {
        "_smokeFixture": _marker(),
        "status": "on",
        "schedule": schedule,
        "nextOccurrenceUTC": occurrence,
        "nextTriggerUTC": trigger,
        "timezoneAtCalculation": "America/New_York",
        "timezoneRecalculation": {
            "schemaVersion": 1,
            "eventId": "event-1",
            "status": "recalculated",
            "fromTimezone": "America/Los_Angeles",
            "toTimezone": "America/New_York",
            "sourceUpdateTime": changed_at.isoformat(),
            "recalculatedAt": datetime.now(timezone.utc).isoformat(),
        },
    }
    assert _schedule_document_matches(
        document,
        expected_occurrence=occurrence,
        expected_trigger=trigger,
        expected_schedule=schedule,
        from_timezone="America/Los_Angeles",
        to_timezone="America/New_York",
        changed_after=changed_at,
    )
    document["lastDelivered"] = datetime.now(timezone.utc)
    assert not _schedule_document_matches(
        document,
        expected_occurrence=occurrence,
        expected_trigger=trigger,
        expected_schedule=schedule,
        from_timezone="America/Los_Angeles",
        to_timezone="America/New_York",
        changed_after=changed_at,
    )


def test_bridge_match_requires_exact_owner_and_source_database() -> None:
    changed_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    document = {
        "_smokeFixture": _marker(),
        "uid": DEVELOPMENT_UID,
        "timezone": "America/New_York",
        "timezoneScheduleSync": {
            "schemaVersion": 1,
            "sourceEventId": "event-1",
            "sourceUpdateTime": changed_at.isoformat(),
            "sourceUid": DEVELOPMENT_UID,
            "sourceDatabase": "development",
            "syncedAt": datetime.now(timezone.utc),
        },
    }
    assert _bridge_document_matches(
        document,
        expected_timezone="America/New_York",
        changed_after=changed_at,
    )
    document["uid"] = "wrong-owner"
    assert not _bridge_document_matches(
        document,
        expected_timezone="America/New_York",
        changed_after=changed_at,
    )


def test_daily_call_match_requires_new_event_and_no_dispatch() -> None:
    changed_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    occurrence = changed_at + timedelta(days=1)
    preserved = {
        "times": {"mon": "09:17"},
        "billing": {"paidCallsRemaining": 3},
    }
    document = {
        "_smokeFixture": _marker(),
        "status": "on",
        **preserved,
        "nextOccurrenceUTC": occurrence,
        "timezoneAtCalculation": "America/Chicago",
        "timezoneRecalculation": {
            "schemaVersion": 1,
            "eventId": "event-2",
            "oldTimezone": "America/New_York",
            "newTimezone": "America/Chicago",
            "scheduleType": "daily_call",
            "outcome": "recalculated",
            "sourceUpdateTime": changed_at.isoformat(),
            "recalculatedAt": datetime.now(timezone.utc).isoformat(),
        },
    }
    assert _daily_call_document_matches(
        document,
        expected_occurrence=occurrence,
        expected_preserved_fields=preserved,
        from_timezone="America/New_York",
        to_timezone="America/Chicago",
        changed_after=changed_at,
        previous_event_id="event-1",
    )
    document["dispatchedAt"] = datetime.now(timezone.utc)
    assert not _daily_call_document_matches(
        document,
        expected_occurrence=occurrence,
        expected_preserved_fields=preserved,
        from_timezone="America/New_York",
        to_timezone="America/Chicago",
        changed_after=changed_at,
        previous_event_id="event-1",
    )


def test_cleanup_deletes_children_before_parents_and_verifies_absence() -> None:
    class EmptyAfterDeleteAdapter:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_document_if_marker(
            self,
            path: str,
            *,
            marker: dict,
        ) -> str:
            self.deleted.append(path)
            return "deleted"

        def get_document(self, path: str):
            return None

        def list_descendant_documents(self, path: str) -> dict:
            return {}

        def find_legacy_schedule_documents(self, uid: str) -> dict:
            return {}

    development = EmptyAfterDeleteAdapter()
    default = EmptyAfterDeleteAdapter()
    result = asyncio.run(
        _cleanup_and_verify(
            {
                "development": development,
                "(default)": default,
            },
            marker=_marker(),
            timeout_seconds=1,
        )
    )

    assert result["success"] is True
    assert development.deleted == [
        DEVELOPMENT_SCHEDULE_PATH,
        DEVELOPMENT_REMINDER_PATH,
        DEVELOPMENT_USER_PATH,
    ]
    assert default.deleted == [
        DEFAULT_DAILY_CALL_PATH,
        DEFAULT_USER_PATH,
    ]
