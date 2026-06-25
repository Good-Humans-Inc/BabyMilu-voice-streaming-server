import os
import io
import re
import wave
import uuid
import json
import time
import shutil
import queue
import asyncio
import traceback
import threading
import unicodedata
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
WORD_PATTERN = re.compile(r"[a-z]+")
DEFAULT_LOW_SIGNAL_ASR_FRAGMENTS = {
    "ah",
    "eh",
    "er",
    "hm",
    "hmm",
    "hmmm",
    "oh",
    "uh",
    "uhh",
    "um",
    "umm",
    "you",
    "empty",
}
DEFAULT_AMBIGUOUS_SHORT_ASR_FRAGMENTS = {
    "a",
    "actually",
    "an",
    "and",
    "are",
    "basically",
    "because",
    "but",
    "for",
    "from",
    "i mean",
    "i have",
    "i said",
    "i was",
    "if",
    "in",
    "is",
    "it",
    "it was",
    "like",
    "of",
    "on",
    "or",
    "so",
    "that",
    "the",
    "then",
    "there",
    "there are",
    "there is",
    "this",
    "to",
    "was",
    "we were",
    "well",
    "were",
    "with",
    "you know",
}
DEFAULT_NON_ENGLISH_MARKER_GROUPS = {
    "french": {
        "bonjour",
        "comment",
        "fatigue",
        "je",
        "merci",
        "pourquoi",
        "quoi",
        "suis",
        "tres",
        "vous",
    },
    "german": {
        "aber",
        "bin",
        "bitte",
        "danke",
        "guten",
        "hallo",
        "ich",
        "morgen",
        "mude",
        "nicht",
        "und",
    },
    "spanish": {
        "buenas",
        "buenos",
        "como",
        "dias",
        "estas",
        "estoy",
        "gracias",
        "hola",
        "muy",
        "pero",
        "porque",
        "quiero",
    },
    "nordic": {
        "det",
        "hva",
        "hvor",
        "ikke",
        "jeg",
        "ma",
        "med",
        "og",
        "se",
        "skal",
        "takk",
    },
}
INCOMPLETE_ENDING_WORDS = {
    "a",
    "an",
    "the",
}
UNCLEAR_ASR_PROMPT_REASONS = {
    "single_character_fragment",
    "no_ascii_letters",
    "non_english_characters",
    "detected_non_english",
    "low_signal_fragment",
    "ambiguous_short_fragment",
    "incomplete_fragment",
}


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
        self.reject_low_signal_fragments = True
        self.reject_ambiguous_short_fragments = True
        self.reject_incomplete_fragments = True
        self.low_signal_fragment_max_audio_seconds = 1.2
        self.ambiguous_short_fragment_max_audio_seconds = 0.7
        self.low_signal_fragments = set(DEFAULT_LOW_SIGNAL_ASR_FRAGMENTS)
        self.ambiguous_short_fragments = {
            self._normalize_short_fragment_key(item)
            for item in DEFAULT_AMBIGUOUS_SHORT_ASR_FRAGMENTS
        }
        self.non_english_marker_groups = {
            language: set(markers)
            for language, markers in DEFAULT_NON_ENGLISH_MARKER_GROUPS.items()
        }
        self.speak_on_unclear_asr = True
        self.unclear_asr_prompt = "I didn't catch that clearly. Can you say it again?"
        self.unclear_asr_prompt_cooldown_seconds = 4.0

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
        self.reject_low_signal_fragments = self._as_bool(
            config.get("reject_low_signal_fragments"),
            self.reject_low_signal_fragments,
        )
        self.reject_ambiguous_short_fragments = self._as_bool(
            config.get("reject_ambiguous_short_fragments"),
            self.reject_ambiguous_short_fragments,
        )
        self.reject_incomplete_fragments = self._as_bool(
            config.get("reject_incomplete_fragments"),
            self.reject_incomplete_fragments,
        )
        self.low_signal_fragment_max_audio_seconds = self._as_float(
            config.get("low_signal_fragment_max_audio_seconds"),
            self.low_signal_fragment_max_audio_seconds,
        )
        self.ambiguous_short_fragment_max_audio_seconds = self._as_float(
            config.get("ambiguous_short_fragment_max_audio_seconds"),
            self.ambiguous_short_fragment_max_audio_seconds,
        )
        configured_low_signal = config.get("low_signal_fragments")
        if isinstance(configured_low_signal, str):
            configured_low_signal = [
                item.strip()
                for item in configured_low_signal.split(",")
                if item.strip()
            ]
        if isinstance(configured_low_signal, (list, tuple, set)):
            self.low_signal_fragments = {
                str(item).strip().casefold()
                for item in configured_low_signal
                if str(item).strip()
            }
        configured_ambiguous_short = config.get("ambiguous_short_fragments")
        if isinstance(configured_ambiguous_short, str):
            configured_ambiguous_short = [
                item.strip()
                for item in configured_ambiguous_short.split(",")
                if item.strip()
            ]
        if isinstance(configured_ambiguous_short, (list, tuple, set)):
            self.ambiguous_short_fragments = {
                self._normalize_short_fragment_key(item)
                for item in configured_ambiguous_short
                if str(item).strip()
            }
        configured_non_english = config.get("non_english_marker_groups")
        if isinstance(configured_non_english, dict):
            marker_groups = {}
            for language, markers in configured_non_english.items():
                if isinstance(markers, str):
                    markers = [
                        item.strip()
                        for item in markers.split(",")
                        if item.strip()
                    ]
                if not isinstance(markers, (list, tuple, set)):
                    continue
                normalized_markers = {
                    self._normalize_language_marker(item)
                    for item in markers
                    if str(item).strip()
                }
                normalized_markers.discard("")
                if normalized_markers:
                    marker_groups[str(language)] = normalized_markers
            if marker_groups:
                self.non_english_marker_groups = marker_groups
        self.speak_on_unclear_asr = self._as_bool(
            config.get("speak_on_unclear_asr"),
            self.speak_on_unclear_asr,
        )
        self.unclear_asr_prompt = str(
            config.get("unclear_asr_prompt") or self.unclear_asr_prompt
        ).strip()
        self.unclear_asr_prompt_cooldown_seconds = self._as_float(
            config.get("unclear_asr_prompt_cooldown_seconds"),
            self.unclear_asr_prompt_cooldown_seconds,
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

    @staticmethod
    def _as_float(value, default: float) -> float:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

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

            audio_duration_seconds = self._estimate_audio_duration_seconds(
                combined_pcm_data,
                asr_audio_task,
            )
            should_forward, filtered_text, reject_reason = (
                self._should_forward_asr_text(
                    raw_text,
                    audio_duration_seconds=audio_duration_seconds,
                )
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
                await self._maybe_speak_unclear_asr_prompt(conn, reject_reason)

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

    def _should_forward_asr_text(
        self,
        raw_text: Optional[str],
        *,
        audio_duration_seconds: Optional[float] = None,
    ) -> tuple[bool, str, str]:
        raw_text_value = raw_text or ""
        text_len, filtered_text = remove_punctuation_and_length(raw_text_value)
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

        if (
            self.reject_non_english_fragments
            and self._has_non_ascii_letter(raw_text_value)
        ):
            return False, filtered_text, "non_english_characters"

        if (
            self.reject_non_english_fragments
            and self._is_detected_non_english_text(raw_text_value)
        ):
            return False, filtered_text, "detected_non_english"

        if self._is_low_signal_asr_fragment(
            filtered_text,
            audio_duration_seconds=audio_duration_seconds,
        ):
            return False, filtered_text, "low_signal_fragment"

        if self._is_ambiguous_short_asr_fragment(
            filtered_text,
            audio_duration_seconds=audio_duration_seconds,
        ):
            return False, filtered_text, "ambiguous_short_fragment"

        if self._is_incomplete_asr_fragment(raw_text_value):
            return False, filtered_text, "incomplete_fragment"

        return True, filtered_text, "ok"

    def _is_low_signal_asr_fragment(
        self,
        filtered_text: str,
        *,
        audio_duration_seconds: Optional[float],
    ) -> bool:
        if not self.reject_low_signal_fragments:
            return False
        normalized = " ".join((filtered_text or "").casefold().split())
        if normalized not in self.low_signal_fragments:
            return False
        if audio_duration_seconds is None:
            return True
        return audio_duration_seconds <= self.low_signal_fragment_max_audio_seconds

    def _is_ambiguous_short_asr_fragment(
        self,
        filtered_text: str,
        *,
        audio_duration_seconds: Optional[float],
    ) -> bool:
        if not self.reject_ambiguous_short_fragments:
            return False
        if audio_duration_seconds is None:
            return False
        normalized = self._normalize_short_fragment_key(filtered_text)
        if normalized not in self.ambiguous_short_fragments:
            return False
        return audio_duration_seconds <= self.ambiguous_short_fragment_max_audio_seconds

    @staticmethod
    def _normalize_short_fragment_key(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(text or "").casefold())

    def _is_detected_non_english_text(self, text: str) -> bool:
        tokens = self._language_tokens(text)
        if len(tokens) < 2:
            return False
        token_set = set(tokens)
        for markers in self.non_english_marker_groups.values():
            if len(token_set & markers) >= 2:
                return True
        return False

    def _is_incomplete_asr_fragment(self, text: str) -> bool:
        if not self.reject_incomplete_fragments:
            return False

        stripped = str(text or "").strip()
        if not stripped or stripped.endswith("?"):
            return False

        tokens = self._language_tokens(stripped)
        if not tokens:
            return False

        if tokens[-1] in INCOMPLETE_ENDING_WORDS:
            return True

        return False

    @classmethod
    def _language_tokens(cls, text: str) -> list[str]:
        normalized = unicodedata.normalize("NFKD", str(text or ""))
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        return WORD_PATTERN.findall(ascii_text.casefold())

    @classmethod
    def _normalize_language_marker(cls, text: str) -> str:
        tokens = cls._language_tokens(str(text or ""))
        return tokens[0] if tokens else ""

    @staticmethod
    def _has_non_ascii_letter(text: str) -> bool:
        return any(ord(char) > 127 and char.isalpha() for char in text or "")

    @staticmethod
    def _estimate_audio_duration_seconds(
        combined_pcm_data: bytes,
        asr_audio_task: List[bytes],
    ) -> Optional[float]:
        if combined_pcm_data:
            return len(combined_pcm_data) / float(16000 * 2)
        if asr_audio_task:
            return len(asr_audio_task) * 0.06
        return None

    async def _maybe_speak_unclear_asr_prompt(self, conn, reject_reason: str) -> None:
        if (
            not self.speak_on_unclear_asr
            or not self.unclear_asr_prompt
            or reject_reason not in UNCLEAR_ASR_PROMPT_REASONS
        ):
            return

        if not getattr(conn, "llm_finish_task", True) or getattr(
            conn, "client_is_speaking", False
        ):
            logger.bind(tag=TAG).info(
                "Skipping unclear ASR prompt while response is active: "
                f"reason={reject_reason} "
                f"llm_finish_task={getattr(conn, 'llm_finish_task', None)} "
                f"client_is_speaking={getattr(conn, 'client_is_speaking', None)}"
            )
            return

        now = time.monotonic()
        last_prompt_at = float(getattr(conn, "_last_unclear_asr_prompt_at", 0.0) or 0.0)
        if now - last_prompt_at < self.unclear_asr_prompt_cooldown_seconds:
            return
        setattr(conn, "_last_unclear_asr_prompt_at", now)

        try:
            from core.providers.tts.dto.dto import (
                ContentType,
                SentenceType,
                TTSMessageDTO,
            )

            sentence_id = str(uuid.uuid4().hex)
            conn.sentence_id = sentence_id
            conn.active_tts_sentence_id = sentence_id
            conn.tts_MessageText = self.unclear_asr_prompt

            tts = getattr(conn, "tts", None)
            if tts is None:
                return
            queue = getattr(tts, "tts_text_queue", None)
            if queue is not None:
                queue.put(
                    TTSMessageDTO(
                        sentence_id=sentence_id,
                        sentence_type=SentenceType.FIRST,
                        content_type=ContentType.ACTION,
                    )
                )
            tts.tts_one_sentence(
                conn,
                ContentType.TEXT,
                content_detail=self.unclear_asr_prompt,
                sentence_id=sentence_id,
            )
            if queue is not None:
                queue.put(
                    TTSMessageDTO(
                        sentence_id=sentence_id,
                        sentence_type=SentenceType.LAST,
                        content_type=ContentType.ACTION,
                    )
                )
        except Exception as exc:
            logger.bind(tag=TAG).warning(
                f"Failed to speak unclear ASR prompt: {exc}"
            )

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
