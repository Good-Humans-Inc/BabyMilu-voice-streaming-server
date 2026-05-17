from services.journals import generator


def test_classify_session_defaults_new_fields(monkeypatch):
    monkeypatch.setattr(generator, "_json_response", lambda *args, **kwargs: {"should_journal": True, "journal_value_type": "strong"})

    result = generator.classify_session(
        turns=[],
        recent_memory_events=[],
        journal_memory_events=[],
        trigger_memory_events=[],
        session_start_time="2026-05-11T12:00:00+00:00",
    )

    assert result["journal_value_type"] == "strong"
    assert result["coverageSummary"] == []
    assert result["concreteAnchors"] == []
    assert result["emotionalThemes"] == []


def test_generation_retries_banned_time_opening(monkeypatch):
    calls = []

    def fake_json_response(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return {
                "text": "Today I learned something about her.",
                "thread_reference": False,
                "topicSummary": ["learning"],
                "voice_check": {
                    "first_person_character_voice": True,
                    "generic_summary": False,
                    "starts_with_banned_time_phrase": True,
                },
            }
        return {
            "text": "I keep thinking about what she trusted me with.",
            "thread_reference": False,
            "topicSummary": ["trust"],
            "coverageSummary": ["User trusted the character with a personal detail."],
            "concreteAnchors": ["trusted me"],
            "emotionalThemes": ["trust"],
            "avoidRepeating": ["Do not repeat this trust moment without new detail."],
            "voice_check": {
                "first_person_character_voice": True,
                "generic_summary": False,
                "starts_with_banned_time_phrase": False,
            },
        }

    monkeypatch.setattr(generator, "_json_response", fake_json_response)

    result = generator.generate_journal_text(
        journal_type="regular",
        character_data={"name": "Milu"},
        user_data={"name": "the user"},
        system_memory_block="",
        sessions=[],
        prior_journal_entries=[],
        thread_reference=False,
    )

    assert result["text"].startswith("I keep thinking")
    assert result["coverageSummary"] == ["User trusted the character with a personal detail."]
    assert len(calls) == 2
