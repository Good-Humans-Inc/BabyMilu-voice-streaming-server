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


def test_agent_base_prompt_limits_question_loop_and_unclear_asr_guessing():
    prompt_path = pathlib.Path(__file__).resolve().parents[1] / "agent-base-prompt.txt"

    prompt = prompt_path.read_text(encoding="utf-8")

    assert "Prefer observations and personal reactions over questions" in prompt
    assert "Most replies should not end with a question" in prompt
    assert "Do not ask questions in consecutive turns" in prompt
    assert "If a transcript sounds incomplete, correction-like, cut off, or out of context" in prompt
    assert "Do not invent missing facts or force the conversation forward" in prompt
