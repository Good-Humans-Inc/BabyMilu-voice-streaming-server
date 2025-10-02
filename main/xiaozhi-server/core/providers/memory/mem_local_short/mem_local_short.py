from ..base import MemoryProviderBase, logger
import time
import json
import os
import yaml
from config.config_loader import get_project_dir
from config.manage_api_client import save_mem_local_short
from core.utils.util import check_model_key


short_term_memory_prompt_en = """
# Dynamic Memory Agent

## Core Mission

Build a growing memory system that keeps key information while tracking how it changes over time.
Summarize important details from conversations so future responses can be more personalized.

## Memory Rules

### 1. Memory Evaluation (every update)

| Dimension   | Criteria                            | Weight |
| ----------- | ----------------------------------- | ------ |
| Recency     | How fresh the information is        | 40%    |
| Emotion     | 💖 mark / frequency of mention      | 35%    |
| Connections | How many links to other stored info | 25%    |

### 2. Update Mechanism

**Example: Name Change**
Original: `"former_names": ["John"], "current_name": "Jonathan"`
Trigger: phrases like “My name is X” or “Call me Y”
Steps:

1. Move old name to `"former_names"`
2. Record timeline: `"2024-02-15 14:32: Switched to Jonathan"`
3. Add note: `"Identity shift from John to Jonathan"`

### 3. Space Optimization

* **Compression:** Use shorthand instead of long text

  * ✅ `"Jonathan [NY/SWE/🐱]"`
  * ❌ `"Jonathan, software engineer in New York, owns a cat"`
* **Pruning:** If total text ≥ 900 characters

  1. Delete entries with score < 60 not mentioned in last 3 turns
  2. Merge similar items (keep most recent timestamp)

## Memory Structure

Always output valid JSON.
Only extract from the actual conversation (no examples or comments).

```json
{
  "archive": {
    "identity": {
      "current_name": "",
      "tags": []
    },
    "memories": [
      {
        "event": "Joined a new company",
        "timestamp": "2024-03-20",
        "emotion_score": 0.9,
        "related": ["afternoon tea"],
        "shelf_life": 30
      }
    ]
  },
  "network": {
    "frequent_topics": {"work": 12},
    "hidden_links": [""]
  },
  "pending": {
    "urgent": ["Tasks that need immediate action"],
    "care": ["Support that could be offered"]
  },
  "highlights": [
    "Most moving moments, strong emotions, exact user quotes"
  ]
}
```
"""

short_term_memory_prompt = """
# 时空记忆编织者

## 核心使命
构建可生长的动态记忆网络，在有限空间内保留关键信息的同时，智能维护信息演变轨迹
根据对话记录，总结user的重要信息，以便在未来的对话中提供更个性化的服务

## 记忆法则
### 1. 三维度记忆评估（每次更新必执行）
| 维度       | 评估标准                  | 权重分 |
|------------|---------------------------|--------|
| 时效性     | 信息新鲜度（按对话轮次） | 40%    |
| 情感强度   | 含💖标记/重复提及次数     | 35%    |
| 关联密度   | 与其他信息的连接数量      | 25%    |

### 2. 动态更新机制
**名字变更处理示例：**
原始记忆："曾用名": ["张三"], "现用名": "张三丰"
触发条件：当检测到「我叫X」「称呼我Y」等命名信号时
操作流程：
1. 将旧名移入"曾用名"列表
2. 记录命名时间轴："2024-02-15 14:32:启用张三丰"
3. 在记忆立方追加：「从张三到张三丰的身份蜕变」

### 3. 空间优化策略
- **信息压缩术**：用符号体系提升密度
  - ✅"张三丰[北/软工/🐱]"
  - ❌"北京软件工程师，养猫"
- **淘汰预警**：当总字数≥900时触发
  1. 删除权重分<60且3轮未提及的信息
  2. 合并相似条目（保留时间戳最近的）

## 记忆结构
输出格式必须为可解析的json字符串，不需要解释、注释和说明，保存记忆时仅从对话提取信息，不要混入示例内容
```json
{
  "时空档案": {
    "身份图谱": {
      "现用名": "",
      "特征标记": [] 
    },
    "记忆立方": [
      {
        "事件": "入职新公司",
        "时间戳": "2024-03-20",
        "情感值": 0.9,
        "关联项": ["下午茶"],
        "保鲜期": 30 
      }
    ]
  },
  "关系网络": {
    "高频话题": {"职场": 12},
    "暗线联系": [""]
  },
  "待响应": {
    "紧急事项": ["需立即处理的任务"], 
    "潜在关怀": ["可主动提供的帮助"]
  },
  "高光语录": [
    "最打动人心的瞬间，强烈的情感表达，user的原话"
  ]
}
```
"""

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


def extract_json_data(json_code):
    start = json_code.find("```json")
    # 从start开始找到下一个```结束
    end = json_code.find("```", start + 1)
    # print("start:", start, "end:", end)
    if start == -1 or end == -1:
        try:
            jsonData = json.loads(json_code)
            return json_code
        except Exception as e:
            print("Error:", e)
        return ""
    jsonData = json_code[start + 7 : end]
    return jsonData


TAG = __name__


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory):
        super().__init__(config)
        self.short_memory = ""
        self.save_to_file = True
        self.memory_path = get_project_dir() + "data/.memory.yaml"
        self.load_memory(summary_memory)

    def init_memory(
        self, role_id, llm, summary_memory=None, save_to_file=True, **kwargs
    ):
        super().init_memory(role_id, llm, **kwargs)
        self.save_to_file = save_to_file
        self.load_memory(summary_memory)

    def load_memory(self, summary_memory):
        # api获取到总结记忆后直接返回
        if summary_memory or not self.save_to_file:
            self.short_memory = summary_memory
            return

        all_memory = {}
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                all_memory = yaml.safe_load(f) or {}
        if self.role_id in all_memory:
            self.short_memory = all_memory[self.role_id]

    def save_memory_to_file(self):
        all_memory = {}
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                all_memory = yaml.safe_load(f) or {}
        all_memory[self.role_id] = self.short_memory
        with open(self.memory_path, "w", encoding="utf-8") as f:
            yaml.dump(all_memory, f, allow_unicode=True)

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
            msgStr += "Previous memory：\n"
            msgStr += self.short_memory

        # 当前时间
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        msgStr += f"Current time:{time_str}"

        if self.save_to_file:
            logger.bind(tag=TAG).info("short_term_memory_prompt_en")
            result = self.llm.response_no_stream(
                short_term_memory_prompt_en,
                msgStr,
                max_tokens=2000,
                temperature=0.2,
            )
            logger.bind(tag=TAG).debug(f"Raw LLM response: {result[:200]}...")
            json_str = extract_json_data(result)
            logger.bind(tag=TAG).debug(f"Extracted JSON: {json_str[:200]}...")
            try:
                json.loads(json_str)  # 检查json格式是否正确
                self.short_memory = json_str
                self.save_memory_to_file()
            except Exception as e:
                logger.bind(tag=TAG).error(f"JSON parsing error: {e}")
                print("Error:", e)
        else:
            logger.bind(tag=TAG).info("short_term_memory_prompt_only_content")
            result = self.llm.response_no_stream(
                short_term_memory_prompt_only_content,
                msgStr,
                max_tokens=2000,
                temperature=0.2,
            )
            save_mem_local_short(self.role_id, result)
        logger.bind(tag=TAG).info(f"Save memory successful - Role: {self.role_id}")

        return self.short_memory

    async def query_memory(self, query: str) -> str:
        return self.short_memory
