from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from openai import OpenAI

from services.journals import config


BANNED_TIME_OPENINGS = (
    "today",
    "yesterday",
    "this morning",
    "this evening",
)


def _client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY environment variable")
    return OpenAI(api_key=api_key)


def _turns_text(turns: List[Dict[str, Any]]) -> str:
    lines = []
    for turn in turns:
        speaker = str(turn.get("speaker") or "").strip() or "unknown"
        text = str(turn.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _json_response(messages: List[Dict[str, str]], *, temperature: float = 0.2) -> Dict[str, Any]:
    response = _client().chat.completions.create(
        model=config.openai_model(),
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content if response.choices else ""
    if not content:
        raise RuntimeError("LLM returned empty response")
    return json.loads(content)


def classify_session(
    *,
    turns: List[Dict[str, Any]],
    recent_memory_events: List[Dict[str, Any]],
    journal_memory_events: List[Dict[str, Any]],
    trigger_memory_events: List[Dict[str, Any]],
    session_start_time: str,
) -> Dict[str, Any]:
    prompt = {
        "sessionTranscript": _turns_text(turns),
        "recentMemoryEvents": recent_memory_events,
        "priorJournalEvents": journal_memory_events,
        "triggerMemoryEvents": trigger_memory_events,
        "sessionStartTime": session_start_time,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You classify whether a BabyMilu plushie conversation deserves a private "
                "character journal entry. Journals are rare: do not journal a single trivial "
                "fact or generic small talk. Return JSON only with keys: should_journal "
                "boolean, journal_value_type one of strong|medium|weak|none, "
                "journal_reason_type string, primary_trigger string, anchor_words string "
                "array, sensory_moment string, new_fact_detected boolean, dedup_clear "
                "boolean, topicSummary string array, coverageSummary string array, "
                "concreteAnchors string array, emotionalThemes string array, avoidRepeating "
                "string array, thread_reference boolean, thread_reference_reason string, "
                "thread_reference_targets string array, reason string. Classify strong for "
                "a clear user revelation, relationship arc, milestone, memorable interaction, "
                "or recurring theme with new concrete detail. Classify medium for a meaningful "
                "fact cluster or useful life context. Classify weak for one isolated minor fact. "
                "Recurring emotions like tiredness, anxiety, grief, loneliness, or stress are "
                "not duplicates by themselves. dedup_clear is false only when the new journal "
                "would repeat the same concrete prior coverage without a new fact, event, "
                "ritual, conflict, decision, relationship detail, or perspective. "
                "thread_reference means useful continuity with prior concrete coverage, not "
                "dedup suppression."
            ),
        },
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)},
    ]
    data = _json_response(messages, temperature=0.1)
    data.setdefault("should_journal", False)
    data.setdefault("journal_value_type", "none")
    data.setdefault("journal_reason_type", "")
    data.setdefault("dedup_clear", bool(data.get("should_journal")))
    data.setdefault("topicSummary", [])
    data.setdefault("anchor_words", [])
    data.setdefault("coverageSummary", [])
    data.setdefault("concreteAnchors", [])
    data.setdefault("emotionalThemes", [])
    data.setdefault("avoidRepeating", [])
    data.setdefault("thread_reference", False)
    data.setdefault("thread_reference_reason", "")
    data.setdefault("thread_reference_targets", [])
    return data


