from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Any, Dict, List

from openai import OpenAI

from services.journals import config


BANNED_TIME_OPENINGS = (
    "today",
    "yesterday",
    "this morning",
    "this evening",
)

JOURNAL_SHAPES = {
    "small_observed_detail",
    "private_worry",
    "gratitude_without_grandiosity",
    "remembered_user_fact",
    "relationship_thread",
    "quiet_resolution",
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "hers", "him", "his", "i", "in",
    "is", "it", "its", "me", "my", "myself", "of", "on", "or", "our", "she", "that",
    "the", "their", "them", "they", "this", "to", "us", "was", "we", "were",
    "with", "you", "your",
}

PRONOUNS = {
    "he", "her", "hers", "him", "his", "i", "it", "its", "me", "my", "our",
    "she", "their", "them", "they", "us", "we", "you", "your", "myself",
}


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


def _compact_profile(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    result: Dict[str, Any] = {}
    for key in ("id", "name", "displayName", "firstName", "profile"):
        value = data.get(key)
        if value is not None:
            result[key] = value
    return result


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
    prior_context = build_prior_journal_context(prior_journal_entries)
    trigger_classifications = _trigger_classifications(sessions)
    brief = build_journal_brief(
        journal_type=journal_type,
        character_name=character_name,
        character_data=character_data,
        user_data=user_data,
        coverage_window=coverage_window or {},
        sessions=sessions,
        trigger_classifications=trigger_classifications,
        prior_journal_context=prior_context,
    )
    repetition_profile = build_repetition_profile(
        prior_journal_entries=prior_journal_entries,
        protected_phrases=_list(brief.get("must_include_concrete_details")) + _list(brief.get("concreteAnchors")),
        user_data=user_data,
        character_data=character_data,
    )
    style = {
        "sentenceCount": "2 to 4 for regular, 2 to 3 for first, 2 to 4 for lure_back",
        "openingRule": "Do not start with Today, Yesterday, This morning, This evening, or the same shape as recent openings.",
        "endingRule": "Do not end with a generic hope/strength/light/connection sentence. End on the concrete feeling or detail from the brief.",
        "concretenessRule": "Include at least one required concrete detail exactly or near-exactly.",
    }
    if journal_type == "first":
        style["sentenceCount"] = "2 to 3 sentences for first journal"
        style["firstJournalRule"] = (
            f"This is your first journal entry after meeting {user_name}. Focus on what "
            "you learned about who they are. Subtly include the reminder hint only if it "
            "fits naturally: the character hopes the user asks them for help someday."
        )
    elif journal_type == "lure_back":
        style["lureBackRule"] = (
            f"It has been a while since you spoke with {user_name}. Remember something "
            "specific and meaningful about them. Do not say 'I miss you'. 2 to 4 sentences."
        )
    elif len(sessions) > 1:
        style["multiMomentRule"] = "Multiple moments may be provided; write one coherent private reflection. 5 to 6 sentences maximum."

    payload = {
        "characterName": character_name,
        "characterProfile": _compact_profile(character_data),
        "user": _compact_profile(user_data),
        "journalType": journal_type,
        "coverageWindow": coverage_window or {},
        "singleSameDayMoment": allow_time_specific_opening,
        "journalBrief": brief,
        "priorJournalContext": prior_context,
        "repetitionProfile": repetition_profile,
        "style": style,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You write BabyMilu character journals in the plushie character's own "
                "first-person voice. The writer is the character, not an assistant, "
                "narrator, therapist, product, or recap engine. Stay consistent with "
                "the character profile, but do not overuse dramatic catchphrases from "
                "the character profile. Write only from journalBrief. Do not introduce "
                "broad emotional atmosphere unless it is grounded in a concrete detail "
                "from the brief. Do not summarize the transcript. Do not mention being "
                "an AI or model. Do not use markdown. Do not use any hardBannedPhrases "
                "exactly. Avoid repeating recent opening or ending shapes. Return JSON only: "
                "{\"text\": string, \"thread_reference\": boolean, \"topicSummary\": "
                "string[], \"coverageSummary\": string[], \"concreteAnchors\": string[], "
                "\"emotionalThemes\": string[], \"avoidRepeating\": string[], "
                "\"journal_shape\": string, \"main_event\": string, "
                "\"thread_reference_reason\": string, \"thread_reference_targets\": "
                "string[], \"voice_check\": {\"first_person_character_voice\": boolean, "
                "\"generic_summary\": boolean, \"starts_with_banned_time_phrase\": boolean, "
                "\"uses_banned_phrase\": boolean, \"includes_required_concrete_detail\": boolean}}."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]
    data = _json_response(messages, temperature=0.7)
    text = str(data.get("text") or "").strip()
    quality = quality_check_journal_text(
        text=text,
        voice_check=data.get("voice_check") if isinstance(data.get("voice_check"), dict) else {},
        repetition_profile=repetition_profile,
        required_details=_list(brief.get("must_include_concrete_details")),
        allow_time_specific_opening=allow_time_specific_opening,
    )
    retry_quality = None
    if not quality["ok"]:
        retry_messages = messages + [
            {
                "role": "user",
                "content": (
                    "Rewrite once because the previous journal failed deterministic "
                    "quality checks. Failure report: "
                    f"{json.dumps(quality['failureReport'], ensure_ascii=False)}. "
                    "Keep the same journalBrief and meaning. Remove failed phrases. "
                    "Include at least one required concrete detail. Use a different "
                    "opening and ending shape. Do not add new facts."
                ),
            }
        ]
        data = _json_response(retry_messages, temperature=0.5)
        text = str(data.get("text") or "").strip()
        retry_quality = quality_check_journal_text(
            text=text,
            voice_check=data.get("voice_check") if isinstance(data.get("voice_check"), dict) else {},
            repetition_profile=repetition_profile,
            required_details=_list(brief.get("must_include_concrete_details")),
            allow_time_specific_opening=allow_time_specific_opening,
        )
        if not retry_quality["ok"]:
            raise RuntimeError(f"Journal generation failed quality checks: {retry_quality['failureReport']}")
    if not text:
        raise RuntimeError("Journal generation returned empty text")
    return {
        "text": text,
        "thread_reference": bool(data.get("thread_reference", brief.get("thread_reference", thread_reference))),
        "topicSummary": data.get("topicSummary") if isinstance(data.get("topicSummary"), list) else [],
        "coverageSummary": _list(data.get("coverageSummary")),
        "concreteAnchors": _list(data.get("concreteAnchors")),
        "emotionalThemes": _list(data.get("emotionalThemes")),
        "avoidRepeating": _list(data.get("avoidRepeating")),
        "journal_shape": str(data.get("journal_shape") or brief.get("journal_shape") or ""),
        "main_event": str(data.get("main_event") or brief.get("main_event") or ""),
        "thread_reference_reason": str(data.get("thread_reference_reason") or ""),
        "thread_reference_targets": _list(data.get("thread_reference_targets")),
        "voice_check": data.get("voice_check") if isinstance(data.get("voice_check"), dict) else {},
        "journalBrief": brief,
        "repetitionProfile": repetition_profile,
        "qualityCheck": retry_quality or quality,
        "retryAttempted": retry_quality is not None,
        "bannedPhrasesApplied": _list(repetition_profile.get("hardBannedPhrases")),
    }


def build_journal_brief(
    *,
    journal_type: str,
    character_name: str,
    character_data: Dict[str, Any],
    user_data: Dict[str, Any],
    coverage_window: Dict[str, Any],
    sessions: List[Dict[str, Any]],
    trigger_classifications: List[Dict[str, Any]],
    prior_journal_context: List[Dict[str, Any]],
) -> Dict[str, Any]:
    payload = {
        "characterName": character_name,
        "characterProfile": _compact_profile(character_data),
        "user": _compact_profile(user_data),
        "journalType": journal_type,
        "coverageWindow": coverage_window,
        "selectedConversationContext": sessions,
        "triggerClassifications": trigger_classifications,
        "priorJournalContext": prior_journal_context,
        "recentJournalShapes": [item.get("journalShape") for item in prior_journal_context if item.get("journalShape")],
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You prepare a concrete writing brief for a BabyMilu plushie character "
                "journal. Do not write the journal. Choose what this journal is specifically "
                "about so the final writer does not produce a generic emotional summary. "
                "Journals should feel rare, private, and specific. Choose one main event "
                "or user-revealing thread from the selected conversation context. Prefer "
                "concrete user details over broad emotional atmosphere. Use prior journal "
                "context only to avoid repeating already-covered concrete content. Do not "
                "imitate prior journal prose. Recurring emotions are allowed. Repeated "
                "concrete coverage is not. Return JSON only with: {\"main_event\": string, "
                "\"why_this_matters\": string, \"angle\": string, \"journal_shape\": "
                "\"small_observed_detail | private_worry | gratitude_without_grandiosity | "
                "remembered_user_fact | relationship_thread | quiet_resolution\", "
                "\"must_include_concrete_details\": string[], \"supporting_details\": "
                "string[], \"do_not_include\": string[], \"coverageSummary\": string[], "
                "\"concreteAnchors\": string[], \"emotionalThemes\": string[], "
                "\"avoidRepeating\": string[], \"thread_reference\": boolean, "
                "\"thread_reference_reason\": string, \"thread_reference_targets\": string[]}. "
                "Rules: must_include_concrete_details must come from selectedConversationContext. "
                "A valid concrete detail is a person, pet, object, game, place, routine, "
                "holiday, conflict, choice, task, preference, activity, or specific event. "
                "Do not choose vague details like warmth, connection, support, hope, sadness, "
                "tiredness, anxiety, shadows, burdens, or laughter unless paired with a concrete "
                "event/object/person. coverageSummary must describe concrete content covered "
                "by this journal, not poetic mood. avoidRepeating should describe concrete "
                "future dedup guidance. Set thread_reference=true only when this journal "
                "intentionally continues a prior concrete thread with new development."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]
    data = _json_response(messages, temperature=0.2)
    data.setdefault("main_event", "")
    data.setdefault("why_this_matters", "")
    data.setdefault("angle", "")
    data.setdefault("journal_shape", "remembered_user_fact")
    data.setdefault("must_include_concrete_details", [])
    data.setdefault("supporting_details", [])
    data.setdefault("do_not_include", [])
    data.setdefault("coverageSummary", [])
    data.setdefault("concreteAnchors", [])
    data.setdefault("emotionalThemes", [])
    data.setdefault("avoidRepeating", [])
    data.setdefault("thread_reference", False)
    data.setdefault("thread_reference_reason", "")
    data.setdefault("thread_reference_targets", [])
    if str(data.get("journal_shape")) not in JOURNAL_SHAPES:
        data["journal_shape"] = "remembered_user_fact"
    if not _list(data.get("must_include_concrete_details")):
        raise RuntimeError("Journal brief missing concrete details")
    return data


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


def build_prior_journal_context(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    context: List[Dict[str, Any]] = []
    for entry in entries[:10]:
        context.append(
            {
                "entryId": entry.get("_id") or entry.get("entryId"),
                "displayDate": entry.get("display_date") or entry.get("displayDate"),
                "journalType": entry.get("journal_type") or entry.get("journalType"),
                "coverageSummary": _list(entry.get("coverage_summary") or entry.get("coverageSummary")),
                "concreteAnchors": _list(entry.get("concrete_anchors") or entry.get("concreteAnchors")),
                "emotionalThemes": _list(entry.get("emotional_themes") or entry.get("emotionalThemes")),
                "avoidRepeating": _list(entry.get("avoid_repeating") or entry.get("avoidRepeating")),
                "journalShape": entry.get("journal_shape") or entry.get("journalShape"),
                "threadReferenceReason": entry.get("thread_reference_reason") or entry.get("threadReferenceReason"),
            }
        )
    return context


def build_repetition_profile(
    *,
    prior_journal_entries: List[Dict[str, Any]],
    protected_phrases: List[str],
    user_data: Dict[str, Any],
    character_data: Dict[str, Any],
    limit: int = 5,
) -> Dict[str, Any]:
    recent = [entry for entry in prior_journal_entries[:limit] if str(entry.get("text") or "").strip()]
    protected = {_normalize_text(item) for item in protected_phrases if str(item).strip()}
    name_tokens = _name_tokens(user_data, character_data)
    candidates = extract_phrase_candidates([str(entry.get("text") or "") for entry in recent])
    hard, soft = filter_phrase_candidates(candidates, protected, name_tokens)
    return {
        "hardBannedPhrases": hard[:20],
        "softAvoidPhrases": soft[:30],
        "recentOpeningPatterns": _recent_sentence_edges(recent, first=True),
        "recentEndingPatterns": _recent_sentence_edges(recent, first=False),
    }


def extract_phrase_candidates(texts: List[str]) -> Dict[str, Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "journals": set(), "opening": 0, "ending": 0, "words": []})
    for journal_index, text in enumerate(texts):
        sentences = _sentences(text)
        for sentence in sentences:
            tokens = _tokens(sentence)
            if not tokens:
                continue
            for phrase in (_edge_phrase(tokens, first=True), _edge_phrase(tokens, first=False)):
                if phrase:
                    key = " ".join(phrase)
                    stats[key]["count"] += 1
                    stats[key]["journals"].add(journal_index)
                    stats[key]["opening" if phrase == _edge_phrase(tokens, first=True) else "ending"] += 1
                    stats[key]["words"] = phrase
            for n in range(3, 7):
                for idx in range(0, max(len(tokens) - n + 1, 0)):
                    phrase = tokens[idx : idx + n]
                    key = " ".join(phrase)
                    stats[key]["count"] += 1
                    stats[key]["journals"].add(journal_index)
                    stats[key]["words"] = phrase
    return stats


def filter_phrase_candidates(
    candidates: Dict[str, Dict[str, Any]],
    protected_phrases: set[str],
    name_tokens: set[str],
) -> tuple[List[str], List[str]]:
    scored: List[tuple[int, str, Dict[str, Any]]] = []
    for phrase, stats in candidates.items():
        words = stats.get("words") or phrase.split()
        if not _phrase_can_be_banned(words, protected_phrases, name_tokens):
            continue
        journal_count = len(stats.get("journals") or [])
        score = (
            3 * int(stats.get("opening") or 0)
            + 3 * int(stats.get("ending") or 0)
            + 2 * max(int(stats.get("count") or 0) - 1, 0)
            + (1 if journal_count >= 2 else 0)
            + (1 if 3 <= len(words) <= 6 else 0)
        )
        if score >= 3:
            scored.append((score, phrase, stats))
    scored.sort(key=lambda item: (-item[0], item[1]))
    hard = [phrase for score, phrase, _ in scored if score >= 5]
    soft = [phrase for score, phrase, _ in scored if 3 <= score < 5 and phrase not in hard]
    return hard, soft


def quality_check_journal_text(
    *,
    text: str,
    voice_check: Dict[str, Any],
    repetition_profile: Dict[str, Any],
    required_details: List[str],
    allow_time_specific_opening: bool,
) -> Dict[str, Any]:
    normalized = _normalize_text(text)
    used = [
        phrase
        for phrase in _list(repetition_profile.get("hardBannedPhrases"))
        if phrase and _normalize_text(phrase) in normalized
    ]
    missing_detail = not _contains_required_detail(normalized, required_details)
    failure = {
        "hardBannedPhrasesUsed": used,
        "openingTooSimilar": _edge_too_similar(text, _list(repetition_profile.get("recentOpeningPatterns")), first=True),
        "endingTooSimilar": _edge_too_similar(text, _list(repetition_profile.get("recentEndingPatterns")), first=False),
        "missingConcreteDetail": missing_detail,
        "badTimeOpening": (not allow_time_specific_opening) and _starts_with_banned_time(text),
        "genericSummary": voice_check.get("generic_summary") is True,
        "badVoice": voice_check.get("first_person_character_voice") is False,
        "modelReportedBannedPhrase": voice_check.get("uses_banned_phrase") is True,
        "modelReportedMissingConcreteDetail": voice_check.get("includes_required_concrete_detail") is False,
    }
    ok = not any(bool(value) for value in failure.values())
    return {"ok": ok, "failureReport": failure}


def _trigger_classifications(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    classifications: List[Dict[str, Any]] = []
    for session in sessions:
        value = session.get("classification") if isinstance(session, dict) else None
        if isinstance(value, dict):
            classifications.append(value)
    return classifications


def _normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[^a-z0-9'\s]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str) -> List[str]:
    normalized = _normalize_text(value)
    return [token for token in normalized.split() if token]


def _sentences(text: str) -> List[str]:
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", str(text or "")) if item.strip()]


def _edge_phrase(tokens: List[str], *, first: bool) -> List[str]:
    if len(tokens) < 3:
        return []
    window = tokens[:8] if first else tokens[-8:]
    for size in range(min(6, len(window)), 2, -1):
        phrase = window[:size] if first else window[-size:]
        if _content_word_count(phrase) >= 2 and _stopword_ratio(phrase) < 0.6:
            return phrase
    return []


def _phrase_can_be_banned(words: List[str], protected_phrases: set[str], name_tokens: set[str]) -> bool:
    if len(words) < 3:
        return False
    phrase = " ".join(words)
    if phrase in protected_phrases:
        return False
    if any(phrase in protected or protected in phrase for protected in protected_phrases if protected):
        return False
    if _stopword_ratio(words) >= 0.6:
        return False
    if _content_word_count(words) < 2:
        return False
    meaningful = [word for word in words if word not in STOPWORDS]
    if meaningful and all(word in name_tokens or word in PRONOUNS for word in meaningful):
        return False
    if len(meaningful) <= 1 and any(word in name_tokens for word in meaningful):
        return False
    return True


def _stopword_ratio(words: List[str]) -> float:
    if not words:
        return 1.0
    return sum(1 for word in words if word in STOPWORDS) / len(words)


def _content_word_count(words: List[str]) -> int:
    return sum(1 for word in words if word not in STOPWORDS and word not in PRONOUNS)


def _name_tokens(user_data: Dict[str, Any], character_data: Dict[str, Any]) -> set[str]:
    raw: List[str] = []
    for data in (user_data, character_data):
        if not isinstance(data, dict):
            continue
        for key in ("name", "displayName", "firstName"):
            if data.get(key):
                raw.extend(_tokens(str(data.get(key))))
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        for key in ("name", "displayName"):
            if profile.get(key):
                raw.extend(_tokens(str(profile.get(key))))
    return set(raw)


def _recent_sentence_edges(entries: List[Dict[str, Any]], *, first: bool) -> List[str]:
    values: List[str] = []
    for entry in entries:
        for sentence in _sentences(str(entry.get("text") or "")):
            phrase = _edge_phrase(_tokens(sentence), first=first)
            if phrase:
                text = " ".join(phrase)
                if text not in values:
                    values.append(text)
            if len(values) >= 12:
                return values
    return values


def _edge_too_similar(text: str, patterns: List[str], *, first: bool) -> bool:
    sentences = _sentences(text)
    if not sentences:
        return True
    tokens = _edge_phrase(_tokens(sentences[0] if first else sentences[-1]), first=first)
    if not tokens:
        return False
    token_set = set(tokens)
    for pattern in patterns:
        pattern_tokens = set(_tokens(pattern))
        if len(pattern_tokens) < 3:
            continue
        overlap = len(token_set & pattern_tokens) / max(min(len(token_set), len(pattern_tokens)), 1)
        if overlap >= 0.75:
            return True
    return False


def _contains_required_detail(normalized_text: str, required_details: List[str]) -> bool:
    details = [_normalize_text(item) for item in required_details if str(item).strip()]
    if not details:
        return False
    text_words = _expanded_word_set(normalized_text.split())
    for detail in details:
        if detail and detail in normalized_text:
            return True
        words = [word for word in _expanded_word_set(detail.split()) if word not in STOPWORDS]
        if len(words) >= 2 and len(set(words) & text_words) >= 2:
            return True
        distinctive = [word for word in words if len(word) >= 5]
        if distinctive and any(word in text_words for word in distinctive):
            return True
    return False


def _expanded_word_set(words: List[str]) -> set[str]:
    expanded: set[str] = set()
    for word in words:
        expanded.update(_word_variants(word))
    return expanded


def _word_variants(word: str) -> set[str]:
    value = word.strip().lower()
    variants = {value} if value else set()
    if value.endswith("'s") and len(value) > 2:
        variants.add(value[:-2])
    if value.endswith("s") and len(value) > 3:
        variants.add(value[:-1])
    if value.endswith("ed") and len(value) > 4:
        stem = value[:-2]
        variants.add(stem)
        if len(stem) >= 2 and stem[-1] == stem[-2]:
            variants.add(stem[:-1])
    if value.endswith("ing") and len(value) > 5:
        variants.add(value[:-3])
    return {item for item in variants if item}


def _starts_with_banned_time(text: str) -> bool:
    lower = text.strip().lower()
    return any(lower.startswith(opening) for opening in BANNED_TIME_OPENINGS)


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
