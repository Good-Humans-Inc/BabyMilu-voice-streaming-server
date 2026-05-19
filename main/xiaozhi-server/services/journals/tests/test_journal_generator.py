import json

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


def test_required_detail_check_allows_content_word_overlap():
    assert generator._contains_required_detail(
        "cookie hopped around the room while serena laughed",
        ["Cookie's playful hops"],
    )


def test_character_writing_profile_extracts_conversation_like_fields():
    profile = generator.build_character_writing_profile(
        {
            "name": "Top Name",
            "unused": "should not leak",
            "profile": {
                "name": "Nested Name",
                "age": "28",
                "pronouns": "he/him",
                "characterToUser": "protective companion",
                "nicknameCharacterCallsUser": "little flame",
                "personality": "Warm, direct, quietly intense.",
                "secretRawField": "should not leak",
            },
        }
    )

    assert profile == {
        "name": "Top Name",
        "age": "28",
        "pronouns": "he/him",
        "relationshipToUser": "protective companion",
        "callsUser": "little flame",
        "bio": "Warm, direct, quietly intense.",
        "journalVoice": (
            "Private, reflective first-person plushie journal voice. Stay in character, "
            "but be quieter and less performative than live conversation."
        ),
    }
    assert "unused" not in profile
    assert "secretRawField" not in profile


def test_writer_payload_is_small_and_avoids_reasoning_context():
    payload = generator.build_writer_payload(
        journal_type="regular",
        character_data={
            "profile": {
                "name": "Milu",
                "bio": "Gentle and playful.",
                "unrelated": "do not include",
            }
        },
        user_data={"name": "Maliyah", "birthday": "do not include"},
        brief={
            "main_event": "Maliyah told Milu about Cookie.",
            "character_observation": "Milu noticed Maliyah smiling about Cookie.",
            "character_inner_response": "Milu felt trusted.",
            "must_include_concrete_details": ["Cookie"],
            "avoidRepeating": ["Do not repeat Cookie without new detail."],
            "forbidden_pov_claims": ["my dog"],
            "thread_reference": False,
        },
        repetition_profile={
            "hardBannedPhrases": ["same old opening"],
            "softAvoidPhrases": ["soft repeated phrase"],
            "recentOpeningPatterns": ["recent opening"],
            "recentEndingPatterns": ["recent ending"],
        },
        style={"sentenceCount": "2 to 4", "endingRule": "End concretely.", "concretenessRule": "Use Cookie."},
        allow_time_specific_opening=False,
    )

    assert set(payload) == {"character", "user", "journalType", "singleSameDayMoment", "brief", "avoid", "style"}
    assert set(payload["avoid"]) == {"hardBannedPhrases", "softAvoidPhrases"}
    assert payload["character"]["name"] == "Milu"
    assert payload["character"]["bio"] == "Gentle and playful."
    assert payload["user"] == {"name": "Maliyah"}
    assert "selectedConversationContext" not in payload
    assert "priorJournalContext" not in payload
    assert "identityContract" not in payload
    assert "characterProfile" not in payload
    assert "coverageWindow" not in payload
    assert "transcript" not in json.dumps(payload)
    assert "do not include" not in json.dumps(payload)


def test_brief_payload_labels_user_and_character_turn_ownership(monkeypatch):
    captured = {}

    def fake_json_response(messages, **kwargs):
        captured["payload"] = messages[1]["content"]
        return {
            "main_event": "Maliyah told Milu about Cookie.",
            "why_this_matters": "It is a concrete user detail.",
            "angle": "remember the pet detail",
            "journal_shape": "remembered_user_fact",
            "must_include_concrete_details": ["Cookie"],
        }

    monkeypatch.setattr(generator, "_json_response", fake_json_response)

    generator.build_journal_brief(
        journal_type="regular",
        character_name="Milu",
        character_data={"name": "Milu"},
        user_data={"name": "Maliyah"},
        coverage_window={},
        sessions=[
            {
                "sessionId": "s1",
                "turns": [
                    {"speaker": "user", "text": "My dog Cookie made me laugh."},
                    {"speaker": "assistant", "text": "I want to remember Cookie."},
                ],
            }
        ],
        trigger_classifications=[],
        prior_journal_context=[],
    )

    payload = json.loads(captured["payload"])
    assert "characterProfile" not in payload
    assert "identityContract" not in payload
    assert payload["character"]["name"] == "Milu"
    assert payload["user"]["name"] == "Maliyah"
    context = payload["selectedConversationContext"]
    turns = context["sessions"][0]["turns"]
    assert context["participants"]["user"]["name"] == "Maliyah"
    assert context["participants"]["character"]["name"] == "Milu"
    assert turns[0]["speakerRole"] == "user"
    assert turns[0]["speakerName"] == "Maliyah"
    assert "refer to Maliyah" in turns[0]["firstPersonOwnership"]
    assert turns[1]["speakerRole"] == "character"
    assert turns[1]["speakerName"] == "Milu"
    assert "refer to Milu" in turns[1]["firstPersonOwnership"]


def test_quality_check_rejects_forbidden_pov_claims():
    result = generator.quality_check_journal_text(
        text="I keep thinking about my daughter and Cookie.",
        voice_check={
            "first_person_character_voice": True,
            "generic_summary": False,
            "starts_with_banned_time_phrase": False,
            "uses_banned_phrase": False,
            "includes_required_concrete_detail": True,
            "character_embodiment_clear": True,
            "does_not_claim_user_experience": True,
            "does_not_speak_as_user": True,
        },
        repetition_profile={"hardBannedPhrases": [], "recentOpeningPatterns": [], "recentEndingPatterns": []},
        required_details=["Cookie"],
        allow_time_specific_opening=False,
        forbidden_pov_claims=["my daughter"],
    )

    assert result["ok"] is False
    assert result["failureReport"]["forbiddenPovClaimsUsed"] == ["my daughter"]
