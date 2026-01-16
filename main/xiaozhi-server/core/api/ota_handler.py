import json
import time
from aiohttp import web
from core.utils.util import get_local_ip
from core.api.base_handler import BaseHandler
import os
import aiohttp
from core.utils.mac import normalize_mac

TAG = __name__


class OTAHandler(BaseHandler):
    def __init__(self, config: dict):
        super().__init__(config)

    async def _fetch_json(self, url: str) -> dict | None:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        self.logger.bind(tag=TAG).warning(f"Fetch manifest failed: {url} {resp.status}")
                        return None
                    text = await resp.text()
                    return json.loads(text)
        except Exception as e:
            self.logger.bind(tag=TAG).warning(f"Fetch manifest exception: {e}")
            return None

    async def _load_ota_manifest(self, device_report: dict, device_id: str = "") -> dict | None:
        """
        Returns a dict like: {
            "version": "1.7.6", 
            "url": "https://.../xiaozhi.bin", 
            "force": 0,
            "mqtt": {"endpoint": "...", "client_id": "...", "publish_topic": "..."}
        }
        Priority:
          1) OTA_MANIFEST_URL (HTTP/HTTPS JSON), fetched per request (no restart needed)
          2) OTA_MANIFEST_PATH (local JSON file), read per request
          3) OTA_URL (+ optional OTA_VERSION, OTA_FORCE) from env (requires restart if changed)
        
        Supports device-specific config via "devices" key in manifest:
        {
            "version": "1.7.6",
            "url": "...",
            "mqtt": {"endpoint": "global:1883"},  // Global default
            "devices": {
                "AA:BB:CC:DD:EE:FF": {
                    "mqtt": {"endpoint": "device-specific:1883"}
                }
            }
        }
        """
        manifest_url = os.environ.get("OTA_MANIFEST_URL", "").strip()
        if manifest_url:
            data = await self._fetch_json(manifest_url)
            if isinstance(data, dict) and ("url" in data or "version" in data):
                result = {
                    "version": str(data.get("version") or device_report.get("application", {}).get("version", "1.0.0")),
                    "url": str(data.get("url") or ""),
                    "force": int(data.get("force") or 0),
                }
                # Extract MQTT config (device-specific or global)
                mqtt_config = self._extract_mqtt_config(data, device_id)
                if mqtt_config:
                    result["mqtt"] = mqtt_config
                return result

        manifest_path = os.environ.get("OTA_MANIFEST_PATH", "").strip()
        if manifest_path:
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and ("url" in data or "version" in data):
                    result = {
                        "version": str(data.get("version") or device_report.get("application", {}).get("version", "1.0.0")),
                        "url": str(data.get("url") or ""),
                        "force": int(data.get("force") or 0),
                    }
                    # Extract MQTT config (device-specific or global)
                    mqtt_config = self._extract_mqtt_config(data, device_id)
                    if mqtt_config:
                        result["mqtt"] = mqtt_config
                    return result
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"Read manifest file failed: {manifest_path} err={e}")

        ota_url = os.environ.get("OTA_URL", "").strip()
        if ota_url:
            version = os.environ.get("OTA_VERSION", "").strip() or device_report.get("application", {}).get("version", "1.0.0")
            force = int(os.environ.get("OTA_FORCE", "0").strip() or "0")
            return {
                "version": version,
                "url": ota_url,
                "force": force,
            }

        return None

    def _extract_mqtt_config(self, manifest_data: dict, device_id: str = "") -> dict | None:
        """
        Extract MQTT configuration from manifest.
        Priority: device-specific > global mqtt config
        
        Args:
            manifest_data: The manifest JSON data
            device_id: Device ID (MAC address) in uppercase, e.g. "AA:BB:CC:DD:EE:FF"
        
        Returns:
            MQTT config dict or None
        """
        # Check for device-specific MQTT config first
        if device_id:
            devices = manifest_data.get("devices", {})
            if isinstance(devices, dict):
                device_config = devices.get(device_id, {})
                if isinstance(device_config, dict) and "mqtt" in device_config:
                    mqtt_config = device_config.get("mqtt", {})
                    if isinstance(mqtt_config, dict) and mqtt_config:
                        self.logger.bind(tag=TAG).info(f"Using device-specific MQTT config for {device_id}")
                        return mqtt_config
        
        # Fall back to global MQTT config
        mqtt_config = manifest_data.get("mqtt", {})
        if isinstance(mqtt_config, dict) and mqtt_config:
            self.logger.bind(tag=TAG).info("Using global MQTT config from manifest")
            return mqtt_config
        
        return None

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket", "")

        if "你的" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def handle_post(self, request):
        """处理 OTA POST 请求"""
        try:
            data = await request.text()
            self.logger.bind(tag=TAG).debug(f"OTA请求方法: {request.method}")
            self.logger.bind(tag=TAG).debug(f"OTA请求头: {request.headers}")
            self.logger.bind(tag=TAG).debug(f"OTA请求数据: {data}")

            device_id = request.headers.get("device-id", "")
            if device_id:
                # 标准化：始终输出冒号分隔小写
                device_id = normalize_mac(device_id)
                self.logger.bind(tag=TAG).info(f"OTA请求设备ID: {device_id}")
            else:
                raise Exception("OTA请求设备ID为空")

            data_json = json.loads(data)

            server_config = self.config["server"]
            port = int(server_config.get("port", 8000))
            local_ip = get_local_ip()
            mac_upper = (device_id or "").strip().upper()
            # 主题使用标准化小写冒号格式，避免大小写/分隔符不一致
            normalized_mac = normalize_mac(device_id or "")
            publish_topic = f"xiaozhi/{normalized_mac}/up" if normalized_mac else ""

            # Load dynamic OTA manifest (no server restart needed)
            manifest = await self._load_ota_manifest(data_json, mac_upper)  # may be None
            if manifest:
                fw_version = manifest.get("version") or data_json["application"].get("version", "1.0.0")
                fw_url = manifest.get("url", "")
                fw_force = int(manifest.get("force", 0))
                manifest_mqtt = manifest.get("mqtt")
            else:
                fw_version = data_json["application"].get("version", "1.0.0")
                fw_url = ""
                fw_force = 0
                manifest_mqtt = None

            # Build MQTT config: manifest > environment variable > server's own IP (clean environment)
            # Default: use server's own IP + MQTT port (since MQTT runs on same server)
            mqtt_endpoint = f"{local_ip}:1883"
            if manifest_mqtt and isinstance(manifest_mqtt, dict):
                # Use endpoint from manifest if provided
                mqtt_endpoint = manifest_mqtt.get("endpoint", mqtt_endpoint)
                # Use client_id from manifest if provided, otherwise use MAC
                mqtt_client_id = manifest_mqtt.get("client_id", mac_upper)
                # Use publish_topic from manifest if provided, otherwise derive from MAC
                mqtt_publish_topic = manifest_mqtt.get("publish_topic", publish_topic)
                self.logger.bind(tag=TAG).info(f"Using MQTT config from manifest: endpoint={mqtt_endpoint}")
            else:
                # Check environment variable as fallback
                env_mqtt_endpoint = os.environ.get("MQTT_ENDPOINT", "").strip()
                if env_mqtt_endpoint:
                    mqtt_endpoint = env_mqtt_endpoint
                    self.logger.bind(tag=TAG).info(f"Using MQTT endpoint from environment: {mqtt_endpoint}")
                mqtt_client_id = mac_upper
                mqtt_publish_topic = publish_topic

            return_json = {
                "server_time": {
                    "timestamp": int(round(time.time() * 1000)),
                    "timezone_offset": server_config.get("timezone_offset", 8) * 60,
                },
                "firmware": {
                    "version": fw_version,
                    "url": fw_url,
                    "force": fw_force,
                },
                "websocket": {
                    "url": self._get_websocket_url(local_ip, port),
                },
                # Provide MQTT settings so device can switch to MQTT protocol
                # Configurable via OTA manifest (per-device or global) or MQTT_ENDPOINT env var
                "mqtt": {
                    "endpoint": mqtt_endpoint,
                    "client_id": mqtt_client_id,
                    "publish_topic": mqtt_publish_topic,
                },
            }
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        except Exception as e:
            return_json = {"success": False, "message": "request error."}
            response = web.Response(
                text=json.dumps(return_json, separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_get(self, request):
        """处理 OTA GET 请求"""
        try:
            server_config = self.config["server"]
            local_ip = get_local_ip()
            port = int(server_config.get("port", 8000))
            websocket_url = self._get_websocket_url(local_ip, port)
            message = f"OTA接口运行正常，向设备发送的websocket地址是：{websocket_url}"
            response = web.Response(text=message, content_type="text/plain")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"OTA GET请求异常: {e}")
            response = web.Response(text="OTA接口异常", content_type="text/plain")
        finally:
            self._add_cors_headers(response)
            return response
