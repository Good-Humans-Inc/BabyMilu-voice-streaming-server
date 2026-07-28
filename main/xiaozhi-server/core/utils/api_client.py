import json
import os
from typing import List, Dict, Any
import requests
from config.logger import setup_logging
from core.utils.task_api_auth import (
    TASK_API_AUDIENCE,
    TASK_API_AUTH_MODE,
    GoogleOidcTokenProvider,
    redact_auth_secrets,
)

TAG = __name__
logger = setup_logging()
_TASK_API_TOKEN_PROVIDER = GoogleOidcTokenProvider()


def get_assigned_tasks_for_user(device_id: str) -> List[Dict[str, Any]]:
    """Fetch owned tasks; the backend resolves the user from ``device_id``."""
    device_id = str(device_id or "").strip()
    if not device_id:
        logger.bind(tag=TAG).warning("get assigned tasks skipped: missing device id")
        return []
    try:
        params = {
            "deviceId": device_id,
            "extra": "true",
            "device": "plushie",
        }
        response = requests.get(
            f"{_task_api_base_url()}/tasks",
            params=params,
            headers=_task_api_headers(),
            timeout=_task_api_timeout_seconds(),
        )

        if response.status_code != 200:
            logger.bind(tag=TAG).error(
                "get assigned tasks error: "
                f"{response.status_code} {redact_auth_secrets(response.text, limit=500)}"
            )
            return []
        response_data = response.json()
        tasks = response_data.get("data", {}).get("tasks", response_data.get("tasks", []))
        return list(filter(lambda x: isinstance(x, dict) and x.get("device") == "plushie", tasks))
    except Exception as e:
        logger.bind(tag=TAG).error(
            f"get assigned tasks error ({type(e).__name__}): {redact_auth_secrets(e)}"
        )
        return []


def query_task(device_id: str, character_name: str, user_name: str) -> str:
        """Query tasks for user"""
        try:
            assigned_tasks = get_assigned_tasks_for_user(device_id)
            if not assigned_tasks or len(assigned_tasks) == 0:
                return ""
            
            tasks_text = build_tasks_text_from_list(filter(lambda x: x.get("taskType") != "daily", assigned_tasks), character_name, user_name)
            return tasks_text
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"query task error ({type(e).__name__}): {redact_auth_secrets(e)}",
                exc_info=True,
            )
            return ""

def build_tasks_text_from_list(tasks, character_name: str, user_name: str):
    """Build tasks text from task list"""
    tasks_text = ""
    for idx, task in enumerate(tasks, 1):
        task_title = task.get("title", "No title")
        action_config = task.get("actionConfig", {})
        action = action_config.get("action", "N/A")
        if character_name:
            task_title = task_title.replace("{character}", character_name)

        tasks_text += f"Task {idx}: {task_title}\n"
        tasks_text += f"Action: {action}\n\n"
        prompts = task.get('prompts', "").replace("{user}", user_name)
        # TODO Need to improve
        if prompts:
            tasks_text += f"Conversation guide for this task: {prompts}\n\n"
    return tasks_text 

def process_user_action(device_id: str, tasks: List[Dict[str, Any]]) -> bool:
    """Process matched actions for the owner of ``device_id``."""
    device_id = str(device_id or "").strip()
    if not device_id:
        logger.bind(tag=TAG).warning("process user action skipped: missing device id")
        return False
    try:
        # TODO 这里需要优化，一次处理多个任务
        for task in tasks:
            action = task.get("task_action", "")
            body = {
                "deviceId": device_id,
                "actionType": action,
                "actionData": {
                    "deviceId": device_id,
                    "source": "legacy_voice_task_provider",
                    **(
                        {"taskId": str(task.get("task_id")).strip()}
                        if str(task.get("task_id") or "").strip()
                        else {}
                    ),
                },
            }
            response = requests.post(
                f"{_task_api_base_url()}/tasks/process",
                json=body,
                headers=_task_api_headers(),
                timeout=_task_api_timeout_seconds(),
            )
            
            if response.status_code != 200:
                logger.bind(tag=TAG).error(
                    "process user action error: "
                    f"{response.status_code} {redact_auth_secrets(response.text, limit=500)}"
                )
                return False
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(
            f"process user action error ({type(e).__name__}): {redact_auth_secrets(e)}"
        )
        return False


def _task_api_base_url() -> str:
    base_url = os.getenv("BABYMILU_TASK_API_BASE_URL", TASK_API_AUDIENCE).strip().rstrip("/")
    if base_url != TASK_API_AUDIENCE:
        raise RuntimeError("task API base URL must match the exact miffy-dev OIDC audience")
    return base_url


def _task_api_timeout_seconds() -> float:
    return max(0.1, float(os.getenv("BABYMILU_TASK_API_TIMEOUT_SECONDS", "4.0")))


def _task_api_headers() -> Dict[str, str]:
    token = _TASK_API_TOKEN_PROVIDER.get_token()
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-BabyMilu-Auth-Mode": TASK_API_AUTH_MODE,
    }
