"""服务端插件工具执行器"""

import asyncio
from typing import Dict, Any
from ..base import ToolType, ToolDefinition, ToolExecutor
from plugins_func.register import all_function_registry, Action, ActionResponse


class ServerPluginExecutor(ToolExecutor):
    """服务端插件工具执行器"""

    def __init__(self, conn):
        self.conn = conn
        self.config = conn.config

    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行服务端插件工具"""
        func_item = all_function_registry.get(tool_name)
        if not func_item:
            return ActionResponse(
                action=Action.NOTFOUND, response=f"插件函数 {tool_name} 不存在"
            )

        try:
            def invoke_plugin():
                if hasattr(func_item, "type"):
                    func_type = func_item.type
                    if func_type.code in [4, 5]:
                        return func_item.func(conn, **arguments)
                    if func_type.code == 2:
                        return func_item.func(**arguments)
                    if func_type.code == 3:
                        return func_item.func(conn, **arguments)
                    return func_item.func(**arguments)
                return func_item.func(**arguments)

            if hasattr(conn, "run_sync"):
                timeout_for = getattr(conn, "executor_timeout", lambda _name: 20.0)
                result = await conn.run_sync(
                    "tool",
                    invoke_plugin,
                    timeout=timeout_for("tool"),
                )
            else:
                result = await asyncio.to_thread(invoke_plugin)

            return result

        except Exception as e:
            return ActionResponse(
                action=Action.ERROR,
                response=str(e),
            )

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """获取所有注册的服务端插件工具"""
        tools = {}

        # 获取必要的函数
        necessary_functions = ["handle_exit_intent", "get_lunar", "get_current_time"]

        # function-call tools are configured under LLM.function_call.functions.
        llm_functions = (
            self.config.get("LLM", {})
            .get("function_call", {})
            .get("functions", [])
        )

        # Some deployments also enable intent-side plugin functions; keep those too.
        intent_module = self.config.get("selected_module", {}).get("Intent")
        intent_functions = (
            self.config.get("Intent", {})
            .get(intent_module, {})
            .get("functions", [])
        )

        config_functions = []
        if isinstance(llm_functions, list):
            config_functions.extend(llm_functions)
        if isinstance(intent_functions, list):
            config_functions.extend(intent_functions)

        # 转换为列表
        if not isinstance(config_functions, list):
            try:
                config_functions = list(config_functions)
            except TypeError:
                config_functions = []

        # 合并所有需要的函数
        all_required_functions = list(set(necessary_functions + config_functions))

        for func_name in all_required_functions:
            func_item = all_function_registry.get(func_name)
            if func_item:
                tools[func_name] = ToolDefinition(
                    name=func_name,
                    description=func_item.description,
                    tool_type=ToolType.SERVER_PLUGIN,
                )

        return tools

    def has_tool(self, tool_name: str) -> bool:
        """检查是否有指定的服务端插件工具"""
        return tool_name in all_function_registry
