"""
No Task Detection Provider
Use this module when task detection is not needed
"""

from ..base import TaskProviderBase, logger

TAG = __name__


class TaskProvider(TaskProviderBase):
    """No-op task provider that doesn't perform any task detection"""
    
    def __init__(self, config):
        super().__init__(config)
        logger.bind(tag=TAG).debug("初始化无任务检测模式")

    async def detect_task(self, msgs, tasks=None, user_id=None):
        """
        No-op task detection
        
        Returns:
            list: Always returns empty list
        """
        logger.bind(tag=TAG).debug("notask mode: 跳过任务检测")
        return []

