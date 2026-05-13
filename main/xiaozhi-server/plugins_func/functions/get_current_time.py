import json
from typing import Optional

from config.logger import setup_logging
from core.utils.current_time import (
    get_current_date,
    get_current_time as format_current_time,
    get_current_weekday,
)
from core.utils.firestore_client import get_timezone_for_device
from plugins_func.register import Action, ActionResponse, ToolType, register_function

TAG = __name__
logger = setup_logging()

GET_CURRENT_TIME_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": (
            "Get the current time and date for the current user. "
            "Use this whenever the user asks what time it is, what day it is, "
            "today's date, or any time/date question about 'now'. "
            "The server resolves the user's timezone from their device/user profile."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


def _resolve_user_timezone(conn) -> Optional[str]:
    device_id = getattr(conn, "device_id", None)
    if not device_id:
        return None
    try:
        return get_timezone_for_device(device_id)
    except Exception as exc:
        logger.bind(tag=TAG).warning(
            f"Failed to resolve timezone for device {device_id}: {exc}"
        )
        return None


@register_function("get_current_time", GET_CURRENT_TIME_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def get_current_time(conn) -> ActionResponse:
    timezone = _resolve_user_timezone(conn)
    payload = {
        "current_time": format_current_time(timezone),
        "today_date": get_current_date(timezone),
        "today_weekday": get_current_weekday(timezone),
        "timezone": timezone or "server local time",
        "instruction": (
            "Answer the user's time/date question using only this tool result. "
            "Do not use older time/date values from the conversation context."
        ),
    }
    return ActionResponse(
        action=Action.REQLLM,
        result=json.dumps(payload, ensure_ascii=False),
    )
