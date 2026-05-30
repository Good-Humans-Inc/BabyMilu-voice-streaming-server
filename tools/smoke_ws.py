import argparse
import asyncio
import json
import sys
from urllib.parse import urlencode, urlsplit, urlunsplit

import websockets


def with_device_id(url: str, device_id: str | None) -> str:
    if not device_id:
        return url
    parts = urlsplit(url)
    query = parts.query
    extra = urlencode({"device-id": device_id, "client-id": device_id})
    query = f"{query}&{extra}" if query else extra
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


async def run(url: str, text: str, timeout: float, turns: int, device_id: str | None) -> int:
    seen = {"hello": False}
    total_audio_frames = 0

    url = with_device_id(url, device_id)
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        hello_payload = {"type": "hello", "version": 3, "audio_params": {"format": "opus"}}
        if device_id:
            hello_payload["device_id"] = device_id
        await ws.send(json.dumps(hello_payload))
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        print("hello", hello)
        seen["hello"] = hello.get("type") == "hello"

        for turn in range(1, turns + 1):
            turn_seen = {"stt": False, "llm": False, "tts_stop": False}
            turn_audio_frames = 0
            turn_text = text if turns == 1 else f"{text} Turn {turn}."
            print(f"turn {turn} send listen:detect text={turn_text!r}")
            await ws.send(json.dumps({"type": "listen", "state": "detect", "text": turn_text}))
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=timeout)
                if isinstance(message, bytes):
                    turn_audio_frames += 1
                    total_audio_frames += 1
                    continue
                data = json.loads(message)
                print(data)
                if data.get("type") == "stt":
                    turn_seen["stt"] = bool(data.get("text"))
                if data.get("type") == "llm":
                    turn_seen["llm"] = bool(data.get("text"))
                if data.get("type") == "tts" and data.get("state") == "stop":
                    turn_seen["tts_stop"] = True
                    break
            seen.update({f"turn_{turn}_{name}": ok for name, ok in turn_seen.items()})
            print(f"turn {turn} audio_frames={turn_audio_frames}")

    print(f"total_audio_frames={total_audio_frames}")
    missing = [name for name, ok in seen.items() if not ok]
    if missing:
        print(f"missing expected events: {missing}", file=sys.stderr)
        return 1
    if total_audio_frames <= 0:
        print("missing expected binary TTS audio frames", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--text", default="Hey EchoEar, can you hear me?")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--turns", type=int, default=1)
    parser.add_argument("--device-id")
    args = parser.parse_args()
    if args.turns < 1:
        parser.error("--turns must be at least 1")
    raise SystemExit(asyncio.run(run(args.url, args.text, args.timeout, args.turns, args.device_id)))


if __name__ == "__main__":
    main()
