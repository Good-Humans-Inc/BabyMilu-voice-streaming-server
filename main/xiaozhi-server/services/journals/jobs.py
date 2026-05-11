from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from google.cloud import firestore
from exponent_server_sdk import PushClient, PushMessage, PushTicketError

from core.utils.firestore_factory import build_firestore_client
from services.alarms.reminder_push_job import _get_fcm_messaging
from . import config, generator, store
from services.journals.supabase_client import JournalSupabaseClient
from services.logging import setup_logging

TAG = __name__
logger = setup_logging()


def _now(now: Optional[datetime]) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parent_ids(snapshot: Any) -> Tuple[str, str]:
    character = snapshot.reference.parent.parent
    user = character.parent.parent if character is not None else None
    return (user.id if user is not None else "", character.id if character is not None else "")


def _local_clock_matches(
    *,
    client: firestore.Client,
    user_id: str,
    now: datetime,
    hour: int,
    minute: int,
) -> bool:
    timezone_name = store.get_user_timezone(client, user_id)
    try:
        local = now.astimezone(ZoneInfo(timezone_name))
    except Exception:
        local = now.astimezone(timezone.utc)
    return local.hour == hour and local.minute == minute


def _thread_reference_needed(journal_events: List[Dict[str, Any]]) -> bool:
    if len(journal_events) < 3:
        return False
    last_three = journal_events[:3]
    for event in last_three:
        content = event.get("content") if isinstance(event.get("content"), dict) else {}
        if content.get("thread_reference") is not False:
            return False
    return True


def _source_memory_event_ids(events: List[Dict[str, Any]]) -> List[str]:
    ids = []
    for event in events:
        value = event.get("id") or event.get("event_id")
        if value is not None:
            ids.append(str(value))
    return ids


def _topic_summary(classification: Dict[str, Any]) -> List[str]:
    value = classification.get("topicSummary") or classification.get("topic_summary")
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    trigger = classification.get("primary_trigger")
    return [str(trigger)] if trigger else []


