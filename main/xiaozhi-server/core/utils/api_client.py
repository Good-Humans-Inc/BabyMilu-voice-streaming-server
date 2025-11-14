import json
from typing import List, Dict, Any
import requests
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# From api https://us-central1-composed-augury-469200-g6.cloudfunctions.net/get-tasks-for-user
def get_assigned_tasks_for_user(user_id: str) -> List[Dict[str, Any]]:
    try:
        body = {
            "uid": user_id,
            "status": ["action"],
            "device": "plushie",
            "extra": True
        }
        response = requests.post(f"https://us-central1-composed-augury-469200-g6.cloudfunctions.net/get-tasks-for-user", json=body)
        print("get assigned tasks response:", response)

        if response.status_code != 200:
            logger.bind(tag=TAG).error(f"get assigned tasks error: {response.status_code} {response.text}")
            return []
        return response.json()["data"]["tasks"]
    except Exception as e:
        logger.bind(tag=TAG).error(f"get assigned tasks error: {e}")
        return []

def process_user_action(user_id: str, tasks: List[Dict[str, Any]]) -> bool:
    try:
        # TODO 这里需要优化，一次处理多个任务
        for task in tasks:
            action = task.get("task_action", "")
            body = {
                "uid": user_id,
                "actionType": action,
                "actionData": {}
            }
            response = requests.post(f"https://us-central1-composed-augury-469200-g6.cloudfunctions.net/process-user-action", json=body)
            print("process user action response:", response)
            if response.status_code != 200:
                logger.bind(tag=TAG).error(f"process user action error: {response.status_code} {response.text}")
                return False
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(f"process user action error: {e}")
        return False