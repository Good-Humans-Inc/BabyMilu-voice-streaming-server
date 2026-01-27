from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from ...core.providers.tools.base.tool_types import ToolDefinition
import json


TAG = __name__
logger = setup_logging()

GET_CAPABILITIES_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_capabilities",
        "description": (
            "Return a structured list of the assistant's current capabilities. "
            "This should be used when the user asks what the assistant can do, "
            "what features are available, or how the assistant can help."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

@register_function("get_capabilities", GET_CAPABILITIES_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def get_capabilities(conn):
    logger.bind(tag=TAG).info("get_capabilities tool invoked")

    tool_manager = conn.func_handler.tool_manager
    all_tools = tool_manager.get_all_tools()

    capabilities = []

    for tool in all_tools:
        # -------- Case 1: Server plugin / MCP endpoint tool --------
        if isinstance(tool, ToolDefinition):
            func_desc = tool.description.get("function")
            if not isinstance(func_desc, dict):
                continue

            params = func_desc.get("parameters", {}).get("properties", {})

            capabilities.append({
                "action": tool.name,
                "description": func_desc.get("description", ""),
                "options": list(params.keys()),
                "tool_type": tool.tool_type.value,
            })
            continue

        # -------- Case 2: MCP tools returned as dict --------
        if isinstance(tool, dict):
            func_desc = tool.get("function")
            if not isinstance(func_desc, dict):
                continue

            params = func_desc.get("parameters", {}).get("properties", {})

            capabilities.append({
                "action": func_desc.get("name"),
                "description": func_desc.get("description", ""),
                "options": list(params.keys()),
                "tool_type": "mcp",
            })
            continue

    payload = {
        "instruction": "Use the following capability list to explain what you can do.",
        "capabilities": capabilities,
    }

    return ActionResponse(
        Action.REQLLM,
        json.dumps(payload, ensure_ascii=False),
        None,
    )