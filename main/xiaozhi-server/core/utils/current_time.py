"""
时间工具模块
提供统一的时间获取功能
"""

import cnlunar
from datetime import datetime
from typing import Optional
try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _now_in_timezone(timezone: Optional[str]) -> datetime:
    """
    Return a timezone-aware 'now' in the given IANA timezone if provided and valid.
    Falls back to naive local time if ZoneInfo is unavailable or timezone invalid.
    """
    if timezone and ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(timezone))
        except Exception:
            pass
    return datetime.now()


def get_current_time(timezone: Optional[str] = None) -> str:
    """
    获取当前时间字符串 (格式: HH:MM)
    """
    now = _now_in_timezone(timezone)
    # 12-hour format with AM/PM, removing leading zero from hour
    return now.strftime("%I:%M %p").lstrip("0")


def get_current_date(timezone: Optional[str] = None) -> str:
    """
    获取今天日期字符串 (格式: YYYY-MM-DD)
    """
    now = _now_in_timezone(timezone)
    return now.strftime("%Y-%m-%d")


def get_current_weekday(timezone: Optional[str] = None) -> str:
    """
    获取今天星期几
    """
    now = _now_in_timezone(timezone)
    return now.strftime("%A")


def get_current_lunar_date() -> str:
    """
    获取农历日期字符串
    """
    try:
        now = datetime.now()
        today_lunar = cnlunar.Lunar(now, godType="8char")
        return "%s年%s%s" % (
            today_lunar.lunarYearCn,
            today_lunar.lunarMonthCn[:-1],
            today_lunar.lunarDayCn,
        )
    except Exception:
        return "农历获取失败"


def get_current_time_info() -> tuple:
    """
    获取当前时间信息
    返回: (当前时间字符串, 今天日期, 今天星期, 农历日期)
    """
    current_time = get_current_time(timezone)
    today_date = get_current_date(timezone)
    today_weekday = get_current_weekday(timezone)
    lunar_date = get_current_lunar_date()
    
    return current_time, today_date, today_weekday, lunar_date
