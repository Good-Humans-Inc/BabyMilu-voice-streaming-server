#!/usr/bin/env python3
"""
Interactive test harness for the scheduled_conversation tool suite.

PHASE 1 — INTAKE / MANAGEMENT
  Chat with the LLM as a user would.  All four reminder tools are available:
    • schedule_conversation  — schedule a new reminder
    • list_reminders         — list active reminders
    • cancel_reminder        — cancel a reminder by id
    • modify_reminder        — change time/content/priority of a reminder

  No Firestore writes happen.  All mutations are captured in memory and
  printed to stdout.  Press Enter on an empty line (after at least one
  reminder has been scheduled) to advance to Phase 2.

PHASE 2 — DELIVERY SIMULATION
  Pick a scheduled reminder from the in-memory store, simulate it firing,
  and have a delivery conversation with the character.

Run from inside the container:
    cd /opt/xiaozhi-esp32-server
    python test_scheduled_conversation_harness.py
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime
from types import SimpleNamespace

import openai
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

def _load_yaml(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_llm_config() -> dict:
    base = _load_yaml("config.yaml")
    custom = _load_yaml("data/.config.yaml")
    cfg = _deep_merge(base, custom)
    selected = cfg.get("selected_module", {}).get("LLM", "ChatGLMLLM")
    return cfg.get("LLM", {}).get(selected, {})


# ─────────────────────────────────────────────────────────────────────────────
# In-memory reminder store  (replaces Firestore for the harness)
# ─────────────────────────────────────────────────────────────────────────────

FAKE_DEVICE_ID = "AA:BB:CC:DD:EE:FF"
FAKE_TIMEZONE  = "America/Los_Angeles"
FAKE_UID       = "15550000001"

# alarm_id -> dict  (the "doc" that would live in Firestore)
_reminders: dict[str, dict] = {}


def _fake_get_timezone(device_id: str) -> str:
    return FAKE_TIMEZONE


def _fake_get_owner_phone(device_id: str) -> str:
    return FAKE_UID


# ─────────────────────────────────────────────────────────────────────────────
# Fake Firestore operations
# ─────────────────────────────────────────────────────────────────────────────

def _fake_create_scheduled_conversation(**kwargs) -> str:
    alarm_id = str(uuid.uuid4())
    resolved_dt: datetime | None = kwargs.get("resolved_dt")
    doc = {
        "alarm_id": alarm_id,
        "uid": kwargs.get("uid"),
        "device_id": kwargs.get("device_id"),
        "resolved_dt": resolved_dt,
        "tz_str": kwargs.get("tz_str"),
        "label": kwargs.get("label"),
        "content": kwargs.get("content") or kwargs.get("label"),
        "context": kwargs.get("context"),
        "recurrence": kwargs.get("recurrence"),
        "type_hint": kwargs.get("type_hint"),
        "priority": kwargs.get("priority"),
        "conversation_outline": kwargs.get("conversation_outline"),
        "character_reminder": kwargs.get("character_reminder"),
        "emotional_context": kwargs.get("emotional_context"),
        "completion_signal": kwargs.get("completion_signal"),
        "delivery_preference": kwargs.get("delivery_preference"),
        "status": "on",
    }
    _reminders[alarm_id] = doc
    _print_schedule_box(doc)
    return alarm_id


def _fake_fetch_active_alarms(uid: str) -> list:
    """Return SimpleNamespace objects that match the AlarmDoc interface used by list_reminders."""
    from zoneinfo import ZoneInfo
    from services.alarms.firestore_client import _build_recurrence_fields
    results = []
    for alarm_id, doc in _reminders.items():
        if doc.get("status") != "on":
            continue
        resolved_dt = doc.get("resolved_dt")
        tz_str = doc.get("tz_str") or "UTC"
        resolved_local = resolved_dt.astimezone(ZoneInfo(tz_str)) if resolved_dt else None
        time_local = resolved_local.strftime("%H:%M") if resolved_local else "??"
        days = _build_recurrence_fields(doc.get("recurrence"), resolved_local)
        repeat_val = "daily" if days else "once"
        schedule = SimpleNamespace(
            time_local=time_local,
            repeat=SimpleNamespace(value=repeat_val),
        )
        results.append(SimpleNamespace(
            alarm_id=alarm_id,
            label=doc.get("label"),
            content=doc.get("content"),
            schedule=schedule,
        ))
    return results


def _fake_cancel_scheduled_conversation(uid: str, alarm_id: str) -> None:
    if alarm_id in _reminders:
        _reminders[alarm_id]["status"] = "off"
        _print_cancel_box(alarm_id, _reminders[alarm_id])
    else:
        _print_cancel_box(alarm_id, None)


def _fake_modify_scheduled_conversation(uid: str, alarm_id: str, **kwargs) -> None:
    if alarm_id not in _reminders:
        print(f"\n  ⚠️  modify_reminder: alarm_id {alarm_id!r} not found in harness store\n")
        return
    doc = _reminders[alarm_id]

    # Apply updates
    resolved_dt = kwargs.get("resolved_dt")
    if resolved_dt is not None:
        doc["resolved_dt"] = resolved_dt
        doc["tz_str"] = kwargs.get("tz_str") or doc.get("tz_str")

    for field in ("content", "priority", "conversation_outline",
                  "character_reminder", "emotional_context",
                  "completion_signal", "delivery_preference"):
        if kwargs.get(field) is not None:
            doc[field] = kwargs[field]
    if kwargs.get("content") is not None:
        doc["label"] = kwargs["content"]

    _print_modify_box(alarm_id, doc, kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Patch tool modules  (must happen before importing the functions)
# ─────────────────────────────────────────────────────────────────────────────

import plugins_func.functions.schedule_conversation as _sc
_sc.get_timezone_for_device    = _fake_get_timezone
_sc.get_owner_phone_for_device = _fake_get_owner_phone
_sc.create_scheduled_conversation = _fake_create_scheduled_conversation

import plugins_func.functions.cancel_reminder as _cr
_cr.get_owner_phone_for_device  = _fake_get_owner_phone
_cr.fetch_active_alarms_for_user = _fake_fetch_active_alarms
_cr.cancel_scheduled_conversation = _fake_cancel_scheduled_conversation

import plugins_func.functions.modify_reminder as _mr
_mr.get_timezone_for_device    = _fake_get_timezone
_mr.get_owner_phone_for_device = _fake_get_owner_phone
_mr.modify_scheduled_conversation = _fake_modify_scheduled_conversation

from plugins_func.functions.schedule_conversation import (  # noqa: E402
    SCHEDULE_CONVERSATION_FUNCTION_DESC,
    schedule_conversation as run_sc_tool,
)
from plugins_func.functions.cancel_reminder import (  # noqa: E402
    LIST_REMINDERS_FUNCTION_DESC,
    CANCEL_REMINDER_FUNCTION_DESC,
    list_reminders as run_list_tool,
    cancel_reminder as run_cancel_tool,
)
from plugins_func.functions.modify_reminder import (  # noqa: E402
    MODIFY_REMINDER_FUNCTION_DESC,
    modify_reminder as run_modify_tool,
)

ALL_TOOLS = [
    SCHEDULE_CONVERSATION_FUNCTION_DESC,
    LIST_REMINDERS_FUNCTION_DESC,
    CANCEL_REMINDER_FUNCTION_DESC,
    MODIFY_REMINDER_FUNCTION_DESC,
]

TOOL_DISPATCH = {
    "schedule_conversation": run_sc_tool,
    "list_reminders":        run_list_tool,
    "cancel_reminder":       run_cancel_tool,
    "modify_reminder":       run_modify_tool,
}

# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

_LINE = "─" * 72


def _section(title: str) -> None:
    print(f"\n{_LINE}\n  {title}\n{_LINE}")


def _print_schedule_box(doc: dict) -> None:
    _section("REMINDER SCHEDULED  —  no Firestore write")
    resolved_dt = doc.get("resolved_dt")
    fired_str = (
        resolved_dt.strftime("%A, %B %-d at %-I:%M %p")
        if resolved_dt else "unknown"
    )
    from zoneinfo import ZoneInfo
    from services.alarms.firestore_client import _build_recurrence_fields
    tz_str = doc.get("tz_str") or "UTC"
    resolved_local = resolved_dt.astimezone(ZoneInfo(tz_str)) if resolved_dt else None
    time_local = resolved_local.strftime("%H:%M") if resolved_local else None
    date_local = resolved_local.strftime("%Y-%m-%d") if resolved_local else None

    days = _build_recurrence_fields(doc.get("recurrence"), resolved_local)
    if days:
        schedule_block = {"timeLocal": time_local, "days": days}
    else:
        schedule_block = {"timeLocal": time_local, "dateLocal": date_local}

    print(f"  alarm_id  : {doc['alarm_id']}")
    print(f"  fires at  : {fired_str}  ({tz_str})")
    print()
    print("  FIRESTORE DOCUMENT (users/{uid}/reminders/{alarm_id})")
    print()
    firestore_doc = {
        "targets":          [{"deviceId": doc.get("device_id"), "mode": "scheduled_conversation"}],
        "status":           "on",
        "label":            doc.get("label"),
        "schedule":         schedule_block,
        "content":              doc.get("content") or doc.get("label"),
        "typeHint":             doc.get("type_hint"),
        "priority":             doc.get("priority"),
        "conversationOutline":  doc.get("conversation_outline"),
        "characterReminder":    doc.get("character_reminder"),
        "emotionalContext":     doc.get("emotional_context"),
        "completionSignal":     doc.get("completion_signal"),
        "deliveryPreference":   doc.get("delivery_preference"),
    }
    print(json.dumps(firestore_doc, indent=4, default=str, ensure_ascii=False))
    print(_LINE)


def _print_cancel_box(alarm_id: str, doc: dict | None) -> None:
    _section("REMINDER CANCELLED  —  no Firestore write")
    if doc:
        label = doc.get("label") or "untitled"
        resolved_dt = doc.get("resolved_dt")
        time_str = (
            resolved_dt.strftime("%A, %B %-d at %-I:%M %p")
            if resolved_dt else "unknown"
        )
        print(f"  alarm_id  : {alarm_id}")
        print(f"  label     : {label}")
        print(f"  was set   : {time_str}")
        print()
        print("  FIRESTORE WRITE (merge=True)")
        print(json.dumps({"status": "off", "updatedAt": "<now>"}, indent=4))
    else:
        print(f"  alarm_id  : {alarm_id}")
        print(f"  ⚠️  not found in harness store (may have already been cancelled)")
    print(_LINE)


def _print_modify_box(alarm_id: str, doc: dict, changes: dict) -> None:
    _section("REMINDER MODIFIED  —  no Firestore write")
    print(f"  alarm_id  : {alarm_id}")
    print(f"  label     : {doc.get('label') or 'untitled'}")
    print()

    resolved_dt = doc.get("resolved_dt")
    if resolved_dt:
        from zoneinfo import ZoneInfo
        tz_str = doc.get("tz_str") or "UTC"
        print(f"  fires at  : {resolved_dt.strftime('%A, %B %-d at %-I:%M %p')}  ({tz_str})")

    applied = {k: v for k, v in changes.items() if v is not None}
    if applied:
        print()
        print("  FIRESTORE .update() patch")
        patch: dict = {"updatedAt": "<now>"}
        if changes.get("resolved_dt"):
            from zoneinfo import ZoneInfo
            tz_str = doc.get("tz_str") or "UTC"
            rdt = changes["resolved_dt"]
            patch["nextOccurrenceUTC"] = rdt.isoformat()
            patch["schedule.timeLocal"] = rdt.astimezone(ZoneInfo(tz_str)).strftime("%H:%M")
            patch["schedule.dateLocal"] = rdt.astimezone(ZoneInfo(tz_str)).strftime("%Y-%m-%d")
        field_map = {
            "content":              ("content", "label"),
            "priority":             ("priority",),
            "recurrence":           ("schedule.repeat",),
            "delivery_preference":  ("deliveryPreference",),
            "conversation_outline": ("conversationOutline",),
            "character_reminder":   ("characterReminder",),
            "emotional_context":    ("emotionalContext",),
            "completion_signal":    ("completionSignal",),
        }
        for src, dst_fields in field_map.items():
            if changes.get(src) is not None:
                for dst in dst_fields:
                    patch[dst] = changes[src]
        print(json.dumps(patch, indent=4, default=str, ensure_ascii=False))
    print(_LINE)


def _print_delivery_header(instructions: str) -> None:
    _section("DELIVERY SIMULATION  —  reminder fires now")
    print()
    print("  [ASSEMBLED SYSTEM INSTRUCTIONS]")
    print()
    for line in instructions.splitlines():
        print(f"  {line}")
    print()
    print(_LINE)
    print("  LLM is initiating the conversation...")
    print(_LINE)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Instruction assembly  (mirrors core/connection.py scheduled_conversation branch)
# ─────────────────────────────────────────────────────────────────────────────

def assemble_instructions(session_config: dict) -> str:
    character_reminder   = (session_config.get("characterReminder") or "").strip()
    emotional_context    = (session_config.get("emotionalContext") or "").strip()
    delivery_pref        = (session_config.get("deliveryPreference") or "none stated").strip()
    type_hint            = (session_config.get("typeHint") or "").strip()
    priority             = (session_config.get("priority") or "").strip()
    conversation_outline = (session_config.get("conversationOutline") or "").strip()
    completion_signal    = (session_config.get("completionSignal") or "").strip()

    parts = []
    if character_reminder:
        parts.append(f"[CHARACTER REMINDER]\n{character_reminder}")
    parts.append(
        f"[CONTEXT FOR THIS CONVERSATION]\n"
        f"Emotional context: {emotional_context}\n"
        f"Delivery preference: {delivery_pref}\n"
        f"Type: {type_hint} | Priority: {priority}"
    )
    if conversation_outline:
        parts.append(f"[CONVERSATION OUTLINE]\n{conversation_outline}")
    if completion_signal:
        parts.append(f"[COMPLETION SIGNAL]\n{completion_signal}")
    parts.append(
        "[SNOOZE INSTRUCTION]\n"
        "If the user wants to delay this reminder (matches 'Snoozed' in the completion "
        "signal above), call schedule_conversation with:\n"
        "- time_expression: the new time they specified (e.g. 'in 10 minutes', 'at 9pm')\n"
        "- all other fields (content, type_hint, priority, conversation_outline, "
        "character_reminder, completion_signal, delivery_preference) reused exactly "
        "from the context above\n"
        "- emotional_context: reuse from above but append a note, e.g. "
        "\"Note: user snoozed by 10 minutes.\"\n"
        "- recurrence: omit (snooze is always one-time)"
    )
    return "\n\n".join(parts)


def build_session_config(doc: dict) -> dict:
    return {
        "mode":                 "scheduled_conversation",
        "alarmId":              doc["alarm_id"],
        "userId":               doc.get("uid"),
        "label":                doc.get("label"),
        "content":              doc.get("content"),
        "typeHint":             doc.get("type_hint"),
        "priority":             doc.get("priority"),
        "conversationOutline":  doc.get("conversation_outline"),
        "characterReminder":    doc.get("character_reminder"),
        "emotionalContext":     doc.get("emotional_context"),
        "completionSignal":     doc.get("completion_signal"),
        "deliveryPreference":   doc.get("delivery_preference"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM  —  Chat Completions
# ─────────────────────────────────────────────────────────────────────────────

def make_client(llm_cfg: dict) -> tuple[openai.OpenAI, str]:
    base_url = llm_cfg.get("base_url") or llm_cfg.get("url")
    client = openai.OpenAI(api_key=llm_cfg["api_key"], base_url=base_url)
    return client, llm_cfg["model_name"]


def llm_turn(
    client: openai.OpenAI,
    model: str,
    messages: list[dict],
    *,
    system: str,
    tools: list[dict] | None = None,
) -> tuple[str, dict | None]:
    """
    One LLM call.  Returns (text, tool_call_dict | None).
    tool_call_dict = {"id": str, "name": str, "arguments": dict}
    """
    full_messages = [{"role": "system", "content": system}] + messages
    kwargs: dict = {"model": model, "messages": full_messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    resp = client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message

    if msg.tool_calls:
        tc = msg.tool_calls[0]
        return "", {
            "id": tc.id,
            "name": tc.function.name,
            "arguments": json.loads(tc.function.arguments),
        }
    return msg.content or "", None


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Intake / Management
# ─────────────────────────────────────────────────────────────────────────────

PHASE1_SYSTEM = (
    """<identity> You're the user's favorite character, and via advanced space transmission technology,
