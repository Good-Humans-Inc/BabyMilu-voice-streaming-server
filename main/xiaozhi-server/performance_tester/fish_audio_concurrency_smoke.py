import argparse
import asyncio
import json
import queue
import statistics
import threading
import time
import traceback
from types import SimpleNamespace
from typing import Any, Dict, List

from config.settings import load_config
from core.providers.tts.dto.dto import SentenceType
from core.providers.tts.fish_audio import TTSProvider, close_shared_resources


DEFAULT_PROMPTS = [
    ("Ava", "Hi, I'm Ava. Give me a short cheerful greeting in character."),
    ("Ben", "Hi, I'm Ben. Say a playful hello and ask how my day is going."),
    ("Chloe", "Hi, I'm Chloe. Give me a warm intro in two short sentences."),
    ("Daniel", "Hi, I'm Daniel. Say a quick hello with a confident tone."),
    ("Ella", "Hi, I'm Ella. Give me a short funny greeting in character."),
    ("Felix", "Hi, I'm Felix. Introduce yourself with a little attitude."),
    ("Grace", "Hi, I'm Grace. Say hello and ask if the audio sounds clear."),
    ("Hugo", "Hi, I'm Hugo. Give me a friendly check-in in character."),
    ("Ivy", "Hi, I'm Ivy. Say a quick hello and something energetic."),
    ("Jack", "Hi, I'm Jack. Give me a punchy one-line greeting in character."),
]


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    fraction = index - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


async def _run_one(
    *,
    idx: int,
    prompt_name: str,
    text: str,
    reference_id: str,
    provider_config: Dict[str, Any],
    stagger_ms: int,
) -> Dict[str, Any]:
    if stagger_ms > 0:
        await asyncio.sleep(((idx - 1) * stagger_ms) / 1000)

    started = time.perf_counter()
    first_audio_ms = None
    total_bytes = 0
    opus_packet_count = 0
    sentence_start_count = 0

    provider = TTSProvider(dict(provider_config), delete_audio_file=True)
    provider.conn = SimpleNamespace(
        stop_event=threading.Event(),
        client_abort=False,
        voice_id=reference_id,
    )

    try:
        await provider.text_to_speak(text)

        while True:
            try:
                sentence_type, payload, _segment_text = provider.tts_audio_queue.get_nowait()
            except queue.Empty:
                break
            if sentence_type == SentenceType.FIRST:
                sentence_start_count += 1
            elif sentence_type == SentenceType.MIDDLE and isinstance(
                payload, (bytes, bytearray)
            ):
                if first_audio_ms is None:
                    first_audio_ms = (time.perf_counter() - started) * 1000
                total_bytes += len(payload)
                opus_packet_count += 1

        if opus_packet_count == 0 or total_bytes == 0:
            raise RuntimeError("No Opus audio packets were produced")

        total_ms = (time.perf_counter() - started) * 1000
        return {
            "worker": idx,
            "user": prompt_name,
            "status": 200,
            "ok": True,
            "first_chunk_ms": round(first_audio_ms or total_ms, 1),
            "total_ms": round(total_ms, 1),
            "chunk_count": opus_packet_count,
            "bytes": total_bytes,
            "sentence_starts": sentence_start_count,
        }
    except Exception as exc:
        status = None
        exc_text = str(exc)
        if "429" in exc_text:
            status = 429
        if not exc_text:
            exc_text = f"{type(exc).__name__}: {repr(exc)}"
        return {
            "worker": idx,
            "user": prompt_name,
            "status": status,
            "ok": False,
            "error": exc_text,
            "traceback": traceback.format_exc(limit=5),
        }
    finally:
        await provider.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Fish Audio concurrency smoke test")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--reference-id", required=True)
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--text-prefix", default="")
    parser.add_argument("--stagger-ms", type=int, default=0)
    args = parser.parse_args()

    config = load_config()
    fish_config = (config.get("TTS") or {}).get("FishAudio") or {}

    api_key = args.api_key or fish_config.get("api_key")
    if not api_key:
        raise ValueError("Missing Fish Audio api_key")

    provider_config = dict(fish_config)
    if args.api_url:
        provider_config["api_url"] = args.api_url
    if args.api_key:
        provider_config["api_key"] = args.api_key
    provider_config["reference_id"] = args.reference_id

    prompts: List[tuple[str, str]] = []
    for index in range(args.count):
        name, text = DEFAULT_PROMPTS[index % len(DEFAULT_PROMPTS)]
        prompts.append((f"{name}-{index + 1}", f"{args.text_prefix}{text}"))

    tasks = [
        _run_one(
            idx=index + 1,
            prompt_name=name,
            text=text,
            reference_id=args.reference_id,
            provider_config=provider_config,
            stagger_ms=args.stagger_ms,
        )
        for index, (name, text) in enumerate(prompts)
    ]
    results = await asyncio.gather(*tasks)

    ok_results = [result for result in results if result.get("ok")]
    first_chunk_values = [result["first_chunk_ms"] for result in ok_results]
    total_values = [result["total_ms"] for result in ok_results]

    summary = {
        "count": args.count,
        "reference_id": args.reference_id,
        "successes": len(ok_results),
        "failures": len(results) - len(ok_results),
        "first_chunk_ms": {
            "min": round(min(first_chunk_values), 1) if first_chunk_values else None,
            "p50": round(statistics.median(first_chunk_values), 1)
            if first_chunk_values
            else None,
            "p95": round(_percentile(first_chunk_values, 0.95), 1)
            if first_chunk_values
            else None,
            "max": round(max(first_chunk_values), 1) if first_chunk_values else None,
        },
        "total_ms": {
            "min": round(min(total_values), 1) if total_values else None,
            "p50": round(statistics.median(total_values), 1) if total_values else None,
            "p95": round(_percentile(total_values, 0.95), 1) if total_values else None,
            "max": round(max(total_values), 1) if total_values else None,
        },
        "results": sorted(results, key=lambda item: item["worker"]),
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    await close_shared_resources()


if __name__ == "__main__":
    asyncio.run(main())
