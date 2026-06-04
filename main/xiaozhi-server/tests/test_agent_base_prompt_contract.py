from pathlib import Path


def test_agent_prompt_requires_fish_emotion_tag_on_spoken_replies():
    prompt = Path("agent-base-prompt.txt").read_text(encoding="utf-8")

    assert "every normal spoken reply must include at least one" in prompt
    assert "immediately after the required leading emoji" in prompt
    assert "[friendly]" in prompt
    assert "Use zero, one, or two Fish tags per sentence" not in prompt
    assert "Do not tag every sentence" not in prompt
