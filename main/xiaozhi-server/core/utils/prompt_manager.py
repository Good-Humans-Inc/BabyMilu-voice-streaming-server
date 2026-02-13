"""
系统提示词管理器模块
负责管理和更新系统提示词，包括快速初始化和异步增强功能
"""

import os
import json
import cnlunar
from typing import Dict, Any
from config.logger import setup_logging
from jinja2 import Template
from core.utils.firestore_client import get_timezone_for_device, get_owner_phone_for_device, get_user_profile_by_phone, extract_user_profile_fields
from core.utils.textUtils import get_emoji_list_for_prompt

TAG = __name__

WEEKDAY_MAP = {
    "Monday": "Monday",
    "Tuesday": "Tuesday",
    "Wednesday": "Wednesday",
    "Thursday": "Thursday",
    "Friday": "Friday",
    "Saturday": "Saturday",
    "Sunday": "Sunday",
}

class PromptManager:
    """系统提示词管理器，负责管理和更新系统提示词"""

    def __init__(self, config: Dict[str, Any], logger=None):
        self.config = config
        self.logger = logger or setup_logging()
        self.base_prompt_template = None
        self.last_update_time = 0

        # 导入全局缓存管理器
        from core.utils.cache.manager import cache_manager, CacheType

        self.cache_manager = cache_manager
        self.CacheType = CacheType

        self._load_base_template()

    def _load_base_template(self):
        """加载基础提示词模板"""
        try:
            template_path = "agent-base-prompt.txt"
            cache_key = f"prompt_template:{template_path}"

            # 先从缓存获取
            cached_template = self.cache_manager.get(self.CacheType.CONFIG, cache_key)
            if cached_template is not None:
                self.base_prompt_template = cached_template
                self.logger.bind(tag=TAG).debug("从缓存加载基础提示词模板")
                return

            # 缓存未命中，从文件读取
            if os.path.exists(template_path):
                with open(template_path, "r", encoding="utf-8") as f:
                    template_content = f.read()

                # 存入缓存（CONFIG类型默认不自动过期，需要手动失效）
                self.cache_manager.set(
                    self.CacheType.CONFIG, cache_key, template_content
                )
                self.base_prompt_template = template_content
                self.logger.bind(tag=TAG).debug("成功加载基础提示词模板并缓存")
            else:
                self.logger.bind(tag=TAG).warning("未找到agent-base-prompt.txt文件")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"加载提示词模板失败: {e}")

    def get_quick_prompt(self, user_prompt: str, device_id: str = None) -> str:
        """快速获取系统提示词（使用用户配置）"""
        self.logger.bind(tag=TAG).debug("使用快速提示词")
        return user_prompt

    def _get_current_time_info(self, timezone: str = None) -> tuple:
        """获取当前时间信息"""
        from .current_time import get_current_time, get_current_date, get_current_weekday
        
        today_date = get_current_date(timezone)
        today_weekday = get_current_weekday(timezone)
        current_time = get_current_time(timezone)

        return current_time, today_date, today_weekday

    def _get_location_info(self, client_ip: str) -> str:
        """获取位置信息"""
        try:
            print(f"[WeatherDebug] _get_location_info: check cache for client_ip={client_ip!r}")
            # 先从缓存获取
            cached_location = self.cache_manager.get(self.CacheType.LOCATION, client_ip)
            if cached_location is not None:
                print(f"[WeatherDebug] _get_location_info: cache HIT -> {cached_location!r}")
                return cached_location
            print(f"[WeatherDebug] _get_location_info: cache MISS")

            # 缓存未命中，调用API获取
            from core.utils.util import get_ip_info

            ip_info = get_ip_info(client_ip, self.logger)
            print(f"[WeatherDebug] _get_location_info: ip_info -> {ip_info}")
            city = ip_info.get("city")
            location = city.strip() if isinstance(city, str) else ""

            # 存入缓存
            if location:
                self.cache_manager.set(self.CacheType.LOCATION, client_ip, location)
                print(f"[WeatherDebug] _get_location_info: resolved location={location!r} (cached by client_ip)")
            else:
                print(f"[WeatherDebug] _get_location_info: empty location resolved, skip caching")
            return location
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to get location info: {e}")
            return "Unknown location"

    def _get_user_city_from_profile(self, device_id: str) -> str:
        """优先从用户档案中读取城市字段（格式如：'San Francisco, CA'）"""
        try:
            if not device_id:
                print(f"[WeatherDebug] _get_user_city_from_profile: device_id is empty")
                return ""
            owner_phone = get_owner_phone_for_device(device_id)
            print(f"[WeatherDebug] _get_user_city_from_profile: owner_phone={owner_phone!r}")
            if not owner_phone:
                return ""
            user_doc = get_user_profile_by_phone(owner_phone)
            print(f"[WeatherDebug] _get_user_city_from_profile: user_doc exists={bool(user_doc)}")
            if not user_doc:
                return ""
            # 直接使用原始用户文档中的 city 字段（假设文档格式正确）
            try:
                print(f"[WeatherDebug] _get_user_city_from_profile: user_doc keys={list(user_doc.keys())}")
                raw_doc_str = json.dumps(user_doc, ensure_ascii=False, default=str)
                print(f"[WeatherDebug] _get_user_city_from_profile: user_doc raw={raw_doc_str[:1000]}{'...(trunc)' if len(raw_doc_str) > 1000 else ''}")
            except Exception as e:
                print(f"[WeatherDebug] _get_user_city_from_profile: failed to serialize user_doc: {e}")
            city_str = user_doc.get("city")
            if isinstance(city_str, str) and city_str.strip():
                print(f"[WeatherDebug] _get_user_city_from_profile: using raw user_doc['city'] -> {city_str!r}")
                return city_str.strip()
            print(f"[WeatherDebug] _get_user_city_from_profile: raw user_doc has no valid 'city'")
            return ""
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"读取用户城市失败: {e}")
            return ""

    def _resolve_preferred_location(self, device_id: str, client_ip: str) -> str:
        """
        决定用于上下文的地点字符串：
        1) 优先使用用户档案中的 city（'City, ST'）
        2) 否则回退到基于 IP 的城市
        """
        user_city = self._get_user_city_from_profile(device_id)
        print(f"[WeatherDebug] _resolve_preferred_location: user_city={user_city!r}")
        if user_city:
            print(f"[WeatherDebug] _resolve_preferred_location: choose user_city")
            return user_city
        fallback = self._get_location_info(client_ip) if client_ip else ""
        print(f"[WeatherDebug] _resolve_preferred_location: fallback IP city={fallback!r}")
        return fallback

    def _get_weather_info(self, location: str, client_ip: str = None) -> str:
        """获取天气信息，使用 get_weather 的 Open-Meteo 逻辑和缓存"""
        try:
            from plugins_func.functions.get_weather import get_weather_report_for_location

            return get_weather_report_for_location(
                location=location,
                config=self.config,
                cache_manager=self.cache_manager,
                client_ip=client_ip,
                lang="en_US",
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to get weather info: {e}")
            return "Weather unavailable"

    def update_context_info(self, conn, client_ip: str):
        """同步更新上下文信息"""
        try:
            print(f"[WeatherDebug] update_context_info: start device_id={getattr(conn, 'device_id', None)!r}, client_ip={client_ip!r}")
            # 优先使用用户档案中的城市；否则使用IP定位
            device_id = getattr(conn, "device_id", None)
            local_address = self._resolve_preferred_location(device_id, client_ip)
            # 将决策后的地址写入缓存（以 client_ip 为键，便于后续读取）
            if client_ip and local_address:
                self.cache_manager.set(self.CacheType.LOCATION, client_ip, local_address)
                print(f"[WeatherDebug] update_context_info: set LOCATION cache[{client_ip!r}]={local_address!r}")
            # 获取天气信息（使用 get_weather 的 Open-Meteo 逻辑和缓存）
            self._get_weather_info(local_address, client_ip)
            print(f"[WeatherDebug] update_context_info: done with local_address={local_address!r}")
            self.logger.bind(tag=TAG).info(f"上下文信息更新完成")

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新上下文信息失败: {e}")

    def build_enhanced_prompt(
        self, user_prompt: str, device_id: str, client_ip: str = None
    ) -> str:
        """构建增强的系统提示词"""
        if not self.base_prompt_template:
            return user_prompt

        try:
            # 获取最新的时间信息（不缓存）
            tz = get_timezone_for_device(device_id) if device_id else None
            current_time, today_date, today_weekday = self._get_current_time_info(tz or None)

            # 获取缓存的上下文信息
            local_address = ""
            weather_info = ""

            # 优先根据用户档案/客户端IP解析城市
            preferred_location = self._resolve_preferred_location(device_id, client_ip)
            print(f"[WeatherDebug] build_enhanced_prompt: preferred_location={preferred_location!r}")
            if preferred_location:
                local_address = preferred_location
                # 使用 get_weather 的 Open-Meteo 逻辑和缓存
                weather_info = self._get_weather_info(local_address, client_ip) or ""
                print(f"[WeatherDebug] build_enhanced_prompt: weather fetched fresh -> {weather_info!r}")
                # 将选择的地址也写入 LOCATION 缓存，便于其他模块读取
                if client_ip:
                    self.cache_manager.set(self.CacheType.LOCATION, client_ip, local_address)
                    print(f"[WeatherDebug] build_enhanced_prompt: set LOCATION cache[{client_ip!r}]={local_address!r}")

            # 替换模板变量
            template = Template(self.base_prompt_template)
            # 读取用户名称用于 {{user}}
            user_name = "user"
            try:
                if device_id:
                    owner_phone = get_owner_phone_for_device(device_id)
                    if owner_phone:
                        user_doc = get_user_profile_by_phone(owner_phone)
                        if user_doc:
                            user_fields = extract_user_profile_fields(user_doc)
                            user_name = user_fields.get("name") or "user"
            except Exception:
                user_name = "user"
            enhanced_prompt = template.render(
                base_prompt=user_prompt,
                current_time=current_time,
                today_date=today_date,
                today_weekday=today_weekday,
                local_address=local_address,
                weather_info=weather_info,
                emojiList=get_emoji_list_for_prompt(),
                device_id=device_id,
                user=user_name,
            )
            # 基本验证输出（避免打印全部prompt）
            contains_local = "{{local_address}}" in self.base_prompt_template
            contains_weather = "{{weather_info}}" in self.base_prompt_template
            print(f"[WeatherDebug] build_enhanced_prompt: template has local={contains_local}, weather={contains_weather}")
            print(f"[WeatherDebug] build_enhanced_prompt: values -> local_address={local_address!r}, weather_info={weather_info!r}")
            print(f"[WeatherDebug] build_enhanced_prompt: enhanced prompt length={len(enhanced_prompt)}")
            self.logger.bind(tag=TAG).debug("构建增强提示词成功")
            return enhanced_prompt

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"构建增强提示词失败: {e}")
            return user_prompt
