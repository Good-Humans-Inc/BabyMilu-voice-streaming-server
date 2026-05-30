from echoear_server.profile_context import ProfileContext, build_llm_messages, build_profile_system_prompt, normalize_device_id


def test_profile_prompt_includes_supabase_memory_and_identity():
    context = ProfileContext(
        device_id="90:e5:b1:d6:f8:64",
        user_id="user-1",
        user_name="Jackson",
        system_memory_block="## Who they are\nJackson likes concise answers.",
        profile={"identity": {"name": "Jackson", "city": "San Francisco"}},
        loaded=True,
    )

    prompt = build_profile_system_prompt(context)

    assert "90:e5:b1:d6:f8:64" in prompt
    assert "Jackson" in prompt
    assert "San Francisco" in prompt
    assert "Jackson likes concise answers" in prompt


def test_llm_messages_include_recent_history_and_new_turn():
    messages = build_llm_messages(
        "system profile",
        [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}],
        "third",
    )

    assert messages == [
        {"role": "system", "content": "system profile"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]


def test_device_id_normalization():
    assert normalize_device_id(" 90:E5:B1:D6:F8:64 ") == "90:e5:b1:d6:f8:64"
