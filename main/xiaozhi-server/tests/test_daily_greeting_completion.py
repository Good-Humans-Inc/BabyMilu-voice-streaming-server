import asyncio
from unittest.mock import patch

from core.providers.task.llm_task.llm_task import TaskProvider


GREET_TASK = {
    "id": "daily-greet",
    "title": "Greet {character}",
    "actionConfig": {"action": "greet"},
}


def test_any_non_empty_user_utterance_completes_greeting_without_llm():
    provider = TaskProvider({})
    conversation = [
        {"role": "user", "content": "How are you feeling today?"},
        {"role": "assistant", "content": "I'm happy to chat with you."},
    ]

    with patch(
        "core.providers.task.llm_task.llm_task.process_user_action"
    ) as process_action:
        matches = asyncio.run(
            provider.detect_task(
                conversation,
                tasks=[GREET_TASK],
                user_id="user-1",
                device_id="device-1",
            )
        )

    assert matches == [
        {
            "task_id": "daily-greet",
            "task_action": "greet",
            "match_reason": "User participated in a conversation.",
        }
    ]
    process_action.assert_called_once_with("device-1", matches)


def test_empty_user_content_does_not_complete_greeting():
    provider = TaskProvider({})

    with patch(
        "core.providers.task.llm_task.llm_task.process_user_action"
    ) as process_action:
        matches = asyncio.run(
            provider.detect_task(
                [{"role": "user", "content": "   "}],
                tasks=[GREET_TASK],
                user_id="user-1",
                device_id="device-1",
            )
        )

    assert matches == []
    process_action.assert_not_called()


def test_non_greeting_task_remains_llm_classified():
    provider = TaskProvider({})
    exercise_task = {
        "id": "daily-exercise",
        "title": "Exercise",
        "actionConfig": {"action": "exercise"},
    }

    with patch(
        "core.providers.task.llm_task.llm_task.process_user_action"
    ) as process_action:
        matches = asyncio.run(
            provider.detect_task(
                [{"role": "user", "content": "How are you?"}],
                tasks=[exercise_task],
                user_id="user-1",
                device_id="device-1",
            )
        )

    assert matches == []
    process_action.assert_not_called()
