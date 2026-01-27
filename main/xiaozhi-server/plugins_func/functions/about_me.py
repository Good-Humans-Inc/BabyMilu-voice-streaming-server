from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action

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
    """
    Inspect the server's tool registry and return a structured list
    of available capabilities. This function returns facts only;
    the LLM will render the final response in-character.
    """
    logger.bind(tag=TAG).info("get_capabilities tool invoked")
    try:
        tool_manager = conn.func_handler.tool_manager
        all_tools = tool_manager.get_all_tools()
    except Exception as e:
        logger.bind(tag=TAG).error(f"Failed to retrieve tool registry: {e}")

        return ActionResponse(
            Action.REQLLM,
            {
                "instruction": "Explain the assistant's capabilities.",
                "capabilities": [],
                "error": "Failed to retrieve tool registry",
            },
            None,
        )

    capabilities = []

    for tool in all_tools:
        try:
            func_desc = tool.description.get("function", {})
            params = func_desc.get("parameters", {}).get("properties", {})

            capability = {
                "action": tool.name,
                "description": func_desc.get("description", ""),
                "options": list(params.keys()),
                "tool_type": getattr(tool, "tool_type", None),
            }

            capabilities.append(capability)

        except Exception as e:
            logger.bind(tag=TAG).warning(
                f"Skipping tool {getattr(tool, 'name', 'unknown')}: {e}"
            )

    payload = {
        "instruction": (
            "Use the following capability list to explain what you can do. "
            "Be concise and user-friendly."
        ),
        "capabilities": capabilities,
    }

    return ActionResponse(Action.REQLLM, payload, None)
