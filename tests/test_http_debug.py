from echoear_server.config import load_config
from echoear_server.server import EchoEarServer


async def test_debug_turn_uses_mock_providers(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOEAR_MOCK_PROVIDERS", "1")
    cfg = load_config(tmp_path)
    server = EchoEarServer(cfg)
    app_runner = await server._start_http("127.0.0.1", 0)
    site = next(iter(app_runner.sites))
    sockets = getattr(site, "_server").sockets
    port = sockets[0].getsockname()[1]

    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/debug/turn",
                json={"text": "Can you hear me?"},
            ) as response:
                body = await response.json()
        assert response.status == 200
        assert body["ok"] is True
        assert "Can you hear me" in body["reply"]
        assert body["audio_frames"] == 3
    finally:
        await app_runner.cleanup()
