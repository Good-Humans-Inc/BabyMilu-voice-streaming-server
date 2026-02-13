import requests
from datetime import datetime
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action

TAG = __name__
logger = setup_logging()

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get the weather for a location. The user should provide a location, for example if the user says 'weather in San Francisco', the parameter should be 'San Francisco'. "
            "If the user mentions a state/province, use the capital city by default. If the user mentions a place name that is not a province or city, use the capital city of the province where that place is located by default. "
            "If the user does not specify a location, saying things like 'how's the weather' or 'what's the weather like today', the location parameter should be empty."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Location name, e.g., San Francisco. Optional parameter, if not provided, do not pass it",
                },
                "lang": {
                    "type": "string",
                    "description": "Language code for the response is en_US",
                },
            },
            "required": ["lang"],
        },
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36"
    )
}

# WMO Weather Interpretation Codes (Open-Meteo uses WMO codes)
# https://open-meteo.com/en/docs
WEATHER_CODE_MAP = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Drizzle (light)",
    53: "Rain (moderate)",
    55: "Rain (heavy)",
    56: "Freezing drizzle (light)",
    57: "Freezing drizzle (dense)",
    61: "Rain (slight)",
    63: "Rain (moderate)",
    65: "Rain (heavy)",
    66: "Freezing rain (light)",
    67: "Freezing rain (heavy)",
    71: "Snowfall (slight)",
    73: "Snowfall (moderate)",
    75: "Snowfall (heavy)",
    77: "Snow grains",
    80: "Rain showers (slight)",
    81: "Rain showers (moderate)",
    82: "Rain showers (violent)",
    85: "Snow showers (slight)",
    86: "Snow showers (heavy)",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def fetch_city_info(location):
    """
    Uses Open-Meteo Geocoding API to search for a city and returns
    the latitude, longitude, and city name of the top result.
    """
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": location, "count": 1, "language": "en"}
    
    try:
        response = requests.get(url, params=params, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        
        if not results:
            logger.bind(tag=TAG).error(f"City not found: {location}")
            return None
        
        # Pick the first (most relevant) result
        top_result = results[0]
        return {
            "name": top_result.get("name", location),
            "latitude": top_result.get("latitude"),
            "longitude": top_result.get("longitude"),
            "country": top_result.get("country", ""),
            "admin1": top_result.get("admin1", ""),
            "timezone": top_result.get("timezone", "auto"),
        }
    except requests.RequestException as e:
        logger.bind(tag=TAG).error(f"Failed to get city information: {str(e)}")
        return None


def fetch_weather_forecast(latitude, longitude, timezone="auto", forecast_days=7):
    """
    Uses Open-Meteo Forecast API to fetch forecast data for given coordinates.
    """
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": timezone,
        "forecast_days": forecast_days,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
        "hourly": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
    }
    
    try:
        response = requests.get(url, params=params, headers=HEADERS)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.bind(tag=TAG).error(f"Failed to get weather data: {str(e)}")
        return None


def get_weather_report_for_location(
    location: str,
    config: dict,
    cache_manager,
    client_ip: str = None,
    lang: str = "en_US",
) -> str:
    """
    Get weather report string for a location. Uses Open-Meteo (same as get_weather tool).
    Used by prompt_manager for {{weather_info}} and shares cache with get_weather.
    """
    from core.utils.cache.manager import CacheType

    plugins_cfg = config.get("plugins", {}) or {}
    get_weather_cfg = plugins_cfg.get("get_weather", {}) or {}
    default_location = get_weather_cfg.get("default_location", "San Francisco")

    # Resolve location: user-provided -> LOCATION cache -> default
    if not location and client_ip:
        resolved = cache_manager.get(CacheType.LOCATION, client_ip)
        if resolved:
            location = resolved
    if not location:
        location = default_location

    weather_cache_key = f"full_weather_{location}_{lang}"
    cached = cache_manager.get(CacheType.WEATHER, weather_cache_key)
    if cached:
        return cached

    city_info = fetch_city_info(location)
    if not city_info:
        return "Weather unavailable"

    weather_data = fetch_weather_forecast(
        city_info["latitude"],
        city_info["longitude"],
        city_info.get("timezone", "auto"),
        forecast_days=7,
    )
    if not weather_data:
        return "Weather unavailable"

    parsed_info = parse_weather_info(weather_data, city_info)
    city_name = parsed_info["city_name"]
    if city_info.get("admin1"):
        city_name += f", {city_info['admin1']}"
    if city_info.get("country"):
        city_name += f", {city_info['country']}"

    weather_report = f"Location queried: {city_name}\n\n"
    weather_report += f"Current weather: {parsed_info['current_weather']}\n"
    weather_report += f"Current temperature: {parsed_info['current_temp']}°C\n"
    weather_report += f"Humidity: {parsed_info['current_humidity']}%\n"
    weather_report += f"Wind speed: {parsed_info['current_wind']} km/h\n"
    weather_report += "\n7-day forecast:\n"
    for date, weather, high, low in parsed_info["temps_list"]:
        weather_report += f"{date}: {weather}, temperature {low}°C~{high}°C\n"
    weather_report += "\n(If you need specific weather for a particular day, please tell me the date)"

    cache_manager.set(CacheType.WEATHER, weather_cache_key, weather_report)
    return weather_report


def parse_weather_info(weather_data, city_info):
    """
    Parse Open-Meteo weather data into a structured format.
    """
    current = weather_data.get("current", {})
    daily = weather_data.get("daily", {})
    
    # Current weather
    current_temp = current.get("temperature_2m", "N/A")
    current_weather_code = current.get("weather_code", 0)
    current_weather = WEATHER_CODE_MAP.get(current_weather_code, "Unknown")
    current_humidity = current.get("relative_humidity_2m", "N/A")
    current_wind = current.get("wind_speed_10m", "N/A")
    
    # Daily forecast (7 days)
    temps_list = []
    dates = daily.get("time", [])
    weather_codes = daily.get("weather_code", [])
    temp_max = daily.get("temperature_2m_max", [])
    temp_min = daily.get("temperature_2m_min", [])
    precipitation = daily.get("precipitation_sum", [])
    wind_max = daily.get("wind_speed_10m_max", [])
    
    for i in range(min(7, len(dates))):
        date_str = dates[i]
        # Format date for display
        try:
            date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            date_display = date_obj.strftime("%B %d")
        except:
            date_display = date_str
        
        weather_code = weather_codes[i] if i < len(weather_codes) else 0
        weather = WEATHER_CODE_MAP.get(weather_code, "Unknown")
        high_temp = temp_max[i] if i < len(temp_max) else "N/A"
        low_temp = temp_min[i] if i < len(temp_min) else "N/A"
        
        temps_list.append((date_display, weather, high_temp, low_temp))
    
    return {
        "city_name": city_info.get("name", "Unknown"),
        "current_temp": current_temp,
        "current_weather": current_weather,
        "current_humidity": current_humidity,
        "current_wind": current_wind,
        "temps_list": temps_list,
    }


@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
def get_weather(conn, location: str = None, lang: str = "en_US"):
    from core.utils.cache.manager import cache_manager

    weather_report = get_weather_report_for_location(
        location=location,
        config=conn.config,
        cache_manager=cache_manager,
        client_ip=conn.client_ip,
        lang=lang,
    )
    if weather_report == "Weather unavailable":
        return ActionResponse(
            Action.REQLLM, f"City not found: {location or 'default'}, please verify the location is correct", None
        )
    return ActionResponse(Action.REQLLM, weather_report, None)