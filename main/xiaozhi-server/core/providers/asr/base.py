import os
import io
import re
import sys
import math
import wave
import uuid
import json
import time
import shutil
import queue
import array
import asyncio
import traceback
import threading
import opuslib_next
import concurrent.futures
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from config.logger import setup_logging
from typing import Optional, Tuple, List
from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.utils.util import remove_punctuation_and_length
from core.handle.receiveAudioHandle import handleAudioMessage
from services import log_context

TAG = __name__
logger = setup_logging()

try:
    from google.cloud import storage
except Exception:
    storage = None

GCS_AUDIO_RETENTION_MODES = {"gcs", "upload", "cloud"}
PERSIST_AUDIO_RETENTION_MODES = {"archive", "keep"} | GCS_AUDIO_RETENTION_MODES
ASCII_LETTER_PATTERN = re.compile(r"[A-Za-z]")
ASR_SAMPLE_RATE = 16000
ASR_AUDIO_WINDOW_MS = 20
DEFAULT_MIN_ASR_AUDIO_DURATION_MS = 700
DEFAULT_MIN_ASR_AUDIO_RMS_DBFS = -45.0
DEFAULT_MIN_ASR_AUDIO_ACTIVE_MS = 400
DEFAULT_ASR_ACTIVE_AUDIO_DBFS = -45.0


