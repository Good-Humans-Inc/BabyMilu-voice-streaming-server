from abc import ABC, abstractmethod
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class TaskProviderBase(ABC):
    def __init__(self, config):
        self.config = config
        self.role_id = None
        self.llm = None

    def set_llm(self, llm):
        self.llm = llm

    @abstractmethod
    async def detect_task(self, msgs, tasks=None, user_id=None, character_name=None):
        """
        Detect tasks from conversation messages
        
        Args:
            msgs: List of conversation messages
            tasks: List of user's assigned tasks (optional)
            user_id: User ID for logging purposes
            character_name: Character name for logging purposes
        Returns:
            list: Matched tasks with format [{"task_id": "...", "task_action": "...", "match_reason": "..."}, ...]
        """
        print("this is base func", msgs, tasks, user_id, character_name)
        return []
        
    def init_task(self, role_id, llm, **kwargs):
        """
        Initialize task provider with role and LLM
        
        Args:
            role_id: User/role identifier
            llm: LLM instance for task detection
            **kwargs: Additional parameters
        """
        self.role_id = role_id
        self.llm = llm
