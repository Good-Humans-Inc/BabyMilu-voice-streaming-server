import json

from core.handle.textMessageHandlerRegistry import TextMessageHandlerRegistry

TAG = __name__


class TextMessageProcessor:
    """消息处理器主类"""

    def __init__(self, registry: TextMessageHandlerRegistry):
        self.registry = registry

    async def process_message(self, conn, message: str) -> None:
        """处理消息的主入口"""
        try:
            # 解析JSON消息
            msg_json = json.loads(message)

            # 处理JSON消息
            if isinstance(msg_json, dict):
                message_type = str(msg_json.get("type", "")).lower()

                # 记录日志
                conn.logger.bind(tag=TAG).info(f"收到{message_type}消息：{message}")

                # 获取并执行处理器
                handler = self.registry.get_handler(message_type)
                if handler:
                    await handler.handle(conn, msg_json)
                else:
                    # 动态补注册：当服务未正确加载新处理器时，尝试按需加载
                    try:
                        if message_type == "voice":
                            from core.handle.textHandler.voiceMessageHandler import (
                                VoiceTextMessageHandler,
                            )
                            self.registry.register_handler(VoiceTextMessageHandler())
                            handler = self.registry.get_handler(message_type)
                            if handler:
                                await handler.handle(conn, msg_json)
                                return
                    except Exception as e:
                        conn.logger.bind(tag=TAG).error(
                            f"动态加载voice处理器失败: {type(e).__name__}: {e}"
                        )
                    # 记录当前已注册的消息类型，便于排查
                    try:
                        supported = self.registry.get_supported_types()
                        conn.logger.bind(tag=TAG).error(
                            f"未知类型: {message_type}，已注册类型: {supported}"
                        )
                    except Exception:
                        pass
                    conn.logger.bind(tag=TAG).error(
                        f"收到未知类型消息：{message}"
                    )
            # 处理纯数字消息
            elif isinstance(msg_json, int):
                conn.logger.bind(tag=TAG).info(f"收到数字消息：{message}")
                await conn.websocket.send(message)

        except json.JSONDecodeError:
            # 非JSON消息直接转发
            conn.logger.bind(tag=TAG).error(f"解析到错误的消息：{message}")
            await conn.websocket.send(message)