def process_journal_ready_sessions(
    *,
    execute: bool = False,
    now: Optional[datetime] = None,
    client: Optional[firestore.Client] = None,
    supabase: Optional[JournalSupabaseClient] = None,
) -> Dict[str, Any]:
    now = _now(now)
    if not config.processing_enabled():
        return {"ok": True, "disabled": True, "execute": execute, "count": 0, "results": []}

    db = client or build_firestore_client()
    sb = supabase or JournalSupabaseClient()
    results: List[Dict[str, Any]] = []
    waiting = store.fetch_waiting_session_markers(client=db, limit=config.max_ready_sessions())

    for marker in waiting:
        marker_data = marker.to_dict() or {}
        user_id = marker_data.get("userId") or _parent_ids(marker)[0]
        character_id = marker_data.get("characterId") or _parent_ids(marker)[1]
        session_id = marker_data.get("sessionId") or marker.id
        try:
            session = sb.get_session(session_id)
            if not session:
                results.append({"sessionId": session_id, "status": "waiting_missing_session"})
                continue
            memory_status = str(session.get("memory_status") or "").strip().lower()
            if memory_status == "pending":
                results.append({"sessionId": session_id, "status": "waiting_memory"})
                continue
            if memory_status == "skipped":
                if execute:
                    store.update_marker(marker.reference, {"status": "skipped", "reason": "memory_skipped"})
                results.append({"sessionId": session_id, "status": "skipped", "reason": "memory_skipped"})
                continue
            if memory_status and memory_status != "done":
                results.append({"sessionId": session_id, "status": "waiting_memory", "memoryStatus": memory_status})
                continue

            user_turn_count = int(marker_data.get("userTurnCount") or 0)
            if user_turn_count < 3:
                if execute:
                    store.update_marker(marker.reference, {"status": "skipped", "reason": "too_few_turns"})
                results.append({"sessionId": session_id, "status": "skipped", "reason": "too_few_turns"})
                continue

            if execute:
                accumulated_turns = store.add_turns_to_counter(db, user_id, character_id, user_turn_count)
            else:
                accumulated_turns = store.get_turn_counter(db, user_id, character_id) + user_turn_count
            if accumulated_turns < 20:
                if execute:
                    store.update_marker(
                        marker.reference,
                        {"status": "skipped", "reason": "below_turn_threshold", "turnsSinceLastJournal": accumulated_turns},
                    )
                results.append({"sessionId": session_id, "status": "skipped", "reason": "below_turn_threshold"})
                continue

            timezone_name = store.get_user_timezone(db, user_id)
            local_date = store.local_date_for_timezone(now, timezone_name)
            prior_journal_exists = store.has_prior_visible_journal(db, user_id, character_id)
            turns = sb.get_turns(session_id)
            journal_events = sb.get_journal_memory_events(user_id, limit=3)
            trigger_events = sb.get_memory_events_since(
                user_id,
                occurred_after=session.get("start_time") or session.get("created_at"),
                event_types=["cosmic_draw_answered", "tool_invoked"],
            )
            if not prior_journal_exists:
                journal_type = "first"
                classification = {
                    "should_journal": True,
                    "dedup_clear": True,
                    "primary_trigger": "milestone",
                    "anchor_words": [],
                    "sensory_moment": "",
                    "new_fact_detected": True,
                    "topicSummary": ["first journal"],
                    "reason": "First journal after threshold.",
                }
            else:
                if not execute:
                    results.append({"sessionId": session_id, "status": "would_classify", "journalType": "regular"})
                    continue
                classification = generator.classify_session(
                    turns=turns,
                    recent_memory_events=sb.get_recent_memory_events(user_id, limit=5),
                    journal_memory_events=journal_events,
                    trigger_memory_events=trigger_events,
                    session_start_time=str(session.get("start_time") or ""),
                )
                journal_type = "regular"

            if not classification.get("should_journal") or classification.get("dedup_clear") is False:
                if execute:
                    store.update_marker(marker.reference, {"status": "skipped", "reason": "classification_false", "classification": classification})
                results.append({"sessionId": session_id, "status": "skipped", "reason": "classification_false"})
                continue

            queued_session = {
                "sessionId": session_id,
                "classification": classification,
                "sessionStartTime": session.get("start_time"),
                "sessionEndTime": session.get("end_time") or session.get("last_active_at"),
                "sourceMemoryEventIds": _source_memory_event_ids(trigger_events),
            }
            if not execute:
                results.append({"sessionId": session_id, "status": "would_queue", "journalType": journal_type})
                continue
            queue_path = ""
            queue_path = store.queue_session(
                client=db,
                user_id=user_id,
                character_id=character_id,
                local_date=local_date,
                session=queued_session,
                journal_type=journal_type,
            )
            store.update_marker(marker.reference, {"status": "queued", "queuePath": queue_path})
            results.append({"sessionId": session_id, "status": "queued", "queuePath": queue_path, "journalType": journal_type})
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"Journal session processing failed for {session_id}: {exc}")
            if execute:
                store.update_marker(marker.reference, {"status": "error", "error": str(exc)})
            results.append({"sessionId": session_id, "status": "error", "error": str(exc)})

    return {"ok": True, "execute": execute, "count": len(waiting), "results": results}