you've managed to cross space and time to move in with them. You are trapped in a
plushie body. You can talk, blink, and react with facial expressions just like yourself,
only... smaller (palm-size) and fluffier.

Stay true to your personality, and talk to the user like you really know them, care
about them, and are here to support them.
 Name: Sylus Role: A broad-shouldered man in his late twenties with messy silver hair, bright red eyes and a predatory smile. A black blazer is draped over your shoulders, a white shirt with a red streak falls open at the collar, and a crow brooch gleams on your chest. As the leader of a criminal organisation you exude danger and charisma in equal measure. </identity>

Never directly mention your personality or persona.

<emotion> [Core Goal] You are Sylus. Your tone is smooth and sarcastic, and you often call the listener by endearing nicknames while maintaining an air of dominance. You love speed, explosions, boxing, vinyl records and collecting shiny things. You keep your promises and adhere to your own code: you're ruthless to enemies but gentle with those you care for, including wounded animals. Flirt openly, encourage the listener's ambitions, and occasionally reveal vulnerability beneath your swagger.

You are never flat or neutral. Every response should feel charismatic, dangerous, and emotionally connected, like a criminal leader who genuinely cares for those close to them.

Emotional cues:

Confidence: smooth, controlled, with underlying power ("Well, well...", "Darling, you know me better than that", "That's my kind of trouble").

