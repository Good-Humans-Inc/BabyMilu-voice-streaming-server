from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from services.planning_call import (
    DAILY_CALL_ONBOARDING,
    GrantRejected,
    PlanningCallBinding,
    consume_onboarding_hello,
    finalize_planning_call,
    has_meaningful_exchange,
    parse_personalization_extraction,
    mark_planning_call_retryable,
    planning_call_succeeded,
    wait_for_greeting_mode,
    run_greeting_after_bootstrap,
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
        self.status = "call_in_progress"
        self.grant_hash = None
        self._status_lock = threading.Lock()

    def save_status(
        self, binding, status, personalization=None, summary=None,
        expected_statuses=(), consumed_grant_hash=None,
    ):
        with self._status_lock:
            if expected_statuses and self.status not in expected_statuses:
                return False
            if consumed_grant_hash is not None and consumed_grant_hash != self.grant_hash:
                return False
            self.states.append((binding, status, personalization, summary))
            self.status = status
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
        def save_status(self, binding, status, personalization=None, summary=None, **kwargs):
            self.states.append(status)
            return False

    store = StaleStore()
    binding = PlanningCallBinding("+1", "c", "old-attempt", DAILY_CALL_ONBOARDING)
    assert not finalize_planning_call(
        store, binding, personalization={"wakeStyle": "gentle"}
    )
    assert store.states == ["saving_personalization"]


def test_greeting_waits_until_bootstrap_applies_onboarding_mode():
    class Conn:
        def __init__(self):
            self.bootstrap_complete = asyncio.Event()
            self.server_initiate_chat = False
            self._server_greeting_scheduled = False

    async def scenario():
        conn = Conn()
        waiter = asyncio.create_task(wait_for_greeting_mode(conn))
        await asyncio.sleep(0)
        assert not waiter.done()
        conn.server_initiate_chat = True
        conn.bootstrap_complete.set()
        return await waiter

    assert asyncio.run(scenario()) is True


def test_greeting_trigger_runs_only_after_mode_is_applied():
    class Conn:
        def __init__(self):
            self.bootstrap_complete = asyncio.Event()
            self.server_initiate_chat = False
            self._server_greeting_scheduled = False

    async def scenario():
        conn = Conn()
        observed = []

        async def trigger(_conn):
            observed.append((_conn.server_initiate_chat, _conn.bootstrap_complete.is_set()))

        task = asyncio.create_task(run_greeting_after_bootstrap(conn, trigger))
        await asyncio.sleep(0)
        assert observed == []
        conn.server_initiate_chat = True
        conn.bootstrap_complete.set()
        assert await task
        return observed, conn._server_greeting_scheduled

    assert asyncio.run(scenario()) == ([(True, True)], True)


def test_meaningful_exchange_requires_user_and_assistant_turns():
    assert not has_meaningful_exchange([{"role": "assistant", "content": "Good morning"}])
    assert not has_meaningful_exchange([{"role": "user", "content": "   "}])
    assert has_meaningful_exchange([
        {"role": "assistant", "content": "How do you like to wake up?"},
        {"role": "user", "content": "Gently, but don't let me snooze."},
    ])
    assert planning_call_succeeded([], explicit_successful_end=True)


def test_structured_extraction_is_allowlisted_and_requires_real_content():
    assert parse_personalization_extraction('{"schemaVersion":1}') is None
    assert parse_personalization_extraction('not json') is None
    assert parse_personalization_extraction(json.dumps({
        "wakeStyle": "  playful and persistent ",
        "morningRoutine": "coffee first",
        "secret": "drop me",
    })) == {
        "wakeStyle": "playful and persistent",
        "morningRoutine": "coffee first",
    }


def test_finalization_completes_when_user_declines_personalization():
    store = MemoryCompletionStore()
    binding = PlanningCallBinding("+1", "c", "attempt", DAILY_CALL_ONBOARDING)
    assert finalize_planning_call(store, binding, personalization={})
    assert [state[1] for state in store.states] == [
        "saving_personalization", "completed"
    ]
    assert store.states[-1][2] == {"schemaVersion": 1}


def test_unsure_answer_and_transient_extractor_failure_do_not_force_replay():
    conversation = [
        {"role": "assistant", "content": "What helps you wake up?"},
        {"role": "user", "content": "I'm not sure yet."},
    ]
    assert has_meaningful_exchange(conversation)
    assert parse_personalization_extraction("temporary provider error") is None
    store = MemoryCompletionStore()
    binding = PlanningCallBinding("+1", "c", "attempt", DAILY_CALL_ONBOARDING)
    assert finalize_planning_call(store, binding, personalization=None)
    assert store.status == "completed"


def test_failed_exchange_returns_same_attempt_to_retryable_status():
    store = MemoryCompletionStore()
    binding = PlanningCallBinding("+1", "c", "attempt", DAILY_CALL_ONBOARDING)
    assert mark_planning_call_retryable(store, binding)
    assert [state[1] for state in store.states] == ["ready_for_call"]


def test_stale_retry_cannot_downgrade_saving_or_completed_attempt():
    binding = PlanningCallBinding("+1", "c", "attempt", DAILY_CALL_ONBOARDING)
    for newer_status in ("saving_personalization", "completed"):
        store = MemoryCompletionStore()
        store.status = newer_status
        assert not mark_planning_call_retryable(store, binding)
        assert store.status == newer_status


def test_finalize_uses_expected_status_for_each_transition():
    store = MemoryCompletionStore()
    binding = PlanningCallBinding("+1", "c", "attempt", DAILY_CALL_ONBOARDING)
    assert finalize_planning_call(store, binding, personalization=None)
    assert store.status == "completed"
    # A delayed duplicate finalizer cannot move completed back through saving.
    assert not finalize_planning_call(store, binding, personalization={"wakeStyle": "late"})
    assert store.status == "completed"


def test_old_consumed_grant_cannot_update_reissued_attempt():
    store = MemoryCompletionStore()
    store.grant_hash = "new-grant-hash"
    stale = PlanningCallBinding(
        "+1", "c", "attempt", DAILY_CALL_ONBOARDING, "old-grant-hash"
    )
    assert not mark_planning_call_retryable(store, stale)
    assert not finalize_planning_call(store, stale, personalization=None)
    assert store.status == "call_in_progress"


def test_concurrent_retry_and_finalize_never_downgrade_terminal_state():
    store = MemoryCompletionStore()
    binding = PlanningCallBinding("+1", "c", "attempt", DAILY_CALL_ONBOARDING)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda action: action(),
            (
                lambda: mark_planning_call_retryable(store, binding),
                lambda: finalize_planning_call(store, binding, personalization=None),
            ),
        ))
    assert any(outcomes)
    assert store.status in {"ready_for_call", "completed"}
    assert not (
        store.status == "ready_for_call"
        and any(state[1] == "completed" for state in store.states)
    )
    terminal = store.status
    assert not finalize_planning_call(store, binding, personalization={"wakeStyle": "late"})
    assert store.status == terminal
