import argparse
import asyncio
import json
import statistics
import time
from typing import Any, Dict, List

import aiohttp
import ormsgpack

from config.settings import load_config
from core.providers.tts.fishspeech import ServeTTSRequest


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
    api_url: str,
    api_key: str,
    reference_id: str,
    chunk_length: int,
    normalize: bool,
    top_p: float,
    temperature: float,
    repetition_penalty: float,
    session: aiohttp.ClientSession,
    stagger_ms: int,
) -> Dict[str, Any]:
    if stagger_ms > 0:
        await asyncio.sleep(((idx - 1) * stagger_ms) / 1000)

    request_data = ServeTTSRequest(
        text=text,
        reference_id=reference_id,
        format="pcm",
        normalize=normalize,
        chunk_length=chunk_length,
        top_p=top_p,
        temperature=temperature,
        repetition_penalty=repetition_penalty,
        streaming=True,
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/msgpack",
    }

    started = time.perf_counter()
    first_chunk_ms = None
    total_bytes = 0
    chunk_count = 0

    try:
        async with session.post(
            api_url,
            data=ormsgpack.packb(
                request_data, option=ormsgpack.OPT_SERIALIZE_PYDANTIC
            ),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                return {
                    "worker": idx,
                    "user": prompt_name,
                    "status": resp.status,
                    "ok": False,
                    "error": body[:300],
                }

            async for chunk in resp.content.iter_chunked(4096):
                if not chunk:
                    continue
                if first_chunk_ms is None:
                    first_chunk_ms = (time.perf_counter() - started) * 1000
                total_bytes += len(chunk)
                chunk_count += 1

        total_ms = (time.perf_counter() - started) * 1000
        return {
            "worker": idx,
            "user": prompt_name,
            "status": 200,
            "ok": True,
            "first_chunk_ms": round(first_chunk_ms or total_ms, 1),
            "total_ms": round(total_ms, 1),
            "chunk_count": chunk_count,
            "bytes": total_bytes,
        }
    except Exception as exc:
        return {
            "worker": idx,
            "user": prompt_name,
            "status": None,
            "ok": False,
            "error": str(exc),
        }


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

    api_url = args.api_url or fish_config.get("api_url", "https://api.fish.audio/v1/tts")
    api_key = args.api_key or fish_config.get("api_key")
    if not api_key:
        raise ValueError("Missing Fish Audio api_key")

    prompts: List[tuple[str, str]] = []
    for index in range(args.count):
        name, text = DEFAULT_PROMPTS[index % len(DEFAULT_PROMPTS)]
        prompts.append((f"{name}-{index + 1}", f"{args.text_prefix}{text}"))

    async with aiohttp.ClientSession() as session:
        tasks = [
            _run_one(
                idx=index + 1,
                prompt_name=name,
                text=text,
                api_url=api_url,
                api_key=api_key,
                reference_id=args.reference_id,
                chunk_length=int(fish_config.get("chunk_length", 100)),
                normalize=bool(fish_config.get("normalize", True)),
                top_p=float(fish_config.get("top_p", 0.7)),
                temperature=float(fish_config.get("temperature", 0.7)),
                repetition_penalty=float(fish_config.get("repetition_penalty", 1.2)),
                session=session,
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


if __name__ == "__main__":
    asyncio.run(main())
