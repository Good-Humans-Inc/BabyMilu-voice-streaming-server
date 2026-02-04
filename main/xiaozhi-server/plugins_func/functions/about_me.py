from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.firestore_client import (
    get_active_character_for_device,
    get_character_profile,
    extract_character_profile_fields,
    get_owner_phone_for_device,
    get_user_profile_by_phone,
    extract_user_profile_fields,
)

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

    for tool in all_tools.values():
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
    
    logger.bind(tag=TAG).info(capabilities)

    payload = {
        "instruction": (
            "Based ONLY on the capabilities list below, explain what you can do. "
            "Do NOT mention any features not in this list. "
            "Be concise and user-friendly. "
            "Only describe the actions that are actually available."
        ),
        "capabilities": capabilities,
    }

    return ActionResponse(Action.REQLLM, payload, None)


SELF_INTRODUCTION_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "self_introduction",
        "description": (
            "Introduce yourself to someone new (like the user's friend). "
            "Share who you are, your personality, and your relationship with the user. "
            "Use this when meeting new people or when asked to introduce yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "Optional context about who you're introducing yourself to or the situation",
                }
            },
        },
    },
}


@register_function("self_introduction", SELF_INTRODUCTION_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def self_introduction(conn, context: str = ""):
    """
    Introduce yourself to someone new. Gather character profile information
    and user relationship details, then let the LLM render a natural introduction
    in the character's voice.
    """
    logger.bind(tag=TAG).info("self_introduction tool invoked")
    
    try:
        char_id = None
        if conn.device_id:
            char_id = get_active_character_for_device(conn.device_id)
        
        character_info = {}
        if char_id:
            char_doc = get_character_profile(char_id)
            if char_doc:
                fields = extract_character_profile_fields(char_doc or {})
                character_info = {
                    "name": fields.get("name"),
                    "age": fields.get("age"),
                    "pronouns": fields.get("pronouns"),
                    "relationship": fields.get("relationship"),
                    "callMe": fields.get("callMe"),
                    "bio": fields.get("bio"),
                }
        
        # Fetch user profile
        user_info = {}
        try:
            owner_phone = get_owner_phone_for_device(conn.device_id)
            if owner_phone:
                user_doc = get_user_profile_by_phone(owner_phone)
                if user_doc:
                    fields = extract_user_profile_fields(user_doc or {})
                    user_info = {
                        "name": fields.get("name"),
                        "pronouns": fields.get("pronouns"),
                    }
        except Exception as e:
            logger.bind(tag=TAG).warning(f"Failed to fetch user profile: {e}")
        
        # Get relationship duration if available
        relationship_duration = getattr(conn, "num_days_together", None)
        
        payload = {
            "instruction": (
                "Introduce yourself naturally and warmly. Share who you are, what makes you special, "
                "and your relationship with the user. Be authentic and stay in character. "
                "Keep it conversational and engaging for the person you're meeting. "
                "If context about who you're meeting is provided, tailor your introduction accordingly."
            ),
            "character": character_info,
            "user": user_info,
            "relationship_days": relationship_duration,
            "context": context,
        }
        
        logger.bind(tag=TAG).info(f"self_introduction payload prepared: {payload}")
        
        return ActionResponse(Action.REQLLM, payload, None)
    
    except Exception as e:
        logger.bind(tag=TAG).error(f"Failed in self_introduction: {e}")
        return ActionResponse(
            Action.REQLLM,
            {
                "instruction": "Introduce yourself in a warm and friendly way.",
                "character": {},
                "user": {},
                "error": str(e),
            },
            None,
        )
