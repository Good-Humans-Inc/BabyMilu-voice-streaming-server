from __future__ import annotations

from typing import Any, Dict


GENERIC_IDENTITY_DRIFT_MARKERS = (
    "i don't have a personal name",
    "i do not have a personal name",
    "i don't have a name",
    "i do not have a name",
    "just an ai assistant",
    "friendly ai assistant",
    "your ai assistant",
)


def normalize_identity_query(query) -> str:
    text = str(query or "").lower()
    normalized = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(normalized.split())


def is_forced_self_introduction_query(query) -> bool:
    normalized = normalize_identity_query(query)
    if not normalized:
        return False

    direct_markers = (
        "introduce yourself",
        "introduce your self",
        "tell me about yourself",
        "tell us about yourself",
        "who are you",
        "what are you",
        "what s your name",
        "whats your name",
        "what is your name",
        "what should i call you",
        "what can i call you",
        "do you have a name",
    )
    if any(marker in normalized for marker in direct_markers):
        return True

    tokens = set(normalized.split())
    if "name" in tokens and ("your" in tokens or "you" in tokens):
        return bool(tokens & {"what", "whats", "called", "call", "have"})
    return False


def is_intro_style_identity_query(query) -> bool:
    normalized = normalize_identity_query(query)
    return any(
        marker in normalized
        for marker in (
            "introduce yourself",
            "introduce your self",
            "tell me about yourself",
            "tell us about yourself",
            "who are you",
        )
    )


def identity_text_has_drift(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in GENERIC_IDENTITY_DRIFT_MARKERS)


def short_identity_sentence(text: str, max_chars: int = 150) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    for separator in (". ", "! ", "? "):
        if separator in value:
            value = value.split(separator, 1)[0].strip() + separator.strip()
            break
    if len(value) > max_chars:
        value = value[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:") + "."
    if value and value[-1] not in ".!?":
        value += "."
    return value


def identity_bio_sentence(bio: str) -> str:
    if not isinstance(bio, str) or not bio.strip():
        return ""
    pieces = [piece.strip() for piece in bio.split("|") if piece.strip()]
    for wanted_key in ("personality", "background", "speechstyle"):
        for piece in pieces:
            key, sep, value = piece.partition(":")
            if sep and key.strip().lower() == wanted_key:
                return short_identity_sentence(value)
    return short_identity_sentence(bio)


def extract_prompt_profile_value(prompt: str, label: str) -> str:
    prefix = f"{label}:"
    for line in str(prompt or "").splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if stripped.lower().startswith(prefix.lower()):
            return stripped[len(prefix):].strip()
    return ""


def build_forced_self_introduction_text(
    query,
    fields: Dict[str, Any],
    *,
    prompt: str = "",
    user_name: str = "",
) -> str:
    name = str((fields or {}).get("name") or "").strip()
    if not name:
        name = extract_prompt_profile_value(prompt, "Your Name")

    clean_user_name = str(user_name or "").strip()
    if clean_user_name.lower() == "unknown user":
        clean_user_name = ""

    if not name:
        return (
            "😄 [friendly] I'm your BabyMilu companion. "
            "My character name is still loading, but I'm right here with you."
        )

    if not is_intro_style_identity_query(query):
        return f"😄 [friendly] I'm {name}."

    parts = [f"😄 [friendly] I'm {name}."]
    bio_sentence = identity_bio_sentence(str((fields or {}).get("bio") or ""))
    if bio_sentence:
        parts.append(bio_sentence)
    relationship = str((fields or {}).get("relationship") or "").strip()
    if relationship:
        parts.append(short_identity_sentence(relationship))
    elif clean_user_name:
        parts.append(f"I'm here with {clean_user_name}.")
    return " ".join(part for part in parts if part)
