#!/usr/bin/env python3
"""
Interactive test harness for the scheduled_conversation tool.

PHASE 1 — INTAKE
  Chat with the LLM as a user would.  When the LLM calls schedule_conversation
  the Firestore payload is printed to stdout instead of written.  Press Enter
  on an empty line (after an alarm has been scheduled) to advance to Phase 2.

PHASE 2 — DELIVERY SIMULATION
  The alarm fires.  The assembled system-prompt is printed, then the LLM starts
  the conversation and you respond as the user.

Run from inside the container:
    cd /opt/xiaozhi-esp32-server
    python test_scheduled_conversation_harness.py
"""
from __future__ import annotations

import json
import sys
import uuid
from types import SimpleNamespace

import openai
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Config — read YAML directly to avoid the full config_loader import chain
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
# Fake device / Firestore interceptors
# ─────────────────────────────────────────────────────────────────────────────

FAKE_DEVICE_ID = "AA:BB:CC:DD:EE:FF"
FAKE_TIMEZONE  = "America/Los_Angeles"
FAKE_UID       = "15550000001"

_captured_alarm: dict | None = None


def _fake_get_timezone(device_id: str) -> str:
    return FAKE_TIMEZONE


def _fake_get_owner_phone(device_id: str) -> str:
    return FAKE_UID


def _fake_create_scheduled_conversation(**kwargs) -> str:
    global _captured_alarm
    alarm_id = str(uuid.uuid4())
    _captured_alarm = {"alarm_id": alarm_id, **kwargs}
    _print_alarm_box(_captured_alarm)
    return alarm_id


# Patch before importing schedule_conversation so the module sees our fakes
import plugins_func.functions.schedule_conversation as _sc

_sc.get_timezone_for_device   = _fake_get_timezone
_sc.get_owner_phone_for_device = _fake_get_owner_phone
_sc.create_scheduled_conversation = _fake_create_scheduled_conversation

from plugins_func.functions.schedule_conversation import (  # noqa: E402
    SCHEDULE_CONVERSATION_FUNCTION_DESC,
    schedule_conversation as run_sc_tool,
)

# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

_LINE = "─" * 72


def _section(title: str) -> None:
    print(f"\n{_LINE}\n  {title}\n{_LINE}")


def _print_alarm_box(alarm: dict) -> None:
    _section("ALARM CAPTURED  —  no Firestore write")

    resolved_dt = alarm.get("resolved_dt")
    fired_str = (
        resolved_dt.strftime("%A, %B %-d at %-I:%M %p")
        if resolved_dt else "unknown"
    )

    print(f"  alarm_id  : {alarm['alarm_id']}")
    print(f"  uid       : {alarm.get('uid')}")
    print(f"  device_id : {alarm.get('device_id')}")
    print(f"  fires at  : {fired_str}  ({alarm.get('tz_str')})")
    print()
    print("  FIRESTORE DOCUMENT (what would be written)")
    print()

    doc = {
        "targets": [{"deviceId": alarm.get("device_id"), "mode": "scheduled_conversation"}],
        "status": "on",
        "label": alarm.get("label"),
        "schedule": {
            "repeat": "none",
            "timeLocal": resolved_dt.strftime("%H:%M") if resolved_dt else None,
            "days": [resolved_dt.strftime("%Y-%m-%d")] if resolved_dt else [],
        },
        "content": alarm.get("content") or alarm.get("label"),
        "typeHint": alarm.get("type_hint"),
        "priority": alarm.get("priority"),
        "conversationOutline": alarm.get("conversation_outline"),
        "characterReminder": alarm.get("character_reminder"),
        "emotionalContext": alarm.get("emotional_context"),
        "completionSignal": alarm.get("completion_signal"),
        "deliveryPreference": alarm.get("delivery_preference"),
    }
    print(json.dumps(doc, indent=4, default=str, ensure_ascii=False))
    print(_LINE)


def _print_delivery_header(instructions: str) -> None:
    _section("DELIVERY SIMULATION  —  alarm fires now")
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
    return "\n\n".join(parts)


def build_session_config(alarm: dict) -> dict:
    return {
        "mode": "scheduled_conversation",
        "alarmId": alarm["alarm_id"],
        "userId": alarm.get("uid"),
        "label": alarm.get("label"),
        "content": alarm.get("content"),
        "typeHint": alarm.get("type_hint"),
        "priority": alarm.get("priority"),
        "conversationOutline": alarm.get("conversation_outline"),
        "characterReminder": alarm.get("character_reminder"),
        "emotionalContext": alarm.get("emotional_context"),
        "completionSignal": alarm.get("completion_signal"),
        "deliveryPreference": alarm.get("delivery_preference"),
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
# Phase 1 — Intake
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
    global _captured_alarm
    _captured_alarm = None

    tools_schema = [SCHEDULE_CONVERSATION_FUNCTION_DESC]
    messages: list[dict] = []
    fake_conn = SimpleNamespace(device_id=FAKE_DEVICE_ID)

    _section("PHASE 1 — INTAKE")
    print("  Chat to schedule something. Empty line after scheduling → Phase 2.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            if _captured_alarm:
                print("\n[Moving to delivery simulation...]\n")
                return _captured_alarm
            print("[Type a message, or Ctrl-C to quit]\n")
            continue

        messages.append({"role": "user", "content": user_input})

        text, tool_call = llm_turn(
            client, model, messages,
            system=PHASE1_SYSTEM, tools=tools_schema,
        )

        if tool_call and tool_call["name"] == "schedule_conversation":
            # Record assistant turn (with tool call) in history
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": tool_call["id"],
                    "type": "function",
                    "function": {
                        "name": tool_call["name"],
                        "arguments": json.dumps(tool_call["arguments"]),
                    },
                }],
            })

            # Execute the tool — triggers _fake_create_scheduled_conversation
            result = run_sc_tool(fake_conn, **tool_call["arguments"])

            # Feed result back and get confirmation text
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": result.result or "",
            })
            confirm_text, _ = llm_turn(client, model, messages, system=PHASE1_SYSTEM)
            print(f"\nAssistant: {confirm_text}\n")
            messages.append({"role": "assistant", "content": confirm_text})

            if _captured_alarm:
                print("[Press Enter to simulate delivery, or keep chatting]\n")

        else:
            print(f"\nAssistant: {text}\n")
            messages.append({"role": "assistant", "content": text})

    return _captured_alarm


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Delivery simulation
# ─────────────────────────────────────────────────────────────────────────────

def run_phase2(client: openai.OpenAI, model: str, alarm: dict) -> None:
    session_config = build_session_config(alarm)
    instructions   = assemble_instructions(session_config)

    _print_delivery_header(instructions)

    messages: list[dict] = []

    # LLM initiates (server_initiate_chat = True for scheduled_conversation)
    opener, _ = llm_turn(client, model, messages, system=instructions)
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
        reply, _ = llm_turn(client, model, messages, system=instructions)
        print(f"\nCharacter: {reply}\n")
        messages.append({"role": "assistant", "content": reply})


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

    alarm = run_phase1(client, model)
    if alarm is None:
        print("[No alarm scheduled. Exiting.]")
        return

    run_phase2(client, model, alarm)


if __name__ == "__main__":
    main()
