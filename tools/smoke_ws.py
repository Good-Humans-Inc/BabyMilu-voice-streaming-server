import argparse
import asyncio
import json
import sys

import websockets


async def run(url: str, text: str, timeout: float) -> int:
    seen = {"hello": False, "stt": False, "llm": False, "tts_stop": False}
    audio_frames = 0

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "hello", "version": 3, "audio_params": {"format": "opus"}}))
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        print("hello", hello)
        seen["hello"] = hello.get("type") == "hello"

        await ws.send(json.dumps({"type": "listen", "state": "detect", "text": text}))
        while True:
            message = await asyncio.wait_for(ws.recv(), timeout=timeout)
            if isinstance(message, bytes):
                audio_frames += 1
                continue
            data = json.loads(message)
            print(data)
            if data.get("type") == "stt":
                seen["stt"] = bool(data.get("text"))
            if data.get("type") == "llm":
                seen["llm"] = bool(data.get("text"))
            if data.get("type") == "tts" and data.get("state") == "stop":
                seen["tts_stop"] = True
                break

    print(f"audio_frames={audio_frames}")
    missing = [name for name, ok in seen.items() if not ok]
    if missing:
        print(f"missing expected events: {missing}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--text", default="Hey EchoEar, can you hear me?")
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.url, args.text, args.timeout)))


if __name__ == "__main__":
    main()

