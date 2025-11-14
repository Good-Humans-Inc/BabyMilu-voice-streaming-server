"""
LLM-based Task Detection Provider
Uses LLM to detect and match tasks from conversation against user's assigned tasks
"""

import json
import time
from ..base import TaskProviderBase, logger
from core.utils.util import check_model_key
from core.utils.api_client import get_assigned_tasks_for_user, process_user_action
from pydantic import BaseModel

TAG = __name__

# Task detection prompt template
TASK_DETECTION_PROMPT = """Analyze the following conversation and determine if the content is related to any of the user's assigned tasks.

Conversation:
{conversation}

User's assigned tasks:
{tasks}

Carefully analyze the conversation content to determine if any of the above tasks were discussed, mentioned, or completed.
If there are matching tasks, return a JSON array with the following format:
[
  {{"task_id": "task ID", "task_action": "action from actionConfig", "match_reason": "brief explanation of why the conversation relates to this task"}}
]

If no tasks match, return an empty array: []

Return ONLY the JSON array, no other explanation."""

TASK_DETECTION_PROMPT_CN = """分析以下对话内容，判断是否与用户的已分配任务相关。

对话内容：
{conversation}

用户的已分配任务：
{tasks}

仔细分析对话内容，确定是否讨论、提及或完成了上述任何任务。
如果有匹配的任务，返回以下格式的 JSON 数组：
[
  {{"task_id": "任务ID", "task_action": "actionConfig中的action", "match_reason": "简要说明对话与此任务相关的原因"}}
]

如果没有匹配的任务，返回空数组：[]

只返回 JSON 数组，不需要其他解释。"""

class TaskLLMResponse(BaseModel):
    task_id: str
    task_action: str
    match_reason: str

def extract_json_array(text: str):
    """Extract JSON array from text response"""
    try:
        # Try direct parsing first
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON array in text
        if "[" in text and "]" in text:
            start_idx = text.find("[")
            end_idx = text.rfind("]") + 1
            json_str = text[start_idx:end_idx]
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass
    
    logger.bind(tag=TAG).warning(f"无法解析JSON响应: {text}")
    return []


class TaskProvider(TaskProviderBase):
    """LLM-based task detection provider"""
    
    def __init__(self, config):
        super().__init__(config)
        self.use_chinese = config.get("use_chinese", False)
        self.max_tokens = config.get("max_tokens", 1000)
        self.temperature = config.get("temperature", 0.5)
        self.stateless = config.get("stateless", True)
        
    async def detect_task(self, msgs, tasks=None, user_id=None):
        """
        Detect tasks from conversation messages
        
        Args:
            msgs: List of conversation messages (Message objects or dicts)
            tasks: List of user's assigned tasks (optional, can be provided later)
            user_id: User ID for logging purposes
            
        Returns:
            list: Matched tasks with format:
                  [{"task_id": "...", "task_action": "...", "match_reason": "..."}, ...]
        """
        if not self.llm:
            logger.bind(tag=TAG).warning("LLM未初始化，无法检测任务")
            return []
            
        if not tasks or len(tasks) == 0:
            logger.bind(tag=TAG).debug(f"用户 {user_id or 'unknown'} 没有分配的任务")
            return []
            
        if not msgs or len(msgs) == 0:
            logger.bind(tag=TAG).debug("对话为空，跳过任务检测")
            return []
        
        # Check LLM API key
        api_key = getattr(self.llm, "api_key", None)
        task_key_msg = check_model_key("任务检测专用LLM", api_key)
        if task_key_msg:
            logger.bind(tag=TAG).error(task_key_msg)
            
        try:
            # Build conversation text
            conv_text = self._build_conversation_text(msgs)
            
            # Build tasks text
            tasks_text = self._build_tasks_text(tasks)
            
            # Get appropriate prompt template
            prompt_template = TASK_DETECTION_PROMPT_CN if self.use_chinese else TASK_DETECTION_PROMPT
            
            # Format prompt
            prompt_content = prompt_template.format(
                conversation=conv_text,
                tasks=tasks_text
            )
            
            # Create prompt for LLM
            prompt = [{"role": "user", "content": prompt_content}]
            
            # Call LLM for task detection
            logger.bind(tag=TAG).debug(f"开始任务检测 - 用户: {user_id}, 任务数: {len(tasks)}")
            
            response_parts = []
            llm_responses = self.llm.response_no_stream(
                f"task_detect_{user_id or 'unknown'}_{int(time.time())}",
                prompt,
                stateless=self.stateless,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                text_format=TaskLLMResponse
            )
            # FIXME delete
            print("llm_responses:", llm_responses)
            for response in llm_responses:
                if response:
                    response_parts.append(response)
            
            response_text = "".join(response_parts).strip()
            
            # Parse JSON response
            matched_tasks = extract_json_array(response_text)
            
            if matched_tasks and len(matched_tasks) > 0:
                logger.bind(tag=TAG).info(
                    f"任务检测完成 - 用户: {user_id}, 匹配任务数: {len(matched_tasks)}"
                )
                process_user_action(user_id, matched_tasks)
                return matched_tasks
            else:
                logger.bind(tag=TAG).debug(f"任务检测完成 - 用户: {user_id}, 无匹配任务")
                return []
                
        except Exception as e:
            logger.bind(tag=TAG).error(f"任务检测失败: {e}", exc_info=True)
            return []
    
    def _build_conversation_text(self, msgs):
        """Build conversation text from messages"""
        conv_text = ""
        
        for msg in msgs:
            # Handle both Message objects and dicts
            if hasattr(msg, 'role') and hasattr(msg, 'content'):
                role = msg.role
                content = msg.content
            elif isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                continue
            
            # Skip system messages
            if role == "system":
                continue
                
            role_label = "User" if role == "user" else "Assistant"
            conv_text += f"{role_label}: {content}\n"
        
        return conv_text
    
    def _build_tasks_text(self, user_id):
        """Build tasks text from task list"""
        tasks_text = ""
        tasks = get_assigned_tasks_for_user(user_id)
        if not tasks or len(tasks) == 0:
            self.logger.bind(tag=TAG).debug(f"用户 {user_id} 没有分配的任务")
            return ""
        for idx, task in enumerate(tasks, 1):
            task_id = task.get("id", "unknown")
            task_title = task.get("title", "No title")
            action_config = task.get("actionConfig", {})
            action = action_config.get("action", "N/A")
            
            tasks_text += f"{idx}. ID: {task_id}\n"
            tasks_text += f"   Title: {task_title}\n"
            tasks_text += f"   Action: {action}\n\n"
        
        return tasks_text

