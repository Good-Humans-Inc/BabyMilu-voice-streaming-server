import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.mqtt_alarm import publish_ws_start, publish_down_command
from core.utils.tts import create_instance
from pydub import AudioSegment
import math
import os
import json

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
                    # Marketing: synthesize and play text with emotion on device
                    web.post("/marketing/say", self.handle_marketing_say),
                    web.post("/marketing/script", self.handle_marketing_script),
                ]
            )
            # Serve synthesized TTS files (default tmp directory)
            static_dir = os.path.abspath("tmp")
            os.makedirs(static_dir, exist_ok=True)
            app.add_routes([web.static("/tts", static_dir, show_index=False)])
            # Serve marketing UI
            test_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "test"))
            if os.path.isdir(test_dir):
                app.add_routes([web.static("/marketing", test_dir, show_index=False)])

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
        ws_url = (data.get("wsUrl") or data.get("wss") or "").strip()
        version = int(data.get("version") or 3)
        broker = (data.get("broker") or os.environ.get("MQTT_URL") or "").strip()

        if not device_id or not ws_url:
            return web.json_response({"ok": False, "error": "deviceId and wsUrl are required"}, status=400)

        ok = publish_ws_start(broker, device_id, ws_url, version=version)
        return web.json_response({"ok": bool(ok)})

    async def handle_marketing_say(self, request: web.Request) -> web.Response:
        """
        Accept text and emotion, synthesize TTS to a local file, serve it, and command device to play it.
        Body JSON:
        {
          "deviceId": "A4_CF_12_34_56_78",  # required (MAC with underscores)
          "text": "Hello from marketing!",  # required
          "emotion": "happy",               # optional
          "gain": 1.0,                      # optional, default 1.0
          "broker": "mqtt://localhost:1883",# optional, fallback env MQTT_URL
          "baseUrl": "http://host:8003"     # optional override for TTS URL base (device-accessible)
        }
        Returns:
        { "ok": true, "ttsUrl": "http://host:8003/tts/<file>.wav", "sent": {"llm":true,"play_url":true} }
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
        text_to_say = (data.get("text") or "").strip()
        emotion = (data.get("emotion") or "").strip()
        gain = float(data.get("gain") or 1.0)
        broker = (data.get("broker") or os.environ.get("MQTT_URL") or "").strip()

        if not device_id or not text_to_say:
            return web.json_response({"ok": False, "error": "deviceId and text are required"}, status=400)

        # Build TTS provider from config
        try:
            selected_tts = self.config.get("selected_module", {}).get("TTS") or "EdgeTTS"
            tts_conf = self.config.get("TTS", {}).get(selected_tts) or {}
            provider_type = tts_conf.get("type") or "edge"
            # Keep file after synthesis so device can download
            tts_provider = create_instance(provider_type, tts_conf, delete_audio_file=False)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"tts init failed: {e}"}, status=500)

        # Synthesize to file in a worker thread (avoid asyncio.run in event loop)
        try:
            loop = asyncio.get_running_loop()
            tts_file_path = await loop.run_in_executor(None, tts_provider.to_tts, text_to_say)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"tts synth failed: {e}"}, status=500)

        if not tts_file_path or not os.path.exists(tts_file_path):
            return web.json_response({"ok": False, "error": "tts file not generated"}, status=500)

        # Compute device-accessible URL
        basename = os.path.basename(tts_file_path)
        # Prefer explicit baseUrl if provided, else construct from request.host and configured http_port
        provided_base = (data.get("baseUrl") or "").rstrip("/")
        if provided_base:
            base_url = provided_base
        else:
            # request.host may include host:port of this HTTP server
            base_url = f"http://{request.host}"
        tts_url = f"{base_url}/tts/{basename}"

        sent = {"llm": False, "play_url": False}
        # Send emotion first (optional)
        if emotion:
            sent["llm"] = publish_down_command(broker, device_id, {"type": "llm", "emotion": emotion})
        # Then play generated URL
        sent["play_url"] = publish_down_command(
            broker,
            device_id,
            {"type": "play_url", "url": tts_url, "gain": gain},
        )

        return web.json_response({"ok": bool(sent["play_url"]), "ttsUrl": tts_url, "sent": sent})

    async def handle_marketing_script(self, request: web.Request) -> web.Response:
        """
        Run a multi-step script in 'download' mode (no WS streaming).
        Body JSON:
        {
          "deviceId": "A4_CF_12_34_56_78",
          "mode": "download",
          "steps": [
            { "text": "...", "emotion": "happy", "gain": 1.0, "pauseMs": 500 },
            { "text": "..." }
          ],
          "broker": "mqtt://localhost:1883",     # optional
          "baseUrl": "http://host:8003"          # optional
        }
        Returns:
        {
          "ok": true,
          "results": [
            { "idx": 0, "ttsUrl": "http://host:8003/tts/xxx.wav", "llm": true, "play_url": true },
            { "idx": 1, "ttsUrl": "http://host:8003/tts/yyy.wav", "llm": false, "play_url": true }
          ]
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
        mode = (data.get("mode") or "download").strip().lower()
        steps = data.get("steps") or []
        broker = (data.get("broker") or os.environ.get("MQTT_URL") or "").strip()
        provided_base = (data.get("baseUrl") or "").rstrip("/")
        combine = bool(data.get("combine") or False)

        if not device_id:
            return web.json_response({"ok": False, "error": "deviceId required"}, status=400)
        if mode != "download":
            return web.json_response({"ok": False, "error": "only 'download' mode supported here"}, status=400)
        if not isinstance(steps, list) or len(steps) == 0:
            return web.json_response({"ok": False, "error": "steps must be a non-empty array"}, status=400)

        # TTS provider once
        try:
            selected_tts = self.config.get("selected_module", {}).get("TTS") or "EdgeTTS"
            tts_conf = self.config.get("TTS", {}).get(selected_tts) or {}
            provider_type = tts_conf.get("type") or "edge"
            tts_provider = create_instance(provider_type, tts_conf, delete_audio_file=False)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"tts init failed: {e}"}, status=500)

        # Compute base URL for file hosting
        if provided_base:
            base_url = provided_base
        else:
            base_url = f"http://{request.host}"

        results = []
        combined_url = None

        def ensure_wav_16k_mono(src_path: str) -> str:
            try:
                root, ext = os.path.splitext(src_path)
                if ext.lower() == ".wav":
                    return src_path
                target = f"{root}.wav"
                aud = AudioSegment.from_file(src_path)
                aud = aud.set_frame_rate(16000).set_channels(1).set_sample_width(2)
                aud.export(target, format="wav")
                return target
            except Exception:
                return src_path

        if combine:
            # Build combined audio and schedule emotions
            segments = []
            schedule = []  # (offset_ms, emotion)
            current_offset = 0
            for idx, step in enumerate(steps):
                if not isinstance(step, dict):
                    results.append({"idx": idx, "error": "invalid step"})
                    continue
                text_to_say = (step.get("text") or "").strip()
                if not text_to_say:
                    results.append({"idx": idx, "error": "missing text"})
                    continue
                emotion = (step.get("emotion") or "").strip()
                gain = float(step.get("gain") or 1.0)
                pause_ms = int(step.get("pauseMs") or 0)

                try:
                    loop = asyncio.get_running_loop()
                    tts_file_path = await loop.run_in_executor(None, tts_provider.to_tts, text_to_say)
                except Exception as e:
                    results.append({"idx": idx, "error": f"tts synth failed: {e}"})
                    continue
                if not tts_file_path or not os.path.exists(tts_file_path):
                    results.append({"idx": idx, "error": "tts file not generated"})
                    continue

                wav_path = ensure_wav_16k_mono(tts_file_path)
                try:
                    seg = AudioSegment.from_file(wav_path).set_frame_rate(16000).set_channels(1).set_sample_width(2)
                except Exception:
                    results.append({"idx": idx, "error": "failed to load audio"})
                    continue
                if gain > 0 and abs(gain - 1.0) > 1e-6:
                    gain_db = 20.0 * math.log10(gain)
                    seg = seg.apply_gain(gain_db)

                if emotion:
                    schedule.append((current_offset, emotion))
                segments.append(seg)
                current_offset += int(seg.duration_seconds * 1000.0)
                if pause_ms > 0:
                    segments.append(AudioSegment.silent(duration=pause_ms))
                    current_offset += pause_ms

            if len(segments) == 0:
                return web.json_response({"ok": False, "error": "no valid steps"}, status=400)

            combined = segments[0]
            for s in segments[1:]:
                combined += s
            combined = combined.set_frame_rate(16000).set_channels(1).set_sample_width(2)

            os.makedirs("tmp", exist_ok=True)
            combined_name = f"tts-combined-{int(asyncio.get_running_loop().time()*1000)}.wav"
            combined_path = os.path.join("tmp", combined_name)
            combined.export(combined_path, format="wav")
            combined_url = f"{base_url}/tts/{combined_name}"

            play_ok = publish_down_command(
                broker,
                device_id,
                {"type": "play_url", "url": combined_url, "gain": 1.0},
            )
            results.append({"idx": 0, "ttsUrl": combined_url, "play_url": bool(play_ok), "combined": True})

            async def schedule_emotions():
                start = asyncio.get_running_loop().time()
                for offset_ms, emo in schedule:
                    delay = max(0.0, (offset_ms / 1000.0) - (asyncio.get_running_loop().time() - start))
                    if delay > 0:
                        try:
                            await asyncio.sleep(delay)
                        except Exception:
                            pass
                    publish_down_command(broker, device_id, {"type": "llm", "emotion": emo})

            asyncio.create_task(schedule_emotions())
        else:
            for idx, step in enumerate(steps):
                if not isinstance(step, dict):
                    results.append({"idx": idx, "error": "invalid step"})
                    continue
                text_to_say = (step.get("text") or "").strip()
                if not text_to_say:
                    results.append({"idx": idx, "error": "missing text"})
                    continue
                emotion = (step.get("emotion") or "").strip()
                gain = float(step.get("gain") or 1.0)
                pause_ms = int(step.get("pauseMs") or 0)

                # Emotion first (optional)
                llm_ok = True
                if emotion:
                    llm_ok = publish_down_command(broker, device_id, {"type": "llm", "emotion": emotion})

                # Synthesize TTS to file
                try:
                    loop = asyncio.get_running_loop()
                    tts_file_path = await loop.run_in_executor(None, tts_provider.to_tts, text_to_say)
                except Exception as e:
                    results.append({"idx": idx, "error": f"tts synth failed: {e}"})
                    continue
                if not tts_file_path or not os.path.exists(tts_file_path):
                    results.append({"idx": idx, "error": "tts file not generated"})
                    continue

                # Ensure WAV 16k mono for device playback
                wav_path = ensure_wav_16k_mono(tts_file_path)
                basename = os.path.basename(wav_path)
                tts_url = f"{base_url}/tts/{basename}"

                # Send play_url
                play_ok = publish_down_command(
                    broker,
                    device_id,
                    {"type": "play_url", "url": tts_url, "gain": gain},
                )

                results.append({"idx": idx, "ttsUrl": tts_url, "llm": bool(llm_ok), "play_url": bool(play_ok)})

                # Optional pause
                if pause_ms > 0:
                    try:
                        await asyncio.sleep(pause_ms / 1000.0)
                    except Exception:
                        pass

        return web.json_response({"ok": True, "results": results, "combinedUrl": combined_url})
