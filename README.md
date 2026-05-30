# EchoEar Ground-Up Audio Server

Minimal WebSocket audio server for EchoEar bring-up.

## Protocol

- Device connects to `ws://host:8000/xiaozhi/v1/`.
- Device sends `hello`; server replies with a session id and `opus/16000/mono/60ms`.
- Device sends `listen:start`; binary audio frames are only queued.
- Device sends `listen:stop`; the queued frames are processed once: terminal ASR, LLM, Fish Audio TTS.
- Server sends `stt`, `tts:start`, `llm`, `tts:sentence_start`, binary Opus TTS frames, then `tts:stop`.

Terminal ASR is intentional here. Since EchoEar handles VAD/AEC device-side and sends a hard `listen:stop`, the simplest and most predictable path is to decode the complete utterance once and transcribe it once. Streaming ASR would only be worth adding after the basic interaction is stable and if sub-second partial transcripts become important.

## Runtime Config

The service reads `data/.config.yaml` first, then `config.yaml`, and supports env overrides:

- `OPENAI_API_KEY`
- `FISH_AUDIO_API_KEY`
- `FISH_AUDIO_REFERENCE_ID`
- `ECHOEAR_MOCK_PROVIDERS=1`
- `ECHOEAR_WS_PORT`
- `ECHOEAR_HTTP_PORT`
- `ECHOEAR_TTS_FRAME_INTERVAL_MS` defaults to `60`, matching the Opus frame duration so small device playback queues are not flooded.

The staging VM already keeps real API config in `/srv/dev/current/data/.config.yaml`; do not commit secrets.

## Local Run

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:ECHOEAR_MOCK_PROVIDERS="1"
.\.venv\Scripts\python -m echoear_server
```

## Tests

```powershell
pytest
```

## Smoke

```powershell
python tools/smoke_ws.py --url ws://127.0.0.1:8000/xiaozhi/v1/ --text "Hey EchoEar, can you hear me?"
```