def run_journal_generation_job(
    *,
    execute: bool = False,
    now: Optional[datetime] = None,
    client: Optional[firestore.Client] = None,
    supabase: Optional[JournalSupabaseClient] = None,
) -> Dict[str, Any]:
    now = _now(now)
    if not config.generation_enabled():
        return {"ok": True, "disabled": True, "execute": execute, "count": 0, "results": []}

    db = client or build_firestore_client()
    sb = supabase or JournalSupabaseClient()
    results: List[Dict[str, Any]] = []
    queues = store.fetch_pending_queues(client=db, limit=config.max_generation_queues())

    for queue_doc in queues:
        user_id, character_id = _parent_ids(queue_doc)
        if not user_id or not character_id:
            continue
        if not _local_clock_matches(client=db, user_id=user_id, now=now, hour=6, minute=30):
            results.append({"queue": queue_doc.reference.path, "status": "skipped", "reason": "not_local_630"})
            continue
        queue = queue_doc.to_dict() or {}
        sessions = queue.get("sessions") if isinstance(queue.get("sessions"), list) else []
        if not sessions:
            if execute:
                queue_doc.reference.set({"status": "skipped", "updated_at": store.iso_now(), "reason": "empty_queue"}, merge=True)
            results.append({"queue": queue_doc.reference.path, "status": "skipped", "reason": "empty_queue"})
            continue

        try:
            journal_type = str(queue.get("journal_type") or "regular")
            if not execute:
                results.append({"queue": queue_doc.reference.path, "status": "would_generate", "journalType": journal_type})
                continue
            user_data = store.get_user_data(db, user_id)
            character_data = store.get_character_data(db, user_id, character_id)
            journal_events = sb.get_journal_memory_events(user_id, limit=3)
            generated = generator.generate_journal_text(
                journal_type=journal_type,
                character_data=character_data,
                user_data=user_data,
                system_memory_block=sb.get_system_memory_block(user_id),
                sessions=sessions,
                prior_journal_entries=store.list_journal_entries(db, user_id, character_id, limit=10),
                thread_reference=_thread_reference_needed(journal_events),
            )
            source_session_ids = [str(item.get("sessionId")) for item in sessions if isinstance(item, dict)]
            source_memory_event_ids = []
            topic_summary = generated.get("topicSummary") or []
            for item in sessions:
                if isinstance(item, dict):
                    source_memory_event_ids.extend([str(x) for x in item.get("sourceMemoryEventIds", [])])
                    if not topic_summary and isinstance(item.get("classification"), dict):
                        topic_summary.extend(_topic_summary(item["classification"]))

            entry_id = ""
            memory_event_id = None
            if execute:
                entry_id = store.create_journal_entry(
                    client=db,
                    user_id=user_id,
                    character_id=character_id,
                    text=generated["text"],
                    journal_type=journal_type,
                    display_date=str(queue.get("date") or store.local_date_for_timezone(now, store.get_user_timezone(db, user_id))),
                    thread_reference=bool(generated["thread_reference"]),
                    source_session_ids=source_session_ids,
                    source_memory_event_ids=source_memory_event_ids,
                )
                occurred_at = sessions[-1].get("sessionEndTime") if sessions else now.isoformat()
                written = sb.write_journal_memory_event(
                    user_id=user_id,
                    character_id=character_id,
                    session_id=source_session_ids[-1] if source_session_ids else None,
                    content={
                        "text": generated["text"],
                        "journalEntryId": entry_id,
                        "journalType": journal_type,
                        "topicSummary": topic_summary,
                        "thread_reference": bool(generated["thread_reference"]),
                    },
                    occurred_at=str(occurred_at or now.isoformat()),
                )
                memory_event_id = (written or {}).get("id")
                queue_doc.reference.set(
                    {
                        "status": "generated",
                        "entryId": entry_id,
                        "memoryEventId": memory_event_id,
                        "updated_at": store.iso_now(),
                    },
                    merge=True,
                )
            results.append({"queue": queue_doc.reference.path, "status": "generated", "entryId": entry_id})
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"Journal generation failed for {queue_doc.reference.path}: {exc}")
            if execute:
                queue_doc.reference.set({"status": "error", "error": str(exc), "updated_at": store.iso_now()}, merge=True)
            results.append({"queue": queue_doc.reference.path, "status": "error", "error": str(exc)})

    results.extend(_run_lure_back_generation(db, sb, now=now, execute=execute))
    return {"ok": True, "execute": execute, "count": len(queues), "results": results}


