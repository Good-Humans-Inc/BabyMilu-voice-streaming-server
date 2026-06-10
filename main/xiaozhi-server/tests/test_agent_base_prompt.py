from __future__ import annotations

import pathlib


def test_agent_base_prompt_encourages_proactive_checkin_reminders():
    prompt_path = pathlib.Path(__file__).resolve().parents[1] / "agent-base-prompt.txt"

    prompt = prompt_path.read_text(encoding="utf-8")

    assert "Proactive check-in reminders" in prompt
    assert "actively offer to set a check-in reminder" in prompt
    assert "Do not wait for the exact words" in prompt
    assert "call `schedule_conversation` immediately" in prompt
    assert "never create a reminder silently" in prompt
