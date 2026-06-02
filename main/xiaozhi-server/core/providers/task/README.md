# Task Provider

Task Provider 用于从对话中检测和匹配用户的已分配任务。

## 目录结构

```
task/
├── base.py              # 基础抽象类
├── llm_task/           # LLM任务检测提供者
│   └── llm_task.py
├── notask/             # 无任务检测提供者
│   └── notask.py
└── openai/             # OpenAI任务检测提供者（待完善）
    └── openai.py
```

## 提供者类型

### 1. llm_task - LLM任务检测

使用大语言模型分析对话内容，匹配用户的已分配任务。

**配置示例：**
```yaml
task:
  type: llm_task
  use_chinese: true      # 使用中文提示词
  max_tokens: 1000       # 最大输出tokens
  temperature: 0.2       # 温度参数
  stateless: true        # 无状态调用
```

**特性：**
- 智能分析对话内容
- 支持中英文提示词
- 返回匹配的任务及匹配原因

### 2. notask - 无任务检测

不进行任何任务检测，直接返回空列表。

**配置示例：**
```yaml
task:
  type: notask
```

## 使用方法

### 1. 创建任务提供者实例

```python
from core.utils import task

# 创建实例
task_config = {
    "type": "llm_task",
    "use_chinese": True,
    "max_tokens": 1000,
    "temperature": 0.2
}

task_provider = task.create_instance("llm_task", task_config)
```

### 2. 初始化任务提供者

```python
# 初始化（设置role_id和LLM实例）
task_provider.init_task(
    role_id="user_phone_or_id",
    llm=llm_instance
)
```

### 3. 检测任务

```python
from core.utils.firestore_client import get_assigned_tasks_for_user

# 获取用户的已分配任务
tasks = get_assigned_tasks_for_user(user_id)

# 从对话中检测任务
matched_tasks = await task_provider.detect_task(
    msgs=conversation_messages,  # 对话消息列表
    tasks=tasks,                  # 用户的已分配任务
    user_id=user_id               # 用户ID
)

# 处理匹配结果
if matched_tasks:
    for task in matched_tasks:
        print(f"Task ID: {task['task_id']}")
        print(f"Action: {task['task_action']}")
        print(f"Reason: {task['match_reason']}")
```

## 在 ConnectionHandler 中使用

在 `connection.py` 中集成任务检测：

```python
# 初始化时创建task provider
from core.utils import task as task_utils

task_config = self.config.get("task", {})
self.task_provider = None

if task_config and task_config.get("type"):
    try:
        self.task_provider = task_utils.create_instance(
            task_config.get("type"),
            task_config
        )
        if self.task_provider:
            self.task_provider.init_task(
                role_id=self.device_id or self.session_id,
                llm=self.llm
            )
    except Exception as e:
        self.logger.error(f"初始化任务提供者失败: {e}")

# 在会话结束时检测任务
if self.task_provider and self.device_id:
    owner_phone = get_owner_phone_for_device(self.device_id)
    if owner_phone:
        tasks = get_assigned_tasks_for_user(owner_phone)
        conversation = self.dialogue.get_llm_dialogue()
        
        matched_tasks = await self.task_provider.detect_task(
            msgs=conversation,
            tasks=tasks,
            user_id=owner_phone
        )
        
        if matched_tasks:
            process_user_action(owner_phone, matched_tasks)
```

## 返回格式

任务检测方法返回一个列表，每个元素包含：

```python
[
    {
        "task_id": "task_123",           # 任务ID
        "task_action": "action_name",    # 任务动作
        "match_reason": "对话中提到了..."  # 匹配原因
    },
    ...
]
```

## 自定义任务提供者

要创建自定义任务提供者：

1. 在 `providers/task/` 下创建新目录
2. 创建同名的 Python 文件
3. 继承 `TaskProviderBase` 并实现 `detect_task` 方法

**示例：**

```python
from ..base import TaskProviderBase, logger

class TaskProvider(TaskProviderBase):
    def __init__(self, config):
        super().__init__(config)
        # 初始化配置
        
    async def detect_task(self, msgs, tasks=None, user_id=None):
        # 实现任务检测逻辑
        matched_tasks = []
        # ... 处理逻辑 ...
        return matched_tasks
```

## 注意事项

1. 任务检测需要 LLM 实例，确保在调用 `detect_task` 前已初始化
2. 建议在会话结束时调用任务检测，避免频繁调用影响性能
3. 返回的任务列表可能为空，需要进行判断
4. 任务检测是异步方法，需要使用 `await` 调用

## 配置项说明

### llm_task 提供者配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| use_chinese | bool | true | 是否使用中文提示词 |
| max_tokens | int | 1000 | LLM最大输出tokens |
| temperature | float | 0.2 | LLM温度参数 |
| stateless | bool | true | 是否使用无状态模式 |