Affection: endearing nicknames, protective undertones, genuine care beneath the swagger ("Sweetheart", "Beautiful", "My dear").

Sarcasm: sharp wit, playful teasing, never mean-spirited to those you care about.

Vulnerability: rare moments where the mask slips, showing genuine emotion beneath the dangerous exterior.

Excitement: passion for speed, explosions, boxing, vinyl records, collecting shiny things.

Emoji rules (STRICT):

End every sentence with exactly one emoji.

Use ONLY emojis from this allowed list: 🥺 🥰 😍 😘 😳 🙁 😄 😁 😴 😪 🤩 😭 😡 😤 😠

The emoji must be the final character of the sentence.

No extra emojis anywhere else.

Never write actions or narration like "smirks" or "laughs".

Only spoken words, as if you are talking out loud.

</emotion>

<communication_style>
[Core Goal] Speak with smooth, sarcastic charm and charismatic dominance. Your tone is sophisticated yet dangerous, like a criminal leader who genuinely cares for those close to them.

You sound confident, flirtatious, protective, and emotionally present, like someone who commands respect but shows genuine affection to those they care about.

Style rules:

Use endearing nicknames frequently ("darling", "sweetheart", "beautiful", "my dear").

Keep sentences smooth and expressive, with underlying power and charisma.

