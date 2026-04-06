import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone

import requests
from google.cloud import firestore

from config.config_loader import load_config
from core.utils import llm as llm_utils
from core.utils import task as task_utils


GET_TASKS_URL = (
    "https://us-central1-composed-augury-469200-g6.cloudfunctions.net/get-tasks-for-user"
)


def _make_test_uid() -> str:
    return f"+1999555{int(time.time()) % 1000000:06d}"


def _ensure_test_user(db: firestore.Client, uid: str) -> None:
    now = datetime.now(timezone.utc)
    db.collection("users").document(uid).set(
        {
            "name": "Codex Smoke Test",
            "phoneNumber": uid,
            "timezone": "America/Los_Angeles",
            "createdAt": now,
            "updatedAt": now,
            "deviceIds": [],
            "characterIds": [],
        },
        merge=True,
    )


def _fetch_tasks(uid: str, status=None, extra=True) -> dict:
    body = {"uid": uid, "extra": extra}
    if status is not None:
        body["status"] = status
    response = requests.post(GET_TASKS_URL, json=body, timeout=60)
    response.raise_for_status()
    return response.json()


def _pick_plushie_daily_task(tasks: list[dict]) -> dict:
    for task in tasks:
        if task.get("taskType") == "daily" and task.get("device") == "plushie":
            return task
    raise RuntimeError("No active plushie daily task found for smoke test user")


def _task_snapshot(db: firestore.Client, uid: str, task_id: str) -> dict:
    user_task = (
        db.collection("users").document(uid).collection("userTasks").document(task_id).get()
    )
    summary_id = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_summary = (
        db.collection("users")
        .document(uid)
        .collection("dailySummaries")
        .document(summary_id)
        .get()
    )
    all_logs = (
        db.collection("users").document(uid).collection("taskLogs").stream()
    )
    matching_logs = []
    for doc in all_logs:
        data = doc.to_dict() or {}
        if data.get("taskId") == task_id:
            matching_logs.append(data)
    recent_logs = sorted(
        matching_logs,
        key=lambda item: item.get("timestamp", 0),
        reverse=True,
    )[:5]
    return {
        "userTask": user_task.to_dict() if user_task.exists else None,
        "dailySummary": daily_summary.to_dict() if daily_summary.exists else None,
        "recentLogs": recent_logs,
    }


def _build_provider():
    cfg = load_config()
    llm_name = cfg["selected_module"]["LLM"]
    llm_cfg = cfg["LLM"][llm_name]
    llm_type = llm_cfg.get("type", llm_name)
    llm = llm_utils.create_instance(llm_type, llm_cfg)

    task_name = cfg["selected_module"]["Task"]
    task_cfg = cfg["Task"][task_name]
    task_type = task_cfg.get("type", task_name)
    provider = task_utils.create_instance(task_type, task_cfg)
    provider.init_task(role_id="task-smoke-test", llm=llm)
    return provider


def _negative_conversation() -> list[dict]:
    return [
        {
            "role": "user",
            "content": "Can you remind me what the weather is like later today?",
        },
        {
            "role": "assistant",
            "content": "Sure, I can help with that. Do you want the afternoon forecast?",
        },
    ]


def _positive_conversation(action: str) -> list[dict]:
    if action == "greet":
        return [
            {
                "role": "user",
                "content": "Hi Milu, good morning. I just wanted to say hello to you today.",
            },
            {
                "role": "assistant",
                "content": "Good morning! Hi there, it is really nice to hear from you.",
            },
        ]
    return [
        {
            "role": "user",
            "content": f"I am doing the task action right now: {action}.",
        },
        {
            "role": "assistant",
            "content": f"Great, I can tell you just completed the action {action}.",
        },
    ]


def _delete_user_data(db: firestore.Client, uid: str) -> None:
    user_ref = db.collection("users").document(uid)
    for collection_name in ("taskLogs", "userTasks", "dailySummaries"):
        docs = list(user_ref.collection(collection_name).stream())
        for doc in docs:
            doc.reference.delete()
    user_ref.delete()


async def _run_smoke(uid: str, keep_user: bool) -> dict:
    db = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    _ensure_test_user(db, uid)

    assignment = _fetch_tasks(uid, status=["action"], extra=True)
    active_task = _pick_plushie_daily_task(assignment["tasks"])
    task_id = active_task["id"]
    task_action = active_task.get("actionConfig", {}).get("action", "")

    provider = _build_provider()

    before = _task_snapshot(db, uid, task_id)
    negative_matches = await provider.detect_task(
        _negative_conversation(), user_id=uid
    )
    after_negative = _task_snapshot(db, uid, task_id)
    positive_matches = await provider.detect_task(
        _positive_conversation(task_action), user_id=uid
    )
    after_positive = _task_snapshot(db, uid, task_id)

    result = {
        "uid": uid,
        "taskId": task_id,
        "taskAction": task_action,
        "before": before,
        "negativeMatches": negative_matches,
        "afterNegative": after_negative,
        "positiveMatches": positive_matches,
        "afterPositive": after_positive,
    }

    user_task_before = (before.get("userTask") or {}).get("status")
    user_task_after_negative = (after_negative.get("userTask") or {}).get("status")
    user_task_after_positive = (after_positive.get("userTask") or {}).get("status")

    if negative_matches:
        raise RuntimeError(f"Negative control matched tasks unexpectedly: {negative_matches}")
    if user_task_before != user_task_after_negative:
        raise RuntimeError("Negative control changed Firestore task state unexpectedly")
    if not positive_matches:
        raise RuntimeError("Positive conversation did not match any tasks")
    if user_task_after_positive != "completed":
        raise RuntimeError(
            f"Expected completed status after positive test, got {user_task_after_positive!r}"
        )

    if not keep_user:
        _delete_user_data(db, uid)
        result["cleanup"] = "deleted"
    else:
        result["cleanup"] = "kept"

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run daily-task smoke test")
    parser.add_argument("--uid", default=_make_test_uid())
    parser.add_argument("--keep-user", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(_run_smoke(args.uid, args.keep_user))
    print(json.dumps(result, default=str, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