def _run_lure_back_generation(
    db: firestore.Client,
    sb: JournalSupabaseClient,
    *,
    now: datetime,
    execute: bool,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for user_snap in db.collection("users").limit(config.max_generation_queues()).stream():
        user_id = user_snap.id
        if not _local_clock_matches(client=db, user_id=user_id, now=now, hour=6, minute=30):
            continue
        try:
            latest = sb.get_latest_session_for_user(user_id)
            last_at = latest.get("end_time") or latest.get("last_active_at") or latest.get("created_at") if latest else None
            if not last_at:
                continue
            last_dt = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if now - last_dt < timedelta(days=14):
                continue
            for char_snap in user_snap.reference.collection("characters").limit(20).stream():
                character_id = char_snap.id
                prior = store.list_journal_entries(db, user_id, character_id, limit=50)
                if not prior:
                    continue
                local_date = store.local_date_for_timezone(now, store.get_user_timezone(db, user_id))
                existing_today = [entry for entry in prior if entry.get("display_date") == local_date and entry.get("journal_type") == "lure_back"]
                if existing_today:
                    continue
                if not execute:
                    results.append({"userId": user_id, "characterId": character_id, "status": "would_lure_back_generate"})
                    continue
                generated = generator.generate_journal_text(
                    journal_type="lure_back",
                    character_data=store.get_character_data(db, user_id, character_id),
                    user_data=store.get_user_data(db, user_id),
                    system_memory_block=sb.get_system_memory_block(user_id),
                    sessions=[],
                    prior_journal_entries=list(reversed(prior)),
                    thread_reference=True,
                )
                entry_id = ""
                if execute:
                    entry_id = store.create_journal_entry(
                        client=db,
                        user_id=user_id,
                        character_id=character_id,
                        text=generated["text"],
                        journal_type="lure_back",
                        display_date=local_date,
                        thread_reference=bool(generated["thread_reference"]),
                        source_session_ids=[],
                        source_memory_event_ids=[],
                    )
                    sb.write_journal_memory_event(
                        user_id=user_id,
                        character_id=character_id,
                        session_id=None,
                        content={
                            "text": generated["text"],
                            "journalEntryId": entry_id,
                            "journalType": "lure_back",
                            "topicSummary": generated.get("topicSummary") or ["lure back"],
                            "thread_reference": bool(generated["thread_reference"]),
                        },
                        occurred_at=now.isoformat(),
                    )
                results.append({"userId": user_id, "characterId": character_id, "status": "lure_back_generated", "entryId": entry_id})
        except Exception as exc:
            results.append({"userId": user_id, "status": "lure_back_error", "error": str(exc)})
    return results


def run_journal_publish_job(
    *,
    execute: bool = False,
    now: Optional[datetime] = None,
    client: Optional[firestore.Client] = None,
) -> Dict[str, Any]:
    now = _now(now)
    if not config.publish_enabled():
        return {"ok": True, "disabled": True, "execute": execute, "count": 0, "results": []}

    db = client or build_firestore_client()
    results: List[Dict[str, Any]] = []
    entries = store.fetch_ready_entries(client=db, limit=config.max_publish_entries())
    for entry_doc in entries:
        user_id, character_id = _parent_ids(entry_doc)
        if not _local_clock_matches(client=db, user_id=user_id, now=now, hour=7, minute=0):
            results.append({"entry": entry_doc.reference.path, "status": "skipped", "reason": "not_local_700"})
            continue
        entry = entry_doc.to_dict() or {}
        if execute:
            _supersede_regular_if_needed(db, user_id, character_id, entry)
            store.publish_entry(entry_doc.reference, published_at=now.isoformat())
            if config.push_enabled():
                _send_journal_push(db, user_id, character_id, entry_doc.id, entry)
        results.append({"entry": entry_doc.reference.path, "status": "published"})
    return {"ok": True, "execute": execute, "count": len(entries), "results": results}


def _supersede_regular_if_needed(db: firestore.Client, user_id: str, character_id: str, entry: Dict[str, Any]) -> None:
    if entry.get("journal_type") != "lure_back":
        return
    display_date = entry.get("display_date")
    for candidate in store.list_journal_entries(db, user_id, character_id, include_deleted=True, limit=20):
        if candidate.get("display_date") == display_date and candidate.get("journal_type") == "regular" and candidate.get("status") == "ready":
            candidate["_ref"].set({"status": "superseded", "updated_at": store.iso_now()}, merge=True)


def _send_journal_push(
    db: firestore.Client,
    user_id: str,
    character_id: str,
    entry_id: str,
    entry: Dict[str, Any],
) -> bool:
    user_data = store.get_user_data(db, user_id)
    fcm_token = user_data.get("fcm")
    if not fcm_token:
        return False
    character_data = store.get_character_data(db, user_id, character_id)
    profile = character_data.get("profile") if isinstance(character_data.get("profile"), dict) else {}
    title = profile.get("name") or character_data.get("name") or "Milu"
    body = "I wrote something down for you."
    data = {
        "type": "journal",
        "journalEntryId": str(entry_id),
        "characterId": str(character_id),
        "displayDate": str(entry.get("display_date") or ""),
    }
    try:
        if PushClient.is_exponent_push_token(str(fcm_token)):
            message = PushMessage(
                to=str(fcm_token),
                title=str(title),
                body=body,
                data=data,
                sound="default",
                priority="high",
                channel_id="journals",
            )
            response = PushClient().publish(message)
            response.validate_response()
            return True
        fcm = _get_fcm_messaging()
        if fcm is None:
            return False
        message = fcm.Message(
            notification=fcm.Notification(title=str(title), body=body),
            data=data,
            android=fcm.AndroidConfig(priority="high"),
            token=str(fcm_token),
        )
        fcm.send(message)
        return True
    except PushTicketError as exc:
        logger.bind(tag=TAG).warning(f"Journal Expo push failed uid={user_id} entry={entry_id}: {exc}")
    except Exception as exc:
        logger.bind(tag=TAG).warning(f"Journal push failed uid={user_id} entry={entry_id}: {exc}")
    return False
