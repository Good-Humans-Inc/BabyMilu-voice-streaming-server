from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from services.planning_call import (
    DAILY_CALL_ONBOARDING,
    GrantRejected,
    PlanningCallBinding,
    consume_onboarding_hello,
    finalize_planning_call,
    redact_planning_call_message,
)


class MemoryGrantStore:
    def __init__(self, grant: str, *, expires_at: datetime | None = None):
        self._lock = asyncio.Lock()
        self.hash = hashlib.sha256(grant.encode()).hexdigest()
        self.used = False
        self.expires_at = expires_at or datetime.now(timezone.utc) + timedelta(minutes=5)

    async def consume(self, grant_hash: str, now: datetime) -> PlanningCallBinding:
        async with self._lock:
            if grant_hash != self.hash or self.used or now >= self.expires_at:
                raise GrantRejected()
            self.used = True
            return PlanningCallBinding(
                account_phone="+16173350204",
                character_id="character-1",
                attempt_id="attempt-1",
                scenario=DAILY_CALL_ONBOARDING,
            )


def test_valid_opaque_grant_binds_all_identity_server_side():
    store = MemoryGrantStore("opaque-secret-grant")
    binding = asyncio.run(consume_onboarding_hello(
        {
            "type": "hello",
            "connectionType": DAILY_CALL_ONBOARDING,
            "connectionGrant": "opaque-secret-grant",
        },
        store,
    ))
    assert binding.account_phone == "+16173350204"
    assert binding.character_id == "character-1"
    assert binding.attempt_id == "attempt-1"
    assert binding.scenario == DAILY_CALL_ONBOARDING


def test_rejects_client_identity_and_scenario_fields():
    store = MemoryGrantStore("opaque-secret-grant")
    for forged in (
        {"device_id": "mine"},
        {"uid": "victim"},
        {"phone": "+10000000000"},
        {"characterId": "other"},
        {"attemptId": "other"},
        {"scenario": DAILY_CALL_ONBOARDING},
    ):
        with pytest.raises(GrantRejected):
            asyncio.run(consume_onboarding_hello(
                {
                    "type": "hello",
                    "connectionType": DAILY_CALL_ONBOARDING,
                    "connectionGrant": "opaque-secret-grant",
                    **forged,
                },
                store,
            ))


def test_concurrent_replay_allows_exactly_one_consumer():
    store = MemoryGrantStore("opaque-secret-grant")
    hello = {
        "type": "hello",
        "connectionType": DAILY_CALL_ONBOARDING,
        "connectionGrant": "opaque-secret-grant",
    }
    async def race():
        return await asyncio.gather(
            consume_onboarding_hello(hello, store),
            consume_onboarding_hello(hello, store),
            return_exceptions=True,
        )
    results = asyncio.run(race())
    assert sum(isinstance(result, PlanningCallBinding) for result in results) == 1
    assert sum(isinstance(result, GrantRejected) for result in results) == 1


def test_expired_grant_is_rejected():
    store = MemoryGrantStore(
        "opaque-secret-grant", expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    with pytest.raises(GrantRejected):
        asyncio.run(consume_onboarding_hello(
            {
                "type": "hello",
                "connectionType": DAILY_CALL_ONBOARDING,
                "connectionGrant": "opaque-secret-grant",
            },
            store,
        ))


def test_planning_hello_redaction_never_emits_grant_or_identity():
    raw = json.dumps(
        {
            "type": "hello",
            "connectionType": DAILY_CALL_ONBOARDING,
            "connectionGrant": "opaque-secret",
            "uid": "private-user",
        }
    )
    redacted = redact_planning_call_message(raw)
    assert "opaque-secret" not in redacted
    assert "private-user" not in redacted
    assert redacted == "planning-call hello"


class MemoryCompletionStore:
    def __init__(self):
        self.states = []

    def save_status(self, binding, status, personalization=None, summary=None):
        self.states.append((binding, status, personalization, summary))
        return True


def test_finalization_uses_same_attempt_and_sanitizes_personalization():
    store = MemoryCompletionStore()
    binding = PlanningCallBinding(
        account_phone="+16173350204",
        character_id="character-1",
        attempt_id="attempt-1",
        scenario=DAILY_CALL_ONBOARDING,
    )
    assert finalize_planning_call(
        store,
        binding,
        personalization={
            "wakeStyle": "  gentle but persistent  ",
            "dailyLife": "student",
            "unknown": "must not persist",
            "schemaVersion": 999,
        },
        summary="  Discussed a calm wake-up.  ",
        conversation_id="conversation-1",
    )
    assert [state[1] for state in store.states] == [
        "saving_personalization",
        "completed",
    ]
    saved = store.states[-1][2]
    assert saved == {
        "schemaVersion": 1,
        "wakeStyle": "gentle but persistent",
        "dailyLife": "student",
        "sourceConversationId": "conversation-1",
    }
    assert store.states[-1][3] == "Discussed a calm wake-up."


def test_finalization_stops_if_attempt_no_longer_matches():
    class StaleStore(MemoryCompletionStore):
        def save_status(self, binding, status, personalization=None, summary=None):
            self.states.append(status)
            return False

    store = StaleStore()
    binding = PlanningCallBinding("+1", "c", "old-attempt", DAILY_CALL_ONBOARDING)
    assert not finalize_planning_call(store, binding, personalization={})
    assert store.states == ["saving_personalization"]
