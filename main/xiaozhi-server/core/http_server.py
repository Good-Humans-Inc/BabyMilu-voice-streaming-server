import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from services.messaging.mqtt import publish_ws_start, publish_auto_update
import os
import json
from core.utils.mac import normalize_mac

TAG = __name__


class SimpleHttpServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")

        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def start(self):
        server_config = self.config["server"]
        read_config_from_api = self.config.get("read_config_from_api", False)
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("http_port", 8003))

        if port:
            app = web.Application()

            if not read_config_from_api:
                # 如果没有开启智控台，只是单模块运行，就需要再添加简单OTA接口，用于下发websocket接口
                app.add_routes(
                    [
                        web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                        web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                        web.options("/xiaozhi/ota/", self.ota_handler.handle_post),
                    ]
                )
            # 添加路由
            app.add_routes(
                [
                    web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                    web.post("/mcp/vision/explain", self.vision_handler.handle_post),
                    web.options("/mcp/vision/explain", self.vision_handler.handle_post),
                    # Minimal alarm trigger: publish ws_start to device via MQTT
                    web.post("/alarm/ws_start", self.handle_alarm_ws_start),
                    # Publish animation auto_update to device via MQTT
                    web.post("/animation/auto_updates", self.handle_animation_auto_updates),
                ]
            )

            # 运行服务
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()

            # 保持服务运行
            while True:
                await asyncio.sleep(3600)  # 每隔 1 小时检查一次

    async def handle_alarm_ws_start(self, request: web.Request) -> web.Response:
        """HTTP endpoint to publish ws_start to a device via MQTT.
        Body JSON:
        {
          "deviceId": "A4:CF:12:34:56:78",
          "wsUrl": "ws://<server>:8000/xiaozhi/v1/",
          "version": 3,
          "broker": "mqtt://localhost:1883"   # optional, fallback env MQTT_URL
        }
        """
        try:
            data = await request.json()
        except Exception:
            text = await request.text()
            try:
                data = json.loads(text)
            except Exception:
                return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        device_id = (data.get("deviceId") or data.get("device_id") or "").strip()
        device_id = normalize_mac(device_id) if device_id else device_id
        ws_url = (data.get("wsUrl") or data.get("wss") or "").strip()
        version = int(data.get("version") or 3)
        broker = (data.get("broker") or os.environ.get("MQTT_URL") or "").strip()

        if not device_id or not ws_url:
            return web.json_response({"ok": False, "error": "deviceId and wsUrl are required"}, status=400)

        ok = publish_ws_start(broker, device_id, ws_url, version=version)
        return web.json_response({"ok": bool(ok)})

    async def handle_animation_auto_updates(self, request: web.Request) -> web.Response:
        """HTTP endpoint to publish animation auto_update to a device via MQTT.
        Body JSON:
        {
          "deviceId": "A4:CF:12:34:56:78",
          "url": "https://storage.googleapis.com/milu-public/device_bin/<MAC_ENC>/mega.bin",
          "broker": "mqtt://host:1883"   # optional, fallback env MQTT_URL
        }
        """
        try:
            data = await request.json()
        except Exception:
            text = await request.text()
            try:
                data = json.loads(text)
            except Exception:
                return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        device_id = (data.get("deviceId") or data.get("device_id") or "").strip()
        device_id = normalize_mac(device_id) if device_id else device_id
        url = (data.get("url") or "").strip()
        broker = (data.get("broker") or os.environ.get("MQTT_URL") or "").strip()

        if not device_id or not url:
            return web.json_response({"ok": False, "error": "deviceId and url are required"}, status=400)

        ok = publish_auto_update(broker, device_id, url)
        return web.json_response({"ok": bool(ok)})
