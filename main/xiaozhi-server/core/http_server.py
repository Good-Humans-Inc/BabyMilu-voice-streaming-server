import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from services.messaging.mqtt import publish_ws_start, publish_auto_update, publish_rtc_alarm
import os
import json
from core.utils.mac import normalize_mac
from datetime import datetime, timezone

TAG = __name__


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class SimpleHttpServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)
        self._alarm_tasks = set()

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
                    # Arm a device-side BM8563 RTC alarm and optional offline WAV cache
                    web.post("/alarm/rtc", self.handle_alarm_rtc),
                    # Publish animation auto_update to device via MQTT
                    web.post("/animation/auto_updates", self.handle_animation_auto_updates),
                ]
            )

            # Serve the test frontend as static files
            test_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test")
            if os.path.isdir(test_dir):
                app.router.add_static("/test/", path=test_dir, show_index=True)
                self.logger.bind(tag=TAG).info("Test frontend served at http://{}:{}/test/test_page.html", host, port)

            # 运行服务
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()

            # 保持服务运行
            while True:
                await asyncio.sleep(3600)  # 每隔 1 小时检查一次

    def _track_alarm_task(self, task: asyncio.Task) -> None:
        self._alarm_tasks.add(task)

        def _cleanup(done: asyncio.Task) -> None:
            self._alarm_tasks.discard(done)
            try:
                done.result()
            except Exception as exc:
                self.logger.bind(tag=TAG).warning(
                    f"Delayed ws_start task failed: {type(exc).__name__}: {exc}"
                )

        task.add_done_callback(_cleanup)

    async def _publish_ws_start_after_delay(
        self,
        *,
        delay_seconds: float,
        broker: str,
        device_id: str,
        ws_url: str,
        version: int,
        reminder_id: str,
    ) -> None:
        await asyncio.sleep(max(0.0, delay_seconds))
        ok = await asyncio.to_thread(
            publish_ws_start,
            broker,
            device_id,
            ws_url,
            version=version,
        )
        self.logger.bind(tag=TAG).info(
            f"Delayed ws_start fired ok={bool(ok)} device={device_id} reminder={reminder_id or '<none>'}"
        )

    async def handle_alarm_ws_start(self, request: web.Request) -> web.Response:
        """HTTP endpoint to publish ws_start to a device via MQTT.
        Body JSON:
        {
          "deviceId": "A4:CF:12:34:56:78",
          "wsUrl": "ws://<server>:8000/xiaozhi/v1/",
          "version": 3,
          "epoch": 1770000000,          # optional: publish at this Unix second
          "delaySeconds": 45,           # optional: publish after this delay
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
        reminder_id = str(data.get("reminderId") or data.get("reminder_id") or "").strip()

        if not device_id or not ws_url:
            return web.json_response({"ok": False, "error": "deviceId and wsUrl are required"}, status=400)

        delay_seconds = None
        if data.get("delaySeconds") is not None:
            delay_seconds = float(data.get("delaySeconds") or 0)
        else:
            trigger_at = data.get("epoch") or data.get("triggerAtEpoch")
            if trigger_at is not None:
                now_epoch = datetime.now(timezone.utc).timestamp()
                delay_seconds = max(0.0, float(trigger_at) - now_epoch)

        if delay_seconds is not None and delay_seconds > 0:
            task = asyncio.create_task(
                self._publish_ws_start_after_delay(
                    delay_seconds=delay_seconds,
                    broker=broker,
                    device_id=device_id,
                    ws_url=ws_url,
                    version=version,
                    reminder_id=reminder_id,
                )
            )
            self._track_alarm_task(task)
            return web.json_response(
                {
                    "ok": True,
                    "scheduled": True,
                    "delaySeconds": delay_seconds,
                    "deviceId": device_id,
                    "reminderId": reminder_id,
                }
            )

        ok = publish_ws_start(broker, device_id, ws_url, version=version)
        return web.json_response({"ok": bool(ok), "scheduled": False})

    async def handle_alarm_rtc(self, request: web.Request) -> web.Response:
        """HTTP endpoint to arm a device-side RTC alarm via MQTT.
        Body JSON:
        {
          "deviceId": "A4:CF:12:34:56:78",
          "epoch": 1770000000,
          "offlineWavUrl": "https://.../reminder.wav",
          "customMode": true,
          "reminderId": "abc",
          "priority": 1,
          "replayIfNoMic": true,
          "broker": "mqtt://localhost:1883"
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
        broker = (data.get("broker") or os.environ.get("MQTT_URL") or "").strip()
        epoch = data.get("epoch") or data.get("triggerAtEpoch")
        if not device_id or not epoch:
            return web.json_response({"ok": False, "error": "deviceId and epoch are required"}, status=400)

        offline_wav_url = (
            data.get("offlineWavUrl")
            or data.get("offline_wav_url")
            or data.get("wavUrl")
            or data.get("audioUrl")
            or data.get("url")
            or ""
        )
        ok = publish_rtc_alarm(
            broker,
            device_id,
            int(epoch),
            offline_wav_url=str(offline_wav_url or "").strip(),
            custom_mode=_as_bool(data.get("customMode", data.get("custom_mode", False))),
            reminder_id=str(data.get("reminderId") or data.get("reminder_id") or "").strip(),
            priority=int(data.get("priority") or 0),
            replay_if_no_mic=_as_bool(
                data.get("replayIfNoMic", data.get("replay_if_no_mic", True)),
                default=True,
            ),
        )
        return web.json_response({"ok": bool(ok)})

    async def handle_animation_auto_updates(self, request: web.Request) -> web.Response:
        """HTTP endpoint to publish animation auto_update to a device via MQTT.
        Body JSON (MAC is lowercase with ':' percent-encoded in storage path):
        {
          "deviceId": "a4:cf:12:34:56:78",
          "url": "https://storage.googleapis.com/milu-public/device_bin/<mac_enc>/mega.bin",
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
