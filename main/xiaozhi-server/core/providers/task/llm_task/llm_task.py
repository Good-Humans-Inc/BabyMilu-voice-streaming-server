"""
LLM-based Task Detection Provider
Uses LLM to detect and match tasks from conversation against user's assigned tasks
"""

import json
import time
from ..base import TaskProviderBase, logger
from core.utils.util import check_model_key
from core.utils.api_client import get_assigned_tasks_for_user, process_user_action

TAG = __name__

# Task detection prompt template
TASK_DETECTION_PROMPT = """Analyze the following conversation and determine if the content is related to any of the user's assigned tasks.

Conversation:
{conversation}

User's assigned tasks:
{tasks}

Carefully analyze the conversation content to determine if any of the above tasks were discussed, mentioned, or completed.
Return your response as a structured JSON object with a "tasks" array containing any matched tasks."""

TASK_DETECTION_PROMPT_CN = """分析以下对话内容，判断是否与用户的已分配任务相关。

对话内容：
{conversation}

用户的已分配任务：
{tasks}

仔细分析对话内容，确定是否讨论、提及或完成了上述任何任务。
将响应作为结构化 JSON 对象返回，包含一个 "tasks" 数组，其中包含所有匹配的任务。"""

# Manual JSON schema for OpenAI structured outputs
TASK_DETECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The ID of the matched task"
                    },
                    "task_action": {
                        "type": "string",
                        "description": "The action from actionConfig"
                    },
                    "match_reason": {
                        "type": "string",
                        "description": "Brief explanation of why the conversation relates to this task"
                    }
                },
                "required": ["task_id", "task_action", "match_reason"],
                "additionalProperties": False
            }
        }
    },
    "required": ["tasks"],
    "additionalProperties": False
}

class TaskProvider(TaskProviderBase):
    """LLM-based task detection provider"""
    
    def __init__(self, config):
        super().__init__(config)
        self.use_chinese = config.get("use_chinese", False)
        self.max_tokens = config.get("max_tokens", 1000)
        self.temperature = config.get("temperature", 0.5)
        self.stateless = config.get("stateless", True)
        
    async def detect_task(self, msgs, tasks=None, user_id=None, character_name=None):
        """
        Detect tasks from conversation messages
        
        Args:
            msgs: List of conversation messages (Message objects or dicts)
            tasks: user's assigned tasks text (optional, can be provided later)
            user_id: User ID for logging purposes
            
        Returns:
            list: Matched tasks with format:
                  [{"task_id": "...", "task_action": "...", "match_reason": "..."}, ...]
        """
        if not self.llm:
            logger.bind(tag=TAG).warning("LLM未初始化，无法检测任务")
            return []
            
        if not msgs or len(msgs) == 0:
            logger.bind(tag=TAG).debug("对话为空，跳过任务检测")
            return []
        
        # Check LLM API key
        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"使用任务检测模型: {model_info}")
        api_key = getattr(self.llm, "api_key", None)
        task_key_msg = check_model_key("任务检测专用LLM", api_key)
        if task_key_msg:
            logger.bind(tag=TAG).error(task_key_msg)
            
        try:
            # Build conversation text
            conv_text = self._build_conversation_text(msgs)
            
            if tasks is not None:
                tasks_text = tasks
            else:
                # Fetch user's assigned tasks
                assigned_tasks = get_assigned_tasks_for_user(user_id)
                
                # Build tasks text
                tasks_text = self._build_tasks_text_from_list(assigned_tasks, character_name)
            if not tasks_text:
                logger.bind(tag=TAG).debug(f"用户 {user_id} 没有分配的任务，跳过任务检测")
                return []
            # Get appropriate prompt template
            prompt_template = TASK_DETECTION_PROMPT_CN if self.use_chinese else TASK_DETECTION_PROMPT
            
            # Format prompt
            prompt_content = prompt_template.format(
                conversation=conv_text,
                tasks=tasks_text
            )
            
            # Call LLM for task detection with structured outputs
            logger.bind(tag=TAG).debug(f"开始任务检测 - 用户: {user_id}, 任务数: {len(assigned_tasks)}")
            
            # Use Chat Completions API for structured outputs
            # The Responses API doesn't support response_format, so we use completions directly
            messages = [{"role": "user", "content": prompt_content}]
            
            try:
                # Use Chat Completions API with structured outputs
                # completion = self.llm.client.chat.completions.create(
                #     model=self.llm.model_name,
                #     messages=messages,
                #     temperature=self.temperature,
                #     max_tokens=self.max_tokens,
                    # response_format={
                    #     "type": "json_schema",
                    #     "json_schema": {
                    #         "name": "task_detection_response",
                    #         "strict": True,
                    #         "schema": TASK_DETECTION_SCHEMA
                    #     }
                    # }
                # )
                
                # response_text = completion.choices[0].message.content
                response_text = self.llm.response_with_structured_output(messages, {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "task_detection_response",
                            "strict": True,
                            "schema": TASK_DETECTION_SCHEMA
                        }
                    })
                logger.bind(tag=TAG).debug(f"LLM响应: {response_text}")
                
                # Parse JSON response
                response_data = json.loads(response_text)
                matched_tasks = response_data.get("tasks", [])
                
            except Exception as api_error:
                logger.bind(tag=TAG).error(f"调用LLM API失败: {api_error}")
                return []
            
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
    
    def _build_tasks_text_from_list(self, tasks, character_name: str):
        """Build tasks text from task list"""
        tasks_text = ""
        for idx, task in enumerate(tasks, 1):
            task_id = task.get("id", "unknown")
            task_title = task.get("title", "No title")
            action_config = task.get("actionConfig", {})
            action = action_config.get("action", "N/A")
            if character_name:
                task_title = task_title.replace("{character}", character_name)

            tasks_text += f"{idx}. ID: {task_id}\n"
            tasks_text += f"   Title: {task_title}\n"
            tasks_text += f"   Action: {action}\n\n"
        
        return tasks_text