class ASRProviderBase(ABC):
    def __init__(self):
        self.delete_audio_file = True
        self.audio_retention_mode = "delete"
        self.audio_archive_dir = "data/asr_archive"
        self.audio_manifest_file = "manifest.jsonl"
        self.audio_gcs_bucket = ""
        self.audio_gcs_prefix = "asr_audio"
        self.audio_gcs_delete_local = True
        self.reject_non_english_fragments = True
        self.min_asr_audio_duration_ms = DEFAULT_MIN_ASR_AUDIO_DURATION_MS
        self.min_asr_audio_rms_dbfs = DEFAULT_MIN_ASR_AUDIO_RMS_DBFS
        self.min_asr_audio_active_ms = DEFAULT_MIN_ASR_AUDIO_ACTIVE_MS
        self.asr_active_audio_dbfs = DEFAULT_ASR_ACTIVE_AUDIO_DBFS

    def configure_audio_retention(
        self,
        config: dict,
        delete_audio_file: bool,
    ) -> None:
        self.delete_audio_file = delete_audio_file
        self.audio_retention_mode = str(
            config.get(
                "audio_retention_mode",
                "delete" if delete_audio_file else "archive",
            )
        ).strip().lower()
        self.audio_archive_dir = str(
            config.get("audio_archive_dir") or "data/asr_archive"
        ).strip()
        self.audio_manifest_file = str(
            config.get("audio_manifest_file") or "manifest.jsonl"
        ).strip()
        self.audio_gcs_bucket = str(
            config.get("audio_gcs_bucket")
            or os.getenv("BABYMILU_ASR_AUDIO_BUCKET")
            or ""
        ).strip()
        self.audio_gcs_prefix = str(
            config.get("audio_gcs_prefix")
            or os.getenv("BABYMILU_ASR_AUDIO_PREFIX")
            or "asr_audio"
        ).strip().strip("/")
        self.audio_gcs_delete_local = self._as_bool(
            config.get("audio_gcs_delete_local"), True
        )
        self.reject_non_english_fragments = self._as_bool(
            config.get("reject_non_english_fragments"),
            self.reject_non_english_fragments,
        )
        self.min_asr_audio_duration_ms = max(
            0,
            int(
                config.get(
                    "min_asr_audio_duration_ms",
                    self.min_asr_audio_duration_ms,
                )
            ),
        )
        self.min_asr_audio_rms_dbfs = float(
            config.get("min_asr_audio_rms_dbfs", self.min_asr_audio_rms_dbfs)
        )
        self.min_asr_audio_active_ms = max(
            0,
            int(config.get("min_asr_audio_active_ms", self.min_asr_audio_active_ms)),
        )
        self.asr_active_audio_dbfs = float(
            config.get("asr_active_audio_dbfs", self.asr_active_audio_dbfs)
        )

    def should_persist_audio_file(self) -> bool:
        return self.audio_retention_mode in PERSIST_AUDIO_RETENTION_MODES

    def finalize_audio_file(self, file_path: Optional[str], session_id: str) -> Optional[str]:
        if not file_path or not os.path.exists(file_path):
            return None

        mode = self.audio_retention_mode
        if mode == "delete":
            try:
                os.remove(file_path)
                logger.bind(tag=TAG).debug(f"已删除临时音频文件: {file_path}")
            except Exception as e:
                logger.bind(tag=TAG).error(f"文件删除失败: {file_path} | 错误: {e}")
            return None

        if mode == "keep":
            logger.bind(tag=TAG).info(f"保留ASR音频文件: {file_path}")
            return file_path

        if mode in GCS_AUDIO_RETENTION_MODES:
            return self._upload_audio_file_to_gcs(file_path, session_id)

        if mode != "archive":
            logger.bind(tag=TAG).warning(
                f"未知 audio_retention_mode={mode}，保留原文件: {file_path}"
            )
            return file_path

        return self._archive_audio_file(file_path, session_id)

    def _archive_audio_file(self, file_path: str, session_id: str) -> Optional[str]:
        archive_root = os.path.abspath(self.audio_archive_dir or "data/asr_archive")
        date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        archive_dir = os.path.join(archive_root, date_part, self._safe_path_part(session_id))
        os.makedirs(archive_dir, exist_ok=True)
        archived_path = os.path.join(archive_dir, os.path.basename(file_path))
        try:
            shutil.move(file_path, archived_path)
            record = {
                "archivedAt": datetime.now(timezone.utc).isoformat(),
                "sessionId": session_id,
                "sourcePath": file_path,
                "archivedPath": archived_path,
                "sizeBytes": os.path.getsize(archived_path),
            }
            self._append_audio_manifest(record)
            logger.bind(tag=TAG).info(f"已归档ASR音频文件: {archived_path}")
            return archived_path
        except Exception as e:
            logger.bind(tag=TAG).error(f"文件归档失败: {file_path} | 错误: {e}")
            return file_path

    def _upload_audio_file_to_gcs(self, file_path: str, session_id: str) -> Optional[str]:
        if not self.audio_gcs_bucket:
            logger.bind(tag=TAG).warning(
                f"audio_retention_mode={self.audio_retention_mode} 但未配置 audio_gcs_bucket，保留原文件: {file_path}"
            )
            return file_path

        if storage is None:
            logger.bind(tag=TAG).error(
                f"google-cloud-storage 不可用，无法上传ASR音频，保留原文件: {file_path}"
            )
            return file_path

        date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        object_name = self._gcs_object_name(file_path, session_id, date_part)
        gcs_uri = f"gs://{self.audio_gcs_bucket}/{object_name}"
        size_bytes = os.path.getsize(file_path)

        try:
            client = storage.Client()
            bucket = client.bucket(self.audio_gcs_bucket)
            blob = bucket.blob(object_name)
            blob.upload_from_filename(file_path, content_type="audio/wav")
            record = {
                "archivedAt": datetime.now(timezone.utc).isoformat(),
                "sessionId": session_id,
                "sourcePath": file_path,
                "gcsUri": gcs_uri,
                "sizeBytes": size_bytes,
                "localDeleted": False,
            }
            if self.audio_gcs_delete_local:
                os.remove(file_path)
                record["localDeleted"] = True
                logger.bind(tag=TAG).info(f"已上传并删除本地ASR音频文件: {gcs_uri}")
            else:
                logger.bind(tag=TAG).info(f"已上传ASR音频文件并保留本地副本: {gcs_uri}")
            self._append_audio_manifest(record)
            return gcs_uri
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"ASR音频上传GCS失败，保留原文件: {file_path} | 错误: {e}"
            )
            return file_path

    def _append_audio_manifest(self, record: dict) -> None:
        if not self.audio_manifest_file:
            return

        archive_root = os.path.abspath(self.audio_archive_dir or "data/asr_archive")
        manifest_path = os.path.join(archive_root, self.audio_manifest_file)
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        with open(manifest_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _gcs_object_name(self, file_path: str, session_id: str, date_part: str) -> str:
        parts = [
            self.audio_gcs_prefix,
            date_part,
            self._safe_path_part(session_id),
            os.path.basename(file_path),
        ]
        return "/".join(part.strip("/") for part in parts if part)

    @staticmethod
    def _safe_path_part(value: Optional[str]) -> str:
        safe = "".join(
            char if char.isalnum() or char in ("-", "_", ".") else "_"
            for char in str(value or "unknown")
        )
        return safe.strip("._") or "unknown"

    @staticmethod
    def _as_bool(value, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    # 打开音频通道
    async def open_audio_channels(self, conn):
        conn.asr_priority_thread = threading.Thread(
            target=self.asr_text_priority_thread, args=(conn,), daemon=True
        )
        conn.asr_priority_thread.start()

    # 有序处理ASR音频
    def asr_text_priority_thread(self, conn):
        while not conn.stop_event.is_set():
            try:
                message = conn.asr_audio_queue.get(timeout=1)
                future = asyncio.run_coroutine_threadsafe(
                    handleAudioMessage(conn, message),
                    conn.loop,
                )
                future.result()
            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"处理ASR文本失败: {str(e)}, 类型: {type(e).__name__}, 堆栈: {traceback.format_exc()}"
                )
                continue

    # 接收音频
    async def receive_audio(self, conn, audio, audio_have_voice):
        if conn.client_listen_mode == "auto" or conn.client_listen_mode == "realtime":
            have_voice = audio_have_voice
        else:
            have_voice = conn.client_have_voice

        conn.asr_audio.append(audio)
        if not have_voice and not conn.client_have_voice:
            conn.asr_audio = conn.asr_audio[-10:]
            release_inactive = getattr(conn, "release_inactive_vad_lease", None)
            if callable(release_inactive):
                release_inactive()
            return

        if conn.client_voice_stop:
            asr_audio_task = conn.asr_audio.copy()
            conn.asr_audio.clear()
            conn.reset_vad_states()
            release_vad = getattr(conn, "release_vad_lease", None)
            if callable(release_vad):
                release_vad(reset_connection_state=False)

            # Opus packets are handled as ~60 ms frames here, so >3 packets
            # allows forwarding utterances of roughly 0.2 seconds or longer.
            if len(asr_audio_task) > 3:
                await self.handle_voice_stop(conn, asr_audio_task)

    # 处理语音停止
    async def handle_voice_stop(self, conn, asr_audio_task: List[bytes]):
        """并行处理ASR和声纹识别"""
        try:
            total_start_time = time.monotonic()

            # 准备音频数据
            if conn.audio_format == "pcm":
                pcm_data = asr_audio_task
            elif hasattr(conn, "run_sync"):
                pcm_data = await conn.run_sync(
                    "audio",
                    self.decode_opus,
                    asr_audio_task,
                    timeout=getattr(conn, "executor_timeout", lambda _name: 15.0)(
                        "audio"
                    ),
                )
            else:
                pcm_data = await asyncio.to_thread(self.decode_opus, asr_audio_task)

            combined_pcm_data = b"".join(pcm_data)
            audio_allowed, audio_stats, audio_reject_reason = (
                self._should_process_asr_audio(combined_pcm_data)
            )
            if not audio_allowed:
                self.stop_ws_connection()
                persisted_audio = self._persist_rejected_asr_audio(
                    pcm_data,
                    conn.session_id,
                )
                logger.bind(tag=TAG).info(
                    "忽略ASR音频片段: "
                    f"reason={audio_reject_reason} stats={audio_stats} "
                    f"persisted_audio={persisted_audio}"
                )
                return

            # 预先准备WAV数据
            wav_data = None
            if conn.voiceprint_provider and combined_pcm_data:
                if hasattr(conn, "run_sync"):
                    wav_data = await conn.run_sync(
                        "audio",
                        self._pcm_to_wav,
                        combined_pcm_data,
                        timeout=getattr(conn, "executor_timeout", lambda _name: 15.0)(
                            "audio"
                        ),
                    )
                else:
                    wav_data = await asyncio.to_thread(
                        self._pcm_to_wav,
                        combined_pcm_data,
                    )

            # 定义ASR任务
            def run_asr():
                start_time = time.monotonic()
                device_token = log_context.set_device_id(getattr(conn, "device_id", None))
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        result = loop.run_until_complete(
                            self.speech_to_text(asr_audio_task, conn.session_id, conn.audio_format)
                        )
                        end_time = time.monotonic()
                        logger.bind(tag=TAG).info(f"ASR耗时: {end_time - start_time:.3f}s")
                        return result
                    finally:
                        loop.close()
                except Exception as e:
                    end_time = time.monotonic()
                    logger.bind(tag=TAG).error(f"ASR失败: {e}")
                    return ("", None)
                finally:
                    log_context.reset_device_id(device_token)

            # 定义声纹识别任务
            def run_voiceprint():
                if not wav_data:
                    return None
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        # 使用连接的声纹识别提供者
                        result = loop.run_until_complete(
                            conn.voiceprint_provider.identify_speaker(wav_data, conn.session_id)
                        )
                        return result
                    finally:
                        loop.close()
                except Exception as e:
                    logger.bind(tag=TAG).error(f"声纹识别失败: {e}")
                    return None

            # 使用线程池执行器并行运行
            thread_executor = getattr(getattr(conn, "executors", None), "audio", None)
            owns_thread_executor = False
            if thread_executor is None:
                thread_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
                owns_thread_executor = True
            asr_future = None
            voiceprint_future = None
            try:
                asr_future = thread_executor.submit(run_asr)

                if conn.voiceprint_provider and wav_data:
                    voiceprint_future = thread_executor.submit(run_voiceprint)

                    # 等待两个线程都完成
                    asr_result, voiceprint_result = await asyncio.gather(
                        asyncio.wait_for(asyncio.wrap_future(asr_future), timeout=15),
                        asyncio.wait_for(asyncio.wrap_future(voiceprint_future), timeout=15),
                    )

                    results = {"asr": asr_result, "voiceprint": voiceprint_result}
                else:
                    asr_result = await asyncio.wait_for(
                        asyncio.wrap_future(asr_future),
                        timeout=15,
                    )
                    results = {"asr": asr_result, "voiceprint": None}
            except asyncio.TimeoutError:
                logger.bind(tag=TAG).error("ASR/voiceprint recognition timed out")
                if asr_future:
                    asr_future.cancel()
                if voiceprint_future:
                    voiceprint_future.cancel()
                results = {"asr": ("", None), "voiceprint": None}
            finally:
                if owns_thread_executor:
                    thread_executor.shutdown(wait=False, cancel_futures=True)


            # 处理结果
            raw_text, _ = results.get("asr", ("", None))
            speaker_name = results.get("voiceprint", None)

            # 记录识别结果
            if raw_text:
                logger.bind(tag=TAG).info(f"识别文本: {raw_text}")
            if speaker_name:
                logger.bind(tag=TAG).info(f"识别说话人: {speaker_name}")

            # 性能监控
            total_time = time.monotonic() - total_start_time
            logger.bind(tag=TAG).info(f"总处理耗时: {total_time:.3f}s")

            should_forward, filtered_text, reject_reason = (
                self._should_forward_asr_text(raw_text)
            )
            self.stop_ws_connection()

            if should_forward:
                # 构建包含说话人信息的JSON字符串
                enhanced_text = self._build_enhanced_text(raw_text, speaker_name)

                # 使用自定义模块进行上报
                await startToChat(conn, enhanced_text)
                enqueue_asr_report(conn, enhanced_text, asr_audio_task)
            elif raw_text:
                logger.bind(tag=TAG).info(
                    "忽略ASR识别片段: "
                    f"reason={reject_reason} filtered={filtered_text!r} raw={raw_text!r}"
                )

        except Exception as e:
            logger.bind(tag=TAG).error(f"处理语音停止失败: {e}")
            import traceback
            logger.bind(tag=TAG).debug(f"异常详情: {traceback.format_exc()}")

    def _build_enhanced_text(self, text: str, speaker_name: Optional[str]) -> str:
        """构建包含说话人信息的文本"""
        if speaker_name and speaker_name.strip():
            return json.dumps({
                "speaker": speaker_name,
                "content": text
            }, ensure_ascii=False)
        else:
            return text

    def _should_forward_asr_text(self, raw_text: Optional[str]) -> tuple[bool, str, str]:
        text_len, filtered_text = remove_punctuation_and_length(raw_text or "")
        filtered_text = (filtered_text or "").strip()
        if text_len <= 0 or not filtered_text:
            return False, filtered_text, "empty_after_punctuation"

        if len(filtered_text) == 1:
            return False, filtered_text, "single_character_fragment"

        if (
            self.reject_non_english_fragments
            and not ASCII_LETTER_PATTERN.search(filtered_text)
        ):
            return False, filtered_text, "no_ascii_letters"

        return True, filtered_text, "ok"

    def _should_process_asr_audio(self, pcm_bytes: bytes) -> tuple[bool, dict, str]:
        stats = self._pcm_audio_stats(pcm_bytes)
        duration_ms = stats["duration_ms"]
        rms_dbfs = stats["rms_dbfs"]
        active_ms = stats["active_ms"]

        if (
            self.min_asr_audio_duration_ms > 0
            and duration_ms < self.min_asr_audio_duration_ms
        ):
            return False, stats, "too_short"

        if rms_dbfs is None:
            return False, stats, "empty_audio"

        if (
            self.min_asr_audio_rms_dbfs > -120
            and rms_dbfs < self.min_asr_audio_rms_dbfs
        ):
            return False, stats, "too_quiet"

        if self.min_asr_audio_active_ms > 0 and active_ms < self.min_asr_audio_active_ms:
            return False, stats, "too_little_active_audio"

        return True, stats, "ok"

    def _pcm_audio_stats(self, pcm_bytes: bytes) -> dict:
        sample_count = len(pcm_bytes or b"") // 2
        if sample_count <= 0:
            return {
                "duration_ms": 0,
                "rms_dbfs": None,
                "peak_dbfs": None,
                "active_ms": 0,
            }

        samples = array.array("h")
        samples.frombytes(pcm_bytes[: sample_count * 2])
        if sys.byteorder != "little":
            samples.byteswap()

        values = [int(sample) for sample in samples]
        rms = math.sqrt(sum(sample * sample for sample in values) / len(values))
        peak = max(abs(sample) for sample in values)
        duration_ms = int(round(len(values) / ASR_SAMPLE_RATE * 1000))
        rms_dbfs = 20 * math.log10(rms / 32768) if rms > 0 else -120.0
        peak_dbfs = 20 * math.log10(peak / 32768) if peak > 0 else -120.0

        window_samples = max(1, int(ASR_SAMPLE_RATE * ASR_AUDIO_WINDOW_MS / 1000))
        active_threshold = 32768 * (10 ** (self.asr_active_audio_dbfs / 20))
        active_ms = 0
        for start in range(0, len(values), window_samples):
            window = values[start : start + window_samples]
            if not window:
                continue
            window_rms = math.sqrt(
                sum(sample * sample for sample in window) / len(window)
            )
            if window_rms >= active_threshold:
                active_ms += int(round(len(window) / ASR_SAMPLE_RATE * 1000))

        return {
            "duration_ms": duration_ms,
            "rms_dbfs": round(rms_dbfs, 1),
            "peak_dbfs": round(peak_dbfs, 1),
            "active_ms": active_ms,
        }

    def _persist_rejected_asr_audio(
        self,
        pcm_data: List[bytes],
        session_id: str,
    ) -> Optional[str]:
        if not self.should_persist_audio_file():
            return None

        file_path = None
        try:
            file_path = self.save_audio_to_file(pcm_data, session_id)
            return self.finalize_audio_file(file_path, session_id)
        except Exception as e:
            logger.bind(tag=TAG).error(
                f"忽略的ASR音频片段保留失败: {file_path} | 错误: {e}"
            )
            return file_path

    def _pcm_to_wav(self, pcm_data: bytes) -> bytes:
        """将PCM数据转换为WAV格式"""
        if len(pcm_data) == 0:
            logger.bind(tag=TAG).warning("PCM数据为空，无法转换WAV")
            return b""

        # 确保数据长度是偶数（16位音频）
        if len(pcm_data) % 2 != 0:
            pcm_data = pcm_data[:-1]

        # 创建WAV文件头
        wav_buffer = io.BytesIO()
        try:
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)      # 单声道
                wav_file.setsampwidth(2)      # 16位
                wav_file.setframerate(16000)  # 16kHz采样率
                wav_file.writeframes(pcm_data)

            wav_buffer.seek(0)
            wav_data = wav_buffer.read()

            return wav_data
        except Exception as e:
            logger.bind(tag=TAG).error(f"WAV转换失败: {e}")
            return b""

    def stop_ws_connection(self):
        pass

    def save_audio_to_file(self, pcm_data: List[bytes], session_id: str) -> str:
        """PCM数据保存为WAV文件"""
        module_name = __name__.split(".")[-1]
        file_name = f"asr_{module_name}_{session_id}_{uuid.uuid4()}.wav"
        file_path = os.path.join(self.output_dir, file_name)

        with wave.open(file_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 2 bytes = 16-bit
            wf.setframerate(16000)
            wf.writeframes(b"".join(pcm_data))

        return file_path

    @abstractmethod
    async def speech_to_text(
        self, opus_data: List[bytes], session_id: str, audio_format="opus"
    ) -> Tuple[Optional[str], Optional[str]]:
        """将语音数据转换为文本"""
        pass

    @staticmethod
    def decode_opus(opus_data: List[bytes]) -> List[bytes]:
        """将Opus音频数据解码为PCM数据"""
        try:
            decoder = opuslib_next.Decoder(16000, 1)
            pcm_data = []
            buffer_size = 960  # 每次处理960个采样点 (60ms at 16kHz)

            for i, opus_packet in enumerate(opus_data):
                try:
                    if not opus_packet or len(opus_packet) == 0:
                        continue

                    pcm_frame = decoder.decode(opus_packet, buffer_size)
                    if pcm_frame and len(pcm_frame) > 0:
                        pcm_data.append(pcm_frame)

                except opuslib_next.OpusError as e:
                    logger.bind(tag=TAG).warning(f"Opus解码错误，跳过数据包 {i}: {e}")
                except Exception as e:
                    logger.bind(tag=TAG).error(f"音频处理错误，数据包 {i}: {e}")

            return pcm_data

        except Exception as e:
            logger.bind(tag=TAG).error(f"音频解码过程发生错误: {e}")
            return []
