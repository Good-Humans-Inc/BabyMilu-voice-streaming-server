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
                "main_event": "The user trusted Milu with a Cookie story.",
                "why_this_matters": "It is specific and personal.",
                "angle": "remember the small detail",
                "journal_shape": "remembered_user_fact",
                "must_include_concrete_details": ["Cookie"],
                "supporting_details": ["trusted me"],
                "do_not_include": [],
                "coverageSummary": ["User shared a Cookie story."],
                "concreteAnchors": ["Cookie"],
                "emotionalThemes": ["trust"],
                "avoidRepeating": ["Do not repeat the Cookie story without a new detail."],
                "thread_reference": False,
                "thread_reference_reason": "",
                "thread_reference_targets": [],
            }
        if len(calls) == 2:
            return {
                "text": "Today I learned something about Cookie.",
                "thread_reference": False,
                "topicSummary": ["learning"],
                "voice_check": {
                    "first_person_character_voice": True,
                    "generic_summary": False,
                    "starts_with_banned_time_phrase": True,
                    "uses_banned_phrase": False,
                    "includes_required_concrete_detail": True,
                },
            }
        return {
            "text": "I keep thinking about Cookie and what she trusted me with.",
            "thread_reference": False,
            "topicSummary": ["trust"],
            "coverageSummary": ["User trusted the character with a Cookie story."],
            "concreteAnchors": ["Cookie"],
            "emotionalThemes": ["trust"],
            "avoidRepeating": ["Do not repeat this Cookie story without new detail."],
            "journal_shape": "remembered_user_fact",
            "main_event": "The user trusted Milu with a Cookie story.",
            "voice_check": {
                "first_person_character_voice": True,
                "generic_summary": False,
                "starts_with_banned_time_phrase": False,
                "uses_banned_phrase": False,
                "includes_required_concrete_detail": True,
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
    assert result["coverageSummary"] == ["User trusted the character with a Cookie story."]
    assert result["retryAttempted"] is True
    assert len(calls) == 3


def test_repetition_profile_hard_bans_repeated_local_phrases():
    profile = generator.build_repetition_profile(
        prior_journal_entries=[
            {"text": "Maliyah's laughter echoes in my thoughts. Cookie made her smile."},
            {"text": "Maliyah's laughter echoes in my thoughts. Cherry Baby came up again."},
        ],
        protected_phrases=[],
        user_data={"name": "Maliyah"},
        character_data={"name": "Milu"},
    )

    assert any(phrase.startswith("maliyah's laughter echoes") for phrase in profile["hardBannedPhrases"])


def test_repetition_profile_protects_required_concrete_details():
    profile = generator.build_repetition_profile(
        prior_journal_entries=[
            {"text": "Cherry Baby perfume stayed in my mind."},
            {"text": "Cherry Baby perfume stayed in my mind."},
        ],
        protected_phrases=["Cherry Baby perfume"],
        user_data={"name": "Maliyah"},
        character_data={"name": "Milu"},
    )

    assert "cherry baby perfume" not in profile["hardBannedPhrases"]


def test_phrase_filter_rejects_common_stopword_phrases():
    candidates = generator.extract_phrase_candidates([
        "The way she was there. The way she was there.",
        "The way she was there. The way she was there.",
    ])
    hard, soft = generator.filter_phrase_candidates(candidates, set(), set())

    assert "the way she was there" not in hard
    assert "the way she was there" not in soft
