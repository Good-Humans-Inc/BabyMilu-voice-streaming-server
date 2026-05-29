import asyncio
import signal

from .config import load_config
from .server import EchoEarServer


async def _run() -> None:
    config = load_config()
    server = EchoEarServer(config)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await server.start()
    await stop_event.wait()
    await server.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