def generate_journal_text(
    *,
    journal_type: str,
    character_data: Dict[str, Any],
    user_data: Dict[str, Any],
    system_memory_block: str,
    sessions: List[Dict[str, Any]],
    prior_journal_entries: List[Dict[str, Any]],
    thread_reference: bool,
    coverage_window: Dict[str, Any] | None = None,
    avoid_repeating: List[str] | None = None,
    allow_time_specific_opening: bool = False,
) -> Dict[str, Any]:
    profile = character_data.get("profile") if isinstance(character_data.get("profile"), dict) else {}
    character_name = (
        profile.get("name") or character_data.get("name") or "Milu"
    )
    user_name = user_data.get("name") or user_data.get("displayName") or "the user"
    style = (
        "Write 2 to 4 sentences. Do not summarize the session. Write as a private diary "
        "from the character's first-person perspective. Use sensory or emotional detail. "
        "No platitudes, no AI-speak, no markdown. The journal may cover multiple "
        "conversations; do not imply everything happened today. Do not start with "
        "'Today', 'Yesterday', 'This morning', 'This evening', or a calendar phrase "
        "unless singleSameDayMoment is true."
    )
    if journal_type == "first":
        style = (
            f"This is your first journal entry after meeting {user_name}. Begin with meeting "
            "them or a close variation in the character's voice. Focus on what you learned "
            "about who they are. 2 to 3 sentences. Subtly include the reminder hint only if "
            "it fits naturally: the character hopes the user asks them for help someday."
        )
    elif journal_type == "lure_back":
        style = (
            f"It has been a while since you spoke with {user_name}. Remember something "
            "specific and meaningful about them. Do not say 'I miss you'. 2 to 4 sentences."
        )
    elif len(sessions) > 1:
        style += " Multiple moments may be provided; write one coherent private reflection. 5 to 6 sentences maximum."
    style += (
        " The writer is the plushie character, not an assistant, narrator, therapist, "
        "or product. Do not start with 'Today', 'Yesterday', 'This morning', "
        "'This evening', or a calendar phrase unless singleSameDayMoment is true."
    )

    payload = {
        "characterName": character_name,
        "characterProfile": character_data,
        "user": user_data,
        "systemMemoryBlock": system_memory_block,
        "queuedSessions": sessions,
        "priorJournalEntries": prior_journal_entries,
        "priorCoverageSummaries": _prior_coverage(prior_journal_entries),
        "avoidRepeating": avoid_repeating or _prior_avoid_repeating(prior_journal_entries),
        "coverageWindow": coverage_window or {},
        "selectedConversationContext": sessions,
        "threadReferenceRequired": thread_reference,
        "singleSameDayMoment": allow_time_specific_opening,
        "style": style,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You write BabyMilu character journals in the plushie character's own "
                "first-person voice. The writer is the character, not an assistant, "
                "narrator, therapist, or product. Stay consistent with the character "
                "profile. Do not mention being an AI or model. Return JSON only: "
                "{\"text\": string, \"thread_reference\": boolean, \"topicSummary\": "
                "string[], \"coverageSummary\": string[], \"concreteAnchors\": string[], "
                "\"emotionalThemes\": string[], \"avoidRepeating\": string[], "
                "\"thread_reference_reason\": string, \"thread_reference_targets\": "
                "string[], \"voice_check\": {\"first_person_character_voice\": boolean, "
                "\"generic_summary\": boolean, \"starts_with_banned_time_phrase\": boolean}}."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]
    data = _json_response(messages, temperature=0.7)
    text = str(data.get("text") or "").strip()
    voice_check = data.get("voice_check") if isinstance(data.get("voice_check"), dict) else {}
    if _needs_regeneration(text, voice_check, allow_time_specific_opening):
        retry_messages = messages + [
            {
                "role": "user",
                "content": (
                    "Rewrite once. Keep the same meaning, but make it clearly first-person "
                    "from the plushie character, not a generic summary. Do not start with "
                    "Today, Yesterday, This morning, This evening, or a calendar phrase."
                ),
            }
        ]
        data = _json_response(retry_messages, temperature=0.5)
        text = str(data.get("text") or "").strip()
    if not text:
        raise RuntimeError("Journal generation returned empty text")
    return {
        "text": text,
        "thread_reference": bool(data.get("thread_reference", thread_reference)),
        "topicSummary": data.get("topicSummary") if isinstance(data.get("topicSummary"), list) else [],
        "coverageSummary": _list(data.get("coverageSummary")),
        "concreteAnchors": _list(data.get("concreteAnchors")),
        "emotionalThemes": _list(data.get("emotionalThemes")),
        "avoidRepeating": _list(data.get("avoidRepeating")),
        "thread_reference_reason": str(data.get("thread_reference_reason") or ""),
        "thread_reference_targets": _list(data.get("thread_reference_targets")),
        "voice_check": data.get("voice_check") if isinstance(data.get("voice_check"), dict) else {},
    }


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _prior_coverage(entries: List[Dict[str, Any]]) -> List[Any]:
    values: List[Any] = []
    for entry in entries:
        values.extend(_list(entry.get("coverage_summary") or entry.get("coverageSummary")))
    return values


def _prior_avoid_repeating(entries: List[Dict[str, Any]]) -> List[Any]:
    values: List[Any] = []
    for entry in entries:
        values.extend(_list(entry.get("avoid_repeating") or entry.get("avoidRepeating")))
    return values


def _needs_regeneration(text: str, voice_check: Dict[str, Any], allow_time_specific_opening: bool) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    lower = stripped.lower()
    banned = (not allow_time_specific_opening) and any(lower.startswith(opening) for opening in BANNED_TIME_OPENINGS)
    if banned:
        return True
    if voice_check.get("starts_with_banned_time_phrase") is True and not allow_time_specific_opening:
        return True
    if voice_check.get("first_person_character_voice") is False:
        return True
    if voice_check.get("generic_summary") is True:
        return True
    return False
