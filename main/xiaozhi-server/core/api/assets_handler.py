import json
import aiohttp
from aiohttp import web
from typing import Optional, Dict
from config.logger import setup_logging
from config.config_loader import get_private_config_from_api
from core.utils.auth import AuthToken


TAG = __name__


class AssetsHandler:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.auth = AuthToken(config["server"]["auth_key"])  # 复用现有认证
        self.auth_enabled = bool(config.get("server", {}).get("auth", {}).get("enabled", False))

    def _create_error_response(self, message: str) -> dict:
        return {"success": False, "message": message}

    def _add_cors_headers(self, response: web.StreamResponse):
        response.headers["Access-Control-Allow-Headers"] = (
            "client-id, content-type, device-id, authorization, Authorization"
        )
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Origin"] = "*"

    def _verify_auth_token(self, request) -> tuple[bool, Optional[str]]:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return False, None
        token = auth_header[7:]
        return self.auth.verify_token(token)

    async def handle_manifest(self, request: web.Request) -> web.Response:
        response: Optional[web.Response] = None
        try:
            device_id = request.query.get("device-id", "")
            client_id = request.headers.get("Client-Id", "")
            emotion = request.query.get("emotion", "normal")
            if self.auth_enabled:
                is_valid, token_device_id = self._verify_auth_token(request)
                if not is_valid:
                    response = web.Response(
                        text=json.dumps(self._create_error_response("无效的认证token或token已过期")),
                        content_type="application/json",
                        status=401,
                    )
                    return response
                if not device_id or device_id != token_device_id:
                    raise ValueError("设备ID与token不匹配或缺失")
            else:
                if not device_id:
                    raise ValueError("缺少设备ID(device-id)")

            # 总是从 Firestore 读取设备动画信息
            current_config = self.config
            from config.config_loader import _get_profile_from_firestore
            fs_conf = current_config.get("firestore", {})
            profile = _get_profile_from_firestore(device_id, fs_conf) or {}

            animation = (profile.get("animation") or {}).get(emotion) or {}
            if not animation:
                raise ValueError("未找到对应的动画清单，请先在云端生成并更新 Firestore")

            manifest = {
                "device_id": device_id,
                "emotion": emotion,
                "version": animation.get("version", ""),
                "size": animation.get("size", 0),
                "sha256": animation.get("sha256", ""),
                "fmt": animation.get("fmt", "rgb565"),
                "width": animation.get("width", 256),
                "height": animation.get("height", 256),
                "fps": animation.get("fps", 12),
                "loop": animation.get("loop", 1),
                # 如果 Firestore 中直接保存了 GCS 签名 URL 则直接返回
                # 否则回落到本服务的 bin 代理
                "url": animation.get("url")
                or f"/mcp/assets/bin?device-id={device_id}&v={animation.get('version','')}&emotion={emotion}",
            }

            response = web.Response(
                text=json.dumps(manifest, separators=(",", ":")),
                content_type="application/json",
            )
        except ValueError as e:
            self.logger.bind(tag=TAG).error(f"Assets manifest 请求异常: {e}")
            response = web.Response(
                text=json.dumps(self._create_error_response(str(e)), separators=(",", ":")),
                content_type="application/json",
                status=400,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Assets manifest 请求异常: {e}")
            response = web.Response(
                text=json.dumps(self._create_error_response("服务器内部错误"), separators=(",", ":")),
                content_type="application/json",
                status=500,
            )
        finally:
            if response is not None:
                self._add_cors_headers(response)
            return response

    async def handle_bin(self, request: web.Request) -> web.StreamResponse:
        response: Optional[web.StreamResponse] = None
        try:
            device_id = request.query.get("device-id", "")
            version = request.query.get("v", "")
            emotion = request.query.get("emotion", "normal")
            client_id = request.headers.get("Client-Id", "")
            if self.auth_enabled:
                is_valid, token_device_id = self._verify_auth_token(request)
                if not is_valid:
                    response = web.Response(
                        text=json.dumps(self._create_error_response("无效的认证token或token已过期")),
                        content_type="application/json",
                        status=401,
                    )
                    return response
                if not device_id or device_id != token_device_id:
                    raise ValueError("设备ID与token不匹配或缺失")
            else:
                if not device_id:
                    raise ValueError("缺少设备ID(device-id)")

            # 读取 Firestore 动画信息，拿到 url 或 GCS 路径
            current_config = self.config
            from config.config_loader import _get_profile_from_firestore
            fs_conf = current_config.get("firestore", {})
            profile = _get_profile_from_firestore(device_id, fs_conf) or {}
            animation = (profile.get("animation") or {}).get(emotion) or {}
            if not animation:
                raise ValueError("未找到对应的动画信息")

            url = animation.get("url", "")
            if not url:
                # 如果没有直链，拼接 GCS 公网地址（需要 bucket 配置）
                assets_conf: Dict[str, str] = current_config.get("assets", {})
                bucket = assets_conf.get("bucket", "")
                if not bucket:
                    raise ValueError("未配置 assets.bucket，无法拼接动画地址")
                url = (
                    f"https://storage.googleapis.com/{bucket}/bin/{device_id}/{version}/{emotion}.bin"
                )

            # 作为代理透传 .bin；支持大文件流式
            timeout = aiohttp.ClientTimeout(total=600)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as upstream:
                    if upstream.status != 200:
                        raise ValueError(f"上游返回错误状态码：{upstream.status}")

                    headers = {
                        "Content-Type": upstream.headers.get(
                            "Content-Type", "application/octet-stream"
                        ),
                    }
                    stream_resp = web.StreamResponse(status=200, headers=headers)
                    await stream_resp.prepare(request)

                    async for chunk in upstream.content.iter_chunked(64 * 1024):
                        await stream_resp.write(chunk)

                    await stream_resp.write_eof()
                    response = stream_resp
                    return response
        except ValueError as e:
            self.logger.bind(tag=TAG).error(f"Assets bin 请求异常: {e}")
            response = web.Response(
                text=json.dumps(self._create_error_response(str(e)), separators=(",", ":")),
                content_type="application/json",
                status=400,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Assets bin 请求异常: {e}")
            response = web.Response(
                text=json.dumps(self._create_error_response("服务器内部错误"), separators=(",", ":")),
                content_type="application/json",
                status=500,
            )
        finally:
            if response is not None:
                self._add_cors_headers(response)
            return response


