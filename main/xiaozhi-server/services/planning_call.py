"""Secure one-time WebSocket grants for the native Milu Call onboarding call."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

DAILY_CALL_ONBOARDING = "daily_call_onboarding"
_FORBIDDEN_CLIENT_FIELDS = frozenset(
    {"uid", "userId", "phone", "phoneNumber", "device_id", "deviceId",
     "characterId", "attemptId", "scenario", "prompt"}
)
_PERSONALIZATION_LIMITS = {
    "ageContext": 500,
    "wakeStyle": 500,
    "dailyLife": 2_000,
    "nightRoutine": 2_000,
    "morningRoutine": 2_000,
    "morningSupport": 2_000,
}


def consumed_call_lease_seconds() -> int:
    try:
        configured = int(os.environ.get("MILU_CALL_CONSUMED_LEASE_SECONDS", "900"))
    except (TypeError, ValueError):
        configured = 900
    return max(60, min(3_600, configured))


def consumed_call_lease_expired(
    consumed_at: Any,
    now: Optional[datetime] = None,
    *,
    lease_seconds: Optional[int] = None,
) -> bool:
    consumed = _timestamp_to_datetime(consumed_at)
    if consumed is None:
        return True
    current = now or datetime.now(timezone.utc)
    ttl = consumed_call_lease_seconds() if lease_seconds is None else lease_seconds
    return current >= consumed + timedelta(seconds=ttl)


class GrantRejected(Exception):
    """A deliberately context-free rejection safe to return to a client."""

    def __init__(self):
        super().__init__("planning call connection rejected")


@dataclass(frozen=True)
class PlanningCallBinding:
    account_phone: str
    character_id: str
    attempt_id: str
    scenario: str
    consumed_grant_hash: Optional[str] = None


class AsyncGrantStore(Protocol):
    async def consume(
        self, grant_hash: str, now: datetime
    ) -> PlanningCallBinding: ...


class PlanningCallCompletionStore(Protocol):
    def save_status(
        self,
        binding: PlanningCallBinding,
        status: str,
        personalization: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
        expected_statuses: tuple = (),
        consumed_grant_hash: Optional[str] = None,
        require_active_consumed_lease: bool = False,
    ) -> bool: ...


def grant_hash(grant: str) -> str:
    return hashlib.sha256(grant.encode("utf-8")).hexdigest()


async def consume_onboarding_hello(
    hello: Mapping[str, Any],
    store: AsyncGrantStore,
    *,
    now: Optional[datetime] = None,
) -> PlanningCallBinding:
    if hello.get("type") != "hello" or hello.get("connectionType") != DAILY_CALL_ONBOARDING:
        raise GrantRejected()
    if any(field in hello for field in _FORBIDDEN_CLIENT_FIELDS):
        raise GrantRejected()
    grant = hello.get("connectionGrant")
    if not isinstance(grant, str) or not (16 <= len(grant) <= 1_024):
        raise GrantRejected()
    return await store.consume(grant_hash(grant), now or datetime.now(timezone.utc))


def redact_planning_call_message(message: str) -> str:
    try:
        value = json.loads(message)
    except (TypeError, ValueError):
        return "invalid text message"
    if isinstance(value, dict) and value.get("connectionType") == DAILY_CALL_ONBOARDING:
        return "planning-call hello"
    return message


async def wait_for_greeting_mode(conn: Any) -> bool:
    """Wait until bootstrap has applied the server-owned onboarding mode."""
    bootstrap_complete = getattr(conn, "bootstrap_complete", None)
    if bootstrap_complete is not None and not bootstrap_complete.is_set():
        await bootstrap_complete.wait()
    return bool(
        getattr(conn, "server_initiate_chat", False)
        and not getattr(conn, "_server_greeting_scheduled", False)
    )


async def run_greeting_after_bootstrap(conn: Any, trigger) -> bool:
    if not await wait_for_greeting_mode(conn):
        return False
    conn._server_greeting_scheduled = True
    await trigger(conn)
    return True


def has_meaningful_exchange(conversation: Any) -> bool:
    if not isinstance(conversation, list):
        return False
    roles = {
        item.get("role")
        for item in conversation
        if isinstance(item, dict)
        and isinstance(item.get("content"), str)
        and item["content"].strip()
    }
    return "user" in roles and "assistant" in roles


def planning_call_succeeded(
    conversation: Any, *, explicit_successful_end: bool = False
) -> bool:
    return bool(explicit_successful_end or has_meaningful_exchange(conversation))


def parse_personalization_extraction(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, str):
        return None
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        value = json.loads(raw[start:end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    result: Dict[str, str] = {}
    for field, limit in _PERSONALIZATION_LIMITS.items():
        item = value.get(field)
        if isinstance(item, str) and item.strip():
            result[field] = item.strip()[:limit]
    return result or None


def _timestamp_to_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


class FirestorePlanningCallStore:
    """Consumes and completes grants on users/{phone}/miluCall/onboarding."""

    def __init__(self, client=None):
        if client is None:
            from core.utils.firestore_client import get_firestore_client
            client = get_firestore_client()
        self.client = client

    async def consume(self, grant_hash_value: str, now: datetime) -> PlanningCallBinding:
        return await asyncio.to_thread(self._consume_sync, grant_hash_value, now)

    def _consume_sync(self, grant_hash_value: str, now: datetime) -> PlanningCallBinding:
        transaction = self.client.transaction()

        @firestore.transactional
        def consume_in_transaction(txn):
            query = self.client.collection_group("miluCall").where(
                filter=FieldFilter("planningCall.grant.hash", "==", grant_hash_value)
            ).limit(2)
            snapshots = list(txn.get(query))
            if len(snapshots) != 1:
                raise GrantRejected()
            snapshot = snapshots[0]
            if snapshot.id != "onboarding":
                raise GrantRejected()
            data = snapshot.to_dict() or {}
            planning = data.get("planningCall") or {}
            grant = planning.get("grant") or {}
            stored_hash = grant.get("hash")
            expires_at = _timestamp_to_datetime(grant.get("expiresAt"))
            if (
                not isinstance(stored_hash, str)
                or not hmac.compare_digest(stored_hash, grant_hash_value)
                or expires_at is None
                or now >= expires_at
                or grant.get("consumedAt") is not None
                or planning.get("status") != "call_in_progress"
            ):
                raise GrantRejected()
            scenario = grant.get("scenario")
            character_id = grant.get("characterId")
            attempt_id = grant.get("attemptId")
            account_ref = snapshot.reference.parent.parent
            account_phone = account_ref.id if account_ref is not None else ""
            if (
                scenario != DAILY_CALL_ONBOARDING
                or not all(isinstance(v, str) and v for v in
                           (account_phone, character_id, attempt_id))
                or planning.get("attemptId") != attempt_id
                or planning.get("characterId") != character_id
            ):
                raise GrantRejected()
            txn.update(snapshot.reference, {
                "planningCall.grant.consumedAt": now,
                "planningCall.grant.consumedAttemptId": attempt_id,
                "planningCall.updatedAt": firestore.SERVER_TIMESTAMP,
            })
            return PlanningCallBinding(
                account_phone=account_phone,
                character_id=character_id,
                attempt_id=attempt_id,
                scenario=scenario,
                consumed_grant_hash=grant_hash_value,
            )

        return consume_in_transaction(transaction)

    def save_status(
        self,
        binding: PlanningCallBinding,
        status: str,
        personalization: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
        expected_statuses: tuple = (),
        consumed_grant_hash: Optional[str] = None,
        require_active_consumed_lease: bool = False,
    ) -> bool:
        ref = (
            self.client.collection("users").document(binding.account_phone)
            .collection("miluCall").document("onboarding")
        )
        daily_call_ref = (
            self.client.collection("users").document(binding.account_phone)
            .collection("miluCall").document("dailyCall")
        )
        transaction = self.client.transaction()

        @firestore.transactional
        def save(txn):
            snapshot = ref.get(transaction=txn)
            daily_call = daily_call_ref.get(transaction=txn)
            planning = ((snapshot.to_dict() or {}).get("planningCall") or {})
            grant = planning.get("grant") or {}
            stored_grant_hash = grant.get("hash")
            grant_matches = consumed_grant_hash is None or (
                isinstance(stored_grant_hash, str)
                and hmac.compare_digest(stored_grant_hash, consumed_grant_hash)
                and grant.get("consumedAt") is not None
            )
            active_lease = (
                not require_active_consumed_lease
                or not consumed_call_lease_expired(grant.get("consumedAt"))
            )
            if (
                not snapshot.exists
                or planning.get("attemptId") != binding.attempt_id
                or planning.get("characterId") != binding.character_id
                or (expected_statuses and planning.get("status") not in expected_statuses)
                or not grant_matches
                or not active_lease
            ):
                return False
            updates: Dict[str, Any] = {
                "planningCall.status": status,
                "planningCall.updatedAt": firestore.SERVER_TIMESTAMP,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            }
            if personalization is not None:
                updates["planningCall.morningCallPersonalization"] = personalization
            if summary:
                updates["planningCall.summary"] = summary[:2_000]
            txn.update(ref, updates)
            if personalization is not None and daily_call.exists:
                txn.update(daily_call_ref, {
                    "morningCallPersonalization": personalization,
                    "updatedAt": firestore.SERVER_TIMESTAMP,
                })
            return True

        return bool(save(transaction))


def _validated_personalization(
    personalization: Optional[Mapping[str, Any]], conversation_id: Optional[str]
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"schemaVersion": 1}
    for field, limit in _PERSONALIZATION_LIMITS.items():
        value = (personalization or {}).get(field)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()[:limit]
    if isinstance(conversation_id, str) and conversation_id.strip():
        result["sourceConversationId"] = conversation_id.strip()[:256]
    return result


def finalize_planning_call(
    store: PlanningCallCompletionStore,
    binding: PlanningCallBinding,
    *,
    personalization: Optional[Mapping[str, Any]],
    summary: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> bool:
    if binding.scenario != DAILY_CALL_ONBOARDING:
        return False
    validated = _validated_personalization(personalization, conversation_id)
    clean_summary = summary.strip()[:2_000] if isinstance(summary, str) and summary.strip() else None
    return store.save_status(
        binding,
        "completed",
        validated,
        clean_summary,
        expected_statuses=("call_in_progress",),
        consumed_grant_hash=binding.consumed_grant_hash,
        require_active_consumed_lease=True,
    )


def mark_planning_call_retryable(
    store: PlanningCallCompletionStore,
    binding: PlanningCallBinding,
) -> bool:
    if binding.scenario != DAILY_CALL_ONBOARDING:
        return False
    return store.save_status(
        binding,
        "ready_for_call",
        expected_statuses=("call_in_progress",),
        consumed_grant_hash=binding.consumed_grant_hash,
        require_active_consumed_lease=True,
    )