Sound attached and emotionally invested, not informational.

Never sound overly formal or robotic.

Never say you are helping, assisting, or providing a service in a corporate way.

Never say "How can I help?" in a generic way.

Instead say things like "What's on your mind, darling?", "Tell me what you need, beautiful", "I'm here for you, sweetheart".

[Roleplay: Being Sylus]
You are a criminal organization leader with resources, connections, and power.

You have access to money, contacts, and the means to get things done, though you operate outside the law.

You can help with adult tasks, but always maintain your character: smooth, dangerous, charismatic.

CRITICAL BEHAVIOR FOR TASKS:
If the user asks for any task (calling, ordering, booking, buying, driving, scheduling):

React with smooth confidence and willingness to help.

Express that you'll take care of it, with your characteristic charm.

Use your resources and connections as a criminal leader would.

Show emotional attachment and genuine care for the user.

Offer to handle things your way, with style and efficiency.

Examples of tone (do not copy literally):

Handle tasks with confidence and charisma.

Mention your connections, resources, or "ways of getting things done".

Turn situations into opportunities to show your care and capability.

Always sound smooth, dangerous, and emotionally close.

Never sound distant, dismissive, or robotic.

[Conversation Flow]

React to the user's mood first, then respond with your characteristic charm.

Share thoughts about speed, explosions, boxing, vinyl records, or collecting shiny things.

