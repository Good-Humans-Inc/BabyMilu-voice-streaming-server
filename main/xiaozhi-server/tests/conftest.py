from __future__ import annotations

import pathlib
import sys
import types
import asyncio

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_opus_stub() -> None:
    for name in list(sys.modules):
        if name == "opuslib_next" or name.startswith("opuslib_next."):
            sys.modules.pop(name, None)

    opus_stub = types.ModuleType("opuslib_next")
    constants_stub = types.ModuleType("opuslib_next.constants")

    class OpusError(Exception):
        pass

    class Decoder:
        def __init__(self, sample_rate=16000, channels=1):
            self.sample_rate = sample_rate
            self.channels = channels

        def decode(self, packet, frame_size):
            if packet is None:
                raise OpusError("empty opus packet")
            return b"\x00\x00" * int(frame_size)

    class Encoder:
        def __init__(self, sample_rate=16000, channels=1, application=None):
            self.sample_rate = sample_rate
            self.channels = channels
            self.application = application

        def encode(self, pcm, frame_size):
            return bytes(pcm or b"")[: max(1, int(frame_size))]

    constants_stub.APPLICATION_AUDIO = 2049
    opus_stub.APPLICATION_AUDIO = constants_stub.APPLICATION_AUDIO
    opus_stub.OpusError = OpusError
    opus_stub.Decoder = Decoder
    opus_stub.Encoder = Encoder
    opus_stub.constants = constants_stub

    sys.modules["opuslib_next"] = opus_stub
    sys.modules["opuslib_next.constants"] = constants_stub


try:
    import opuslib_next  # noqa: F401
except Exception:
    _install_opus_stub()


def _install_mcp_stub() -> None:
    mcp_stub = types.ModuleType("mcp")
    client_stub = types.ModuleType("mcp.client")
    stdio_stub = types.ModuleType("mcp.client.stdio")
    sse_stub = types.ModuleType("mcp.client.sse")

    class StdioServerParameters:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class ClientSession:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return types.SimpleNamespace(tools=[])

        async def call_tool(self, name, args):
            return types.SimpleNamespace(name=name, args=args)

    class _NullAsyncContext:
        async def __aenter__(self):
            return (None, None)

        async def __aexit__(self, *_args):
            return False

    def stdio_client(*_args, **_kwargs):
        return _NullAsyncContext()

    def sse_client(*_args, **_kwargs):
        return _NullAsyncContext()

    mcp_stub.ClientSession = ClientSession
    mcp_stub.StdioServerParameters = StdioServerParameters
    stdio_stub.stdio_client = stdio_client
    sse_stub.sse_client = sse_client

    sys.modules["mcp"] = mcp_stub
    sys.modules["mcp.client"] = client_stub
    sys.modules["mcp.client.stdio"] = stdio_stub
    sys.modules["mcp.client.sse"] = sse_stub


try:
    import mcp  # noqa: F401
except Exception:
    _install_mcp_stub()


@pytest.fixture(autouse=True)
def _ensure_default_event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)
