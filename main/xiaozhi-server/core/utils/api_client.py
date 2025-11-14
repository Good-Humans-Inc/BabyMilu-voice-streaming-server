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
            "extra": True
        }
        response = requests.post(f"https://us-central1-composed-augury-469200-g6.cloudfunctions.net/get-tasks-for-user", json=body)

        if response.status_code != 200:
            logger.bind(tag=TAG).error(f"get assigned tasks error: {response.status_code} {response.text}")
            return []
        return list(filter(lambda x: x["device"] == "plushie", response.json()["tasks"]))
    except Exception as e:
        logger.bind(tag=TAG).error(f"get assigned tasks error: {e}")
        return []

def query_task(user_id: str, character_name: str, user_name: str) -> str:
        """Query tasks for user"""
        try:
            assigned_tasks = get_assigned_tasks_for_user(user_id)
            if not assigned_tasks or len(assigned_tasks) == 0:
                return ""
            
            tasks_text = build_tasks_text_from_list(filter(lambda x: x.get("taskType") != "daily", assigned_tasks), character_name, user_name)
            return tasks_text
        except Exception as e:
            logger.bind(tag=TAG).error(f"query task error: {e}", exc_info=True)
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
            
            if response.status_code != 200:
                logger.bind(tag=TAG).error(f"process user action error: {response.status_code} {response.text}")
                return False
        return True
    except Exception as e:
        logger.bind(tag=TAG).error(f"process user action error: {e}")
        return False