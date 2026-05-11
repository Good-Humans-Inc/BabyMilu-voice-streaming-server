from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from openai import OpenAI

from services.journals import config


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
                "character journal entry. Be stingy. Return JSON only with keys: "
                "should_journal boolean, primary_trigger string, anchor_words string array, "
                "sensory_moment string, new_fact_detected boolean, dedup_clear boolean, "
                "topicSummary string array, reason string. dedup_clear is false only when "
                "the dominant topic overlaps prior journals and the emotional trajectory "
                "has not meaningfully changed."
            ),
        },
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)},
    ]
    data = _json_response(messages, temperature=0.1)
    data.setdefault("should_journal", False)
    data.setdefault("dedup_clear", bool(data.get("should_journal")))
    data.setdefault("topicSummary", [])
    data.setdefault("anchor_words", [])
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
) -> Dict[str, Any]:
    profile = character_data.get("profile") if isinstance(character_data.get("profile"), dict) else {}
    character_name = (
        profile.get("name") or character_data.get("name") or "Milu"
    )
    user_name = user_data.get("name") or user_data.get("displayName") or "the user"
    style = (
        "Write 2 to 4 sentences. Do not summarize the session. Write as a private diary "
        "from the character's first-person perspective. Use sensory or emotional detail. "
        "No platitudes, no AI-speak, no markdown."
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
        style += " Two separate moments happened today; honor contrast if it exists. 5 to 6 sentences maximum."

    payload = {
        "characterName": character_name,
        "characterProfile": character_data,
        "user": user_data,
        "systemMemoryBlock": system_memory_block,
        "queuedSessions": sessions,
        "priorJournalEntries": prior_journal_entries,
        "threadReferenceRequired": thread_reference,
        "style": style,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You write BabyMilu character journals. Return JSON only: "
                "{\"text\": string, \"thread_reference\": boolean, \"topicSummary\": string[]}."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]
    data = _json_response(messages, temperature=0.7)
    text = str(data.get("text") or "").strip()
    if not text:
        raise RuntimeError("Journal generation returned empty text")
    return {
        "text": text,
        "thread_reference": bool(data.get("thread_reference", thread_reference)),
        "topicSummary": data.get("topicSummary") if isinstance(data.get("topicSummary"), list) else [],
    }
