import base64
import asyncio
import importlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.utils import api_client
from core.providers.task.llm_task import llm_task as llm_task_module
from config.settings import get_gcp_credentials_path
from core.utils.task_api_auth import (
    TASK_API_AUDIENCE,
    TASK_API_AUTH_MODE,
    TASK_API_CALLER_SERVICE_ACCOUNT,
    GoogleOidcTokenProvider,
    redact_auth_secrets,
)


@pytest.fixture(autouse=True)
def _restore_api_client_after_global_test_fakes():
    # test_concurrency_isolation installs module-level lambdas during collection.
    importlib.reload(api_client)
    yield


def _jwt(
    exp,
    marker="",
    audience=TASK_API_AUDIENCE,
    email=TASK_API_CALLER_SERVICE_ACCOUNT,
    email_verified=True,
):
    def encode(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")

    claims = {
        "exp": exp,
        "marker": marker,
        "aud": audience,
        "email": email,
        "email_verified": email_verified,
    }
    return f"{encode({'alg': 'none'})}.{encode(claims)}.c2ln"


def test_oidc_provider_caches_concurrent_calls_and_refreshes_near_expiry():
    now = [1_000.0]
    tokens = [_jwt(2_000, "first"), _jwt(3_000, "second")]
    calls = []

    def fetcher(_request, audience):
        calls.append(audience)
        time.sleep(0.01)
        return tokens[min(len(calls) - 1, len(tokens) - 1)]

    provider = GoogleOidcTokenProvider(
        fetcher=fetcher,
        request_factory=object,
        clock=lambda: now[0],
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        first_batch = list(executor.map(lambda _: provider.get_token(), range(8)))
    assert len(set(first_batch)) == 1
    assert calls == [TASK_API_AUDIENCE]

    now[0] = 1_701.0
    assert provider.get_token() != first_batch[0]
    assert calls == [TASK_API_AUDIENCE, TASK_API_AUDIENCE]


def test_oidc_provider_rejects_noncanonical_audience_and_redacts_tokens():
    try:
        GoogleOidcTokenProvider("https://example.invalid")
    except ValueError as exc:
        assert "miffy-dev" in str(exc)
    else:
        raise AssertionError("noncanonical task API audience was accepted")

    provider = GoogleOidcTokenProvider(
        fetcher=lambda _request, _audience: _jwt(
            4_000_000_000,
            email="wrong@example.com",
        ),
        request_factory=object,
    )
    try:
        provider.get_token()
    except RuntimeError as exc:
        assert "caller identity" in str(exc)
    else:
        raise AssertionError("wrong OIDC caller identity was accepted")

    token = _jwt(4_000_000_000, "must-not-leak")
    redacted = redact_auth_secrets(f"Authorization: Bearer {token}")
    assert token not in redacted
    assert "must-not-leak" not in redacted


def test_get_tasks_uses_exact_oidc_audience_and_device_binding(monkeypatch):
    token = _jwt(4_000_000_000, "list")
    captured = {}

    class FakeTokenProvider:
        def get_token(self):
            return token

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _FakeResponse(
            {
                "data": {
                    "tasks": [
                        {"id": "plushie-task", "device": "plushie"},
                        {"id": "app-task", "device": "app"},
                    ]
                }
            }
        )

    monkeypatch.setenv("BABYMILU_TASK_API_BASE_URL", TASK_API_AUDIENCE)
    monkeypatch.setattr(api_client, "_TASK_API_TOKEN_PROVIDER", FakeTokenProvider())
    monkeypatch.setattr(api_client.requests, "get", fake_get)

    tasks = api_client.get_assigned_tasks_for_user("90:E5:B1:D6:F8:58")

    assert [task["id"] for task in tasks] == ["plushie-task"]
    assert captured["url"] == f"{TASK_API_AUDIENCE}/tasks"
    assert captured["params"]["deviceId"] == "90:E5:B1:D6:F8:58"
    assert "uid" not in captured["params"]
    assert captured["headers"]["Authorization"] == f"Bearer {token}"
    assert captured["headers"]["X-BabyMilu-Auth-Mode"] == TASK_API_AUTH_MODE
    assert "X-BabyMilu-Internal-Token" not in captured["headers"]
    assert captured["timeout"] == 4.0


def test_process_action_uses_device_in_top_level_and_action_data(monkeypatch):
    token = _jwt(4_000_000_000, "process")
    captured = {}

    class FakeTokenProvider:
        def get_token(self):
            return token

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _FakeResponse({"success": True})

    monkeypatch.setenv("BABYMILU_TASK_API_BASE_URL", TASK_API_AUDIENCE)
    monkeypatch.setattr(api_client, "_TASK_API_TOKEN_PROVIDER", FakeTokenProvider())
    monkeypatch.setattr(api_client.requests, "post", fake_post)

    ok = api_client.process_user_action(
        "90-e5-b1-d6-f8-58",
        [{"task_id": "task-water", "task_action": "check_in"}],
    )

    assert ok is True
    assert captured["url"] == f"{TASK_API_AUDIENCE}/tasks/process"
    body = captured["json"]
    assert body["deviceId"] == "90-e5-b1-d6-f8-58"
    assert body["actionData"]["deviceId"] == "90-e5-b1-d6-f8-58"
    assert body["actionData"]["taskId"] == "task-water"
    assert "uid" not in body
    assert captured["headers"]["Authorization"] == f"Bearer {token}"
    assert captured["headers"]["X-BabyMilu-Auth-Mode"] == TASK_API_AUTH_MODE


def test_task_api_fails_closed_for_wrong_url_or_missing_device(monkeypatch):
    calls = []
    monkeypatch.setattr(api_client.requests, "get", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("BABYMILU_TASK_API_BASE_URL", "https://example.invalid")

    assert api_client.get_assigned_tasks_for_user("device-1") == []
    assert api_client.get_assigned_tasks_for_user("") == []
    assert calls == []


def test_vm_mode_disables_stale_gcp_json_key_discovery(monkeypatch, tmp_path):
    stale_key = tmp_path / "stale-service-account.json"
    stale_key.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(stale_key))
    monkeypatch.setenv("BABYMILU_ALLOW_GCP_KEY_FILES", "false")

    assert get_gcp_credentials_path() == ""
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_llm_task_provider_uses_active_device_for_fetch_and_process(monkeypatch):
    calls = []

    def fake_get_tasks(device_id):
        calls.append(("get", device_id))
        return [
            {
                "id": "task-water",
                "title": "Drink water",
                "actionConfig": {"action": "check_in"},
            }
        ]

    def fake_process(device_id, tasks):
        calls.append(("process", device_id, tasks))
        return True

    class FakeLlm:
        api_key = "test-key"
        model_name = "fake"

        def response_with_structured_output(self, _messages, _schema):
            return json.dumps(
                {
                    "tasks": [
                        {
                            "task_id": "task-water",
                            "task_action": "check_in",
                            "match_reason": "completed",
                        }
                    ]
                }
            )

    monkeypatch.setattr(llm_task_module, "get_assigned_tasks_for_user", fake_get_tasks)
    monkeypatch.setattr(llm_task_module, "process_user_action", fake_process)
    provider = llm_task_module.TaskProvider({})
    provider.set_llm(FakeLlm())

    result = asyncio.run(
        provider.detect_task(
            [{"role": "user", "content": "I drank water"}],
            user_id="stale-user-hint",
            device_id="device-1",
        )
    )

    assert result[0]["task_id"] == "task-water"
    assert calls[0] == ("get", "device-1")
    assert calls[1][0:2] == ("process", "device-1")


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload
