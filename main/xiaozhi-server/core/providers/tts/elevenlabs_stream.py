import os
import uuid
import json
import time
import queue
import asyncio
import base64
import traceback

import websockets
from config.logger import setup_logging

from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, ContentType, InterfaceType
from core.utils import opus_encoder_utils
from core.utils.tts import MarkdownCleaner

TAG = __name__
logger = setup_logging()


class TTSProvider(TTSProviderBase):
    """
    ElevenLabs WebSocket streaming TTS provider.

    Pattern:
      - `tts_text_priority_thread` consumes TTSMessageDTOs from `tts_text_queue`
        and sends text over a long-lived WebSocket session.
      - `_start_monitor_tts_response` listens for audio chunks, decodes
        base64-encoded PCM, converts to Opus frames, and pushes them via
        `handle_opus` into `tts_audio_queue`.
    """

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)

        self.interface_type = InterfaceType.DUAL_STREAM
        # 基础配置
        self.api_key = config.get("xi-api-key")
        if not self.api_key:
            raise ValueError("xi-api-key is required in config for ElevenLabs TTS")

        # WebSocket配置
        self.ws = None
        self._monitor_task = None
        self.last_active_time = None

        # 模型和音色配置
        self.model_id = config.get("model_id", "eleven_multilingual_v2")
        self.default_voice_id = config.get("default_voice_id")
        
        # Use pcm_16000 so we can reuse the existing Opus pipeline
        self.output_format = config.get("output_format", "pcm_16000")

        # Optional voice & generation config
        self.voice_settings_dict = config.get("voice_settings") or {}
        self.generation_config = config.get("generation_config") or {}
        # 文本缓冲，用于避免将半个单词拆成多个片段发送
        self.pending_text = ""

        # PCM(16k) -> Opus encoder, same as other streaming providers
        self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
            sample_rate=16000, channels=1, frame_size_ms=60
        )


    def tts_text_priority_thread(self):
        """流式TTS文本处理线程"""
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                logger.bind(tag=TAG).debug(
                    f"收到TTS任务｜{message.sentence_type.name}｜{message.content_type.name}"
                )

                if message.sentence_type == SentenceType.FIRST:
                    # Reset abort flag at start of a turn
                    self.conn.client_abort = False
                    # 清空上一轮残留文本缓冲
                    self.pending_text = ""

                # Global abort
                if self.conn.client_abort:
                    logger.bind(tag=TAG).info("收到打断信息，终止TTS文本处理线程")
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self.finish_session(
                                getattr(self.conn, "sentence_id", None)
                            ),
                            loop=self.conn.loop,
                        )
                        future.result()
                    except Exception as e:
                        logger.bind(tag=TAG).warning(
                            f"打断时关闭ElevenLabs会话失败: {e}"
                        )
                    # 打断时清空文本缓冲
                    self.pending_text = ""
                    continue

                if message.sentence_type == SentenceType.FIRST:
                    # 初始化会话
                    try:
                        if not getattr(self.conn, "sentence_id", None):
                            self.conn.sentence_id = uuid.uuid4().hex
                            logger.bind(tag=TAG).info(
                                f"自动生成新的 会话ID: {self.conn.sentence_id}"
                            )

                        logger.bind(tag=TAG).info("开始启动ElevenLabs TTS会话...")
                        future = asyncio.run_coroutine_threadsafe(
                            self.start_session(self.conn.sentence_id),
                            loop=self.conn.loop,
                        )
                        future.result()
                        self.before_stop_play_files.clear()
                        self.tts_audio_first_sentence = True
                        logger.bind(tag=TAG).info("ElevenLabs TTS会话启动成功")
                    except Exception as e:
                        logger.bind(tag=TAG).error(
                            f"启动ElevenLabs TTS会话失败: {str(e)}"
                        )
                        continue

                elif ContentType.TEXT == message.content_type:
                    if message.content_detail:
                        # 将LLM增量片段追加到缓冲，避免半个单词拆分发送
                        self.pending_text += message.content_detail

                        # 查找最后一个“安全”分割点（空格、换行或常见标点）
                        safe_chars = ["\n", "\t", ".", "!", "?", "！", "？"]
                        last_idx = -1
                        for ch in safe_chars:
                            idx = self.pending_text.rfind(ch)
                            if idx > last_idx:
                                last_idx = idx

                        # 如果找到安全边界，发送该边界之前的内容
                        if last_idx != -1:
                            to_send = self.pending_text[: last_idx + 1]
                            self.pending_text = self.pending_text[last_idx + 1 :]

                            segment = to_send.strip()
                            if segment:
                                try:
                                    logger.bind(tag=TAG).info(
                                        f"开始发送TTS文本: {to_send}"
                                    )
                                    future = asyncio.run_coroutine_threadsafe(
                                        self.text_to_speak(to_send, None),
                                        loop=self.conn.loop,
                                    )
                                    future.result()
                                    logger.bind(tag=TAG).debug("TTS文本发送成功")
                                except Exception as e:
                                    logger.bind(tag=TAG).error(
                                        f"发送TTS文本失败: {str(e)}"
                                    )
                                    continue

                elif ContentType.FILE == message.content_type:
                    logger.bind(tag=TAG).info(
                        f"添加音频文件到待播放列表: {message.content_file}"
                    )
                    if message.content_file and os.path.exists(message.content_file):
                        # 先处理文件音频数据
                        self._process_audio_file_stream(message.content_file, callback=lambda audio_data: self.handle_audio_file(audio_data, message.content_detail))

                if message.sentence_type == SentenceType.LAST:
                    try:
                        # flush 剩余缓冲文本（即使没有安全边界，也要让最后几字符被合成）
                        if self.pending_text.strip():
                            try:
                                logger.bind(tag=TAG).info(
                                    f"开始发送TTS文本(收尾): {self.pending_text}"
                                )
                                future = asyncio.run_coroutine_threadsafe(
                                    self.text_to_speak(self.pending_text, None),
                                    loop=self.conn.loop,
                                )
                                future.result()
                                logger.bind(tag=TAG).debug("TTS文本发送成功(收尾)")
                            except Exception as e:
                                logger.bind(tag=TAG).error(
                                    f"发送TTS收尾文本失败: {str(e)}"
                                )
                            finally:
                                self.pending_text = ""

                        logger.bind(tag=TAG).info("开始结束TTS会话...")
                        future = asyncio.run_coroutine_threadsafe(
                            self.finish_session(self.conn.sentence_id),
                            loop=self.conn.loop,
                        )
                        future.result()
                    except Exception as e:
                        logger.bind(tag=TAG).error(f"结束TTS会话失败: {str(e)}")
                        continue

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败: {str(e)}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )
                continue

    async def text_to_speak(self, text, _):
        """发送文本到TTS服务进行合成"""
        try:
            if self.ws is None:
                logger.bind(tag=TAG).warning("WebSocket连接不存在，终止发送文本")
                return

            # 过滤Markdown
            filtered_text = MarkdownCleaner.clean_markdown(text)
            payload = {
                "text": filtered_text,
                "try_trigger_generation": True,
            }
            await self.ws.send(json.dumps(payload))
            self.last_active_time = time.time()
            logger.bind(tag=TAG).debug(f"已发送文本: {filtered_text}")

        except Exception as e:
            logger.bind(tag=TAG).error(f"发送TTS文本失败: {str(e)}")
            await self.close()
            raise

    async def start_session(self, session_id):
        """启动ElevenLabs WebSocket TTS会话"""
        logger.bind(tag=TAG).info(f"开始ElevenLabs会话～～{session_id}")
        try:
            # Resolve voice_id: prefer connection value, then default from config
            voice_id = None
            if self.conn and getattr(self.conn, "voice_id", None):
                voice_id = self.conn.voice_id
            elif getattr(self, "default_voice_id", None):
                voice_id = str(self.default_voice_id)

            if not voice_id:
                logger.bind(tag=TAG).error(
                    "No voice_id resolved (conn/default). Abort ElevenLabs WS session"
                )
                raise Exception("No voice_id resolved; cannot open ElevenLabs WS")

            # Build WS URI with model and output format
            params = [f"model_id={self.model_id}"]
            if self.output_format:
                params.append(f"output_format={self.output_format}")
            query = "&".join(params)

            # Use fixed ElevenLabs WebSocket endpoint as requested
            ws_url = (
                f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?{query}"
            )

            # Close any previous session if still open
            if self.ws:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None

            logger.bind(tag=TAG).info(f"连接ElevenLabs WebSocket: {ws_url}")
            self.ws = await websockets.connect(
                ws_url,
                additional_headers={"xi-api-key": self.api_key},
                ping_interval=30,
                ping_timeout=10,
                close_timeout=10,
            )
            self.last_active_time = time.time()

            # 初始化连接（设置voice_settings等）
            init_payload: dict[str, object] = {
                "text": " ",
                "xi_api_key": self.api_key,
            }
            if self.voice_settings_dict:
                init_payload["voice_settings"] = self.voice_settings_dict
            if self.generation_config:
                init_payload["generation_config"] = self.generation_config

            await self.ws.send(json.dumps(init_payload))
            logger.bind(tag=TAG).info("ElevenLabs 初始化消息已发送")

            # 启动监听任务
            self._monitor_task = asyncio.create_task(self._start_monitor_tts_response())

        except Exception as e:
            logger.bind(tag=TAG).error(f"启动ElevenLabs会话失败: {str(e)}")
            await self.close()
            raise

    async def finish_session(self, session_id):
        """结束ElevenLabs WebSocket TTS会话"""
        logger.bind(tag=TAG).info(f"关闭ElevenLabs会话～～{session_id}")
        try:
            if self.ws:
                # Per docs: send empty text to flush & close sequence
                try:
                    await self.ws.send(json.dumps({"text": ""}))
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"发送关闭消息失败: {e}")

                # 等待监听任务完成
                if self._monitor_task:
                    try:
                        await self._monitor_task
                    except Exception as e:
                        logger.bind(tag=TAG).error(
                            f"等待ElevenLabs监听任务完成时发生错误: {str(e)}"
                        )
                    finally:
                        self._monitor_task = None

                # 关闭连接
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None

        except Exception as e:
            logger.bind(tag=TAG).error(f"关闭ElevenLabs会话失败: {str(e)}")
            await self.close()
            raise

    async def close(self):
        """清理ElevenLabs WebSocket资源"""
        # Cancel monitor task
        if self._monitor_task:
            try:
                self._monitor_task.cancel()
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.bind(tag=TAG).warning(
                    f"关闭时取消ElevenLabs监听任务错误: {e}"
                )
            self._monitor_task = None

        # Close WebSocket connection
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
            self.last_active_time = None

    async def _start_monitor_tts_response(self):
        """监听TTS响应"""
        try:
            session_finished = False
            while not self.conn.stop_event.is_set():
                try:
                    if not self.ws:
                        break

                    msg = await self.ws.recv()
                    self.last_active_time = time.time()

                    # 检查客户端是否中止
                    if self.conn.client_abort:
                        logger.bind(tag=TAG).info("收到打断信息，终止监听TTS响应")
                        break

                    # All messages are JSON text
                    if isinstance(msg, (str, bytes, bytearray)):
                        if not isinstance(msg, str):
                            msg = msg.decode("utf-8", errors="ignore")
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            logger.bind(tag=TAG).warning(
                                f"收到无效的JSON消息: {msg[:100]}"
                            )
                            continue

                        audio_b64 = data.get("audio")
                        if audio_b64:
                            # FIRST marker: 当第一次收到音频时，使用聚合后的LLM文本（如有）
                            if self.tts_audio_first_sentence:
                                first_text = getattr(self.conn, "tts_MessageText", None)
                                self.tts_audio_queue.put(
                                    (SentenceType.FIRST, [], first_text)
                                )
                                self.tts_audio_first_sentence = False

                            try:
                                pcm_bytes = base64.b64decode(audio_b64)
                            except Exception as e:
                                logger.bind(tag=TAG).warning(
                                    f"解码ElevenLabs音频失败: {e}"
                                )
                                continue

                            # PCM(16k) → Opus → handle_opus
                            self.opus_encoder.encode_pcm_to_opus_stream(
                                pcm_bytes, False, callback=self.handle_opus
                            )

                        # isFinal indicates end of this generation
                        if data.get("isFinal"):
                            # 在TTS完成时打印整段LLM输出（如果已聚合）
                            final_text = getattr(self.conn, "tts_MessageText", None)
                            if final_text:
                                logger.bind(tag=TAG).info(
                                    f"句子语音生成成功： {final_text}"
                                )
                            logger.bind(tag=TAG).debug("ElevenLabs TTS任务完成~")
                            # 推送LAST标记，通知播放线程结束本轮TTS
                            self.tts_audio_queue.put(
                                (SentenceType.LAST, [], final_text)
                            )
                            session_finished = True
                            break

                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).warning("WebSocket连接已关闭")
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(
                        f"处理TTS响应时出错: {e}\n{traceback.format_exc()}"
                    )
                    break

        finally:
            if self.ws and not session_finished:
                try:
                    await self.ws.close()
                except Exception:
                    pass
                self.ws = None