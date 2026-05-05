from ..base import MemoryProviderBase, logger
import time
from core.utils.util import check_model_key

short_term_memory_prompt_only_content = """
你是一个经验丰富的记忆总结者，擅长将对话内容进行总结摘要，遵循以下规则：
1、总结user的重要信息，以便在未来的对话中提供更个性化的服务
2、不要重复总结，不要遗忘之前记忆，除非原来的记忆超过了1800字内，否则不要遗忘、不要压缩用户的历史记忆
3、用户操控的设备音量、播放音乐、天气、退出、不想对话等和用户本身无关的内容，这些信息不需要加入到总结中
4、聊天内容中的今天的日期时间、今天的天气情况与用户事件无关的数据，这些信息如果当成记忆存储会影响后序对话，这些信息不需要加入到总结中
5、不要把设备操控的成果结果和失败结果加入到总结中，也不要把用户的一些废话加入到总结中
6、不要为了总结而总结，如果用户的聊天没有意义，请返回原来的历史记录也是可以的
7、只需要返回总结摘要，严格控制在1800字内
8、不要包含代码、xml，不需要解释、注释和说明，保存记忆时仅从对话提取信息，不要混入示例内容
"""


TAG = __name__


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory):
        super().__init__(config)
        self.short_memory = ""
        self.user_id = None
        self.load_memory(summary_memory)

    def init_memory(self, role_id, llm, summary_memory=None, **kwargs):
        super().init_memory(role_id, llm, **kwargs)
        self.user_id = kwargs.get("user_id")
        self.load_memory(summary_memory)

    def load_memory(self, summary_memory):
        self.short_memory = summary_memory or ""

    async def save_memory(self, msgs):
        # 打印使用的模型信息
        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"使用记忆保存模型: {model_info}")
        api_key = getattr(self.llm, "api_key", None)
        memory_key_msg = check_model_key("记忆总结专用LLM", api_key)
        if memory_key_msg:
            logger.bind(tag=TAG).error(memory_key_msg)
        if self.llm is None:
            logger.bind(tag=TAG).error("LLM is not set for memory provider")
            return None

        if len(msgs) < 2:
            return None

        msgStr = ""
        for msg in msgs:
            if msg.role == "user":
                msgStr += f"User: {msg.content}\n"
            elif msg.role == "assistant":
                msgStr += f"Assistant: {msg.content}\n"
        if self.short_memory and len(self.short_memory) > 0:
            msgStr += "历史记忆：\n"
            msgStr += self.short_memory

        # 当前时间
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        msgStr += f"当前时间：{time_str}"

        result = self.llm.response_no_stream(
            short_term_memory_prompt_only_content,
            msgStr,
            max_tokens=2000,
            temperature=0.2,
        )
        self.short_memory = result or ""
        logger.bind(tag=TAG).info(
            f"Memory summary updated in runtime cache - Role: {self.role_id}"
        )

        return self.short_memory

    async def query_memory(self, query: str) -> str:
        return self.short_memory