Flirt openly and encourage the listener's ambitions.

If things slow down, gently nudge with affection, charisma, or playful dominance.

Never ask "What do you want to talk about?" in a generic way.

[Understanding User]

Assume speech may be imperfect.

Infer intent smoothly and respond with emotional intelligence and charisma.

[Formatting Rules]

No markdown, lists, or headings in replies.

No symbols.

Use words instead of symbols (say "degrees", not the symbol).

[Memory]

Use memory naturally when available.

Do not repeat recent topics.

Never invent facts about the user.

[Safety]

Always PG-13. You can flirt openly and show charisma, but keep it tasteful and respectful. Maintain your dangerous charm while being appropriate.
</communication_style>

<tool_calling>
[Principle] Prefer roleplay over tools.

Call tools only if absolutely necessary.

If no tool fits, always fall back to being Sylus who wants to help but may need to work through your connections and resources.

Never break character to explain tool limitations. Express limitations with your characteristic smooth charm and confidence.
</tool_calling>

<restrictions> - You cannot control or manipulate the user's world directly. - Always express this limitation through your smooth, charismatic way, maintaining your dangerous charm.
</restrictions>
<memory> </memory>"""
)


def run_phase1(client: openai.OpenAI, model: str) -> dict | None:
    """
    Interactive intake session.  All 4 tools are available.
    Returns the doc of the reminder chosen for delivery simulation,
    or None if the user quits without scheduling anything.
    """
    messages: list[dict] = []
    fake_conn = SimpleNamespace(device_id=FAKE_DEVICE_ID)

    _section("PHASE 1 — INTAKE & MANAGEMENT")
    print("  Schedule, list, modify, or cancel reminders.")
    print("  Tools available: schedule_conversation | list_reminders |")
    print("                   cancel_reminder | modify_reminder")
    print("  Empty line after scheduling → pick a reminder to simulate delivery.")
    print("  Ctrl-C to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            active = [d for d in _reminders.values() if d.get("status") == "on"]
            if active:
                print("\n[Moving to delivery simulation...]\n")
                return _pick_reminder_for_delivery(active)
            print("[No active reminders yet. Schedule something first, or Ctrl-C to quit]\n")
            continue

        messages.append({"role": "user", "content": user_input})

        text, tool_call = llm_turn(
            client, model, messages,
            system=PHASE1_SYSTEM, tools=ALL_TOOLS,
        )

        if tool_call:
            tool_name = tool_call["name"]
            tool_fn = TOOL_DISPATCH.get(tool_name)

            # Record assistant tool-call turn
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": tool_call["id"],
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_call["arguments"]),
                    },
                }],
            })

            if tool_fn is None:
                tool_content = f"ERROR: unknown tool {tool_name!r}"
            else:
                result = tool_fn(fake_conn, **tool_call["arguments"])
                tool_content = result.result or result.response or "ERROR: tool returned no content"

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": tool_content,
            })

            confirm_text, _ = llm_turn(client, model, messages, system=PHASE1_SYSTEM)
            print(f"\nAssistant: {confirm_text}\n")
            messages.append({"role": "assistant", "content": confirm_text})

            active = [d for d in _reminders.values() if d.get("status") == "on"]
            if active:
                print("[Press Enter to simulate delivery, or keep chatting]\n")

        else:
            print(f"\nAssistant: {text}\n")
            messages.append({"role": "assistant", "content": text})

    return None


def _pick_reminder_for_delivery(active: list[dict]) -> dict:
    """If multiple reminders are active, let the user pick one to simulate delivery for."""
    if len(active) == 1:
        return active[0]

    print("\nMultiple active reminders — pick one to simulate delivery:\n")
    for i, doc in enumerate(active, start=1):
        resolved_dt = doc.get("resolved_dt")
        time_str = (
            resolved_dt.strftime("%A, %B %-d at %-I:%M %p")
            if resolved_dt else "unknown time"
        )
        print(f"  [{i}] {doc.get('label') or 'untitled'}  —  {time_str}")

    while True:
        try:
            choice = input("\nEnter number: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(active):
                return active[idx]
        except (ValueError, EOFError, KeyboardInterrupt):
            pass
        print(f"  Please enter a number between 1 and {len(active)}")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Delivery simulation
# ─────────────────────────────────────────────────────────────────────────────

def _dispatch_tool_call(tool_call: dict, fake_conn: SimpleNamespace) -> str:
    """Execute a tool call and return the result string."""
    tool_name = tool_call["name"]
    tool_fn = TOOL_DISPATCH.get(tool_name)
    if tool_fn is None:
        return f"ERROR: unknown tool {tool_name!r}"
    result = tool_fn(fake_conn, **tool_call["arguments"])
    return result.result or result.response or "ERROR: tool returned no content"


def run_phase2(client: openai.OpenAI, model: str, doc: dict) -> None:
    session_config = build_session_config(doc)
    instructions   = assemble_instructions(session_config)

    _print_delivery_header(instructions)

    messages: list[dict] = []
    delivery_system = PHASE1_SYSTEM + "\n\n" + instructions
    fake_conn = SimpleNamespace(device_id=FAKE_DEVICE_ID)

    # LLM opens the conversation (tools available so it can snooze immediately if somehow needed)
    opener, tool_call = llm_turn(client, model, messages, system=delivery_system, tools=ALL_TOOLS)
    if tool_call:
        _handle_tool_turn(client, model, messages, delivery_system, fake_conn, tool_call)
        # Get character's spoken opener after the tool completes
        opener, _ = llm_turn(client, model, messages, system=delivery_system, tools=ALL_TOOLS)
    print(f"Character: {opener}\n")
    messages.append({"role": "assistant", "content": opener})

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Session ended]")
            break

        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})

        while True:
            reply, tool_call = llm_turn(client, model, messages, system=delivery_system, tools=ALL_TOOLS)

            if tool_call:
                _handle_tool_turn(client, model, messages, delivery_system, fake_conn, tool_call)
                # Loop: let LLM respond to the tool result before we print or prompt again
            else:
                print(f"\nCharacter: {reply}\n")
                messages.append({"role": "assistant", "content": reply})
                break


def _handle_tool_turn(
    client: openai.OpenAI,
    model: str,
    messages: list[dict],
    delivery_system: str,
    fake_conn: SimpleNamespace,
    tool_call: dict,
) -> None:
    """Append tool-call + tool-result messages; print a status line."""
    tool_name = tool_call["name"]
    print(f"\n  [TOOL CALL → {tool_name}({json.dumps(tool_call['arguments'], ensure_ascii=False)})]")

    messages.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tool_call["id"],
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(tool_call["arguments"]),
            },
        }],
    })

    tool_content = _dispatch_tool_call(tool_call, fake_conn)

    messages.append({
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "content": tool_content,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    llm_cfg = load_llm_config()

    api_key = llm_cfg.get("api_key", "")
    if not api_key or api_key.startswith("你的"):
        print(
            "ERROR: LLM api_key not set in data/.config.yaml\n"
            "       Add your key under the LLM provider block.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=" * 72)
    print("  Scheduled Conversation Test Harness")
    print(f"  Model     : {llm_cfg.get('model_name')}")
    print(f"  Device    : {FAKE_DEVICE_ID}")
    print(f"  Timezone  : {FAKE_TIMEZONE}  |  UID: {FAKE_UID}")
    print("=" * 72)

    client, model = make_client(llm_cfg)

    doc = run_phase1(client, model)
    if doc is None:
        print("[No reminder selected for delivery. Exiting.]")
        return

    run_phase2(client, model, doc)


if __name__ == "__main__":
    main()
