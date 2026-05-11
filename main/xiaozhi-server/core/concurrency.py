import asyncio
import functools
import queue
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional


class RejectedExecutionError(RuntimeError):
    """Raised when a bounded executor has no worker or queue capacity."""


class BoundedThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor with a bounded pending+running task count.

    ThreadPoolExecutor's internal queue is unbounded.  For websocket servers that
    accept many concurrent clients, that can turn one slow provider into process
    wide memory pressure.  This executor rejects immediately when saturated so
    callers can apply a timeout/error path without blocking the event loop.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        max_queue_size: int,
        thread_name_prefix: str,
    ) -> None:
        super().__init__(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix=thread_name_prefix,
        )
        self.max_queue_size = max(0, int(max_queue_size))
        self._capacity = threading.BoundedSemaphore(
            max(1, int(max_workers)) + self.max_queue_size
        )
        self._submitted = 0
        self._rejected = 0
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        if not self._capacity.acquire(blocking=False):
            with self._lock:
                self._rejected += 1
            rejected = Future()
            rejected.set_exception(
                RejectedExecutionError(
                    f"{self._thread_name_prefix} executor queue is full"
                )
            )
            return rejected

        try:
            future = super().submit(fn, *args, **kwargs)
        except Exception:
            self._capacity.release()
            raise

        with self._lock:
            self._submitted += 1
        future.add_done_callback(lambda _future: self._capacity.release())
        return future

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {
                "submitted": self._submitted,
                "rejected": self._rejected,
                "max_workers": self._max_workers,
                "max_queue_size": self.max_queue_size,
            }


class DropOldestQueue(queue.Queue):
    """Bounded queue that never blocks producers indefinitely.

    On overflow it drops the oldest queued item and accepts the new one.  That is
    the least surprising behavior for realtime streams: fresh audio/status data
    is more useful than stale backlog.
    """

    def __init__(self, maxsize: int = 0, *, name: str = "queue") -> None:
        super().__init__(maxsize=max(0, int(maxsize)))
        self.name = name
        self.dropped_count = 0

    def put(
        self,
        item: Any,
        block: bool = True,
        timeout: Optional[float] = None,
    ) -> None:
        if self.maxsize <= 0:
            return super().put(item, block=block, timeout=timeout)
        while True:
            try:
                return super().put(item, block=False)
            except queue.Full:
                try:
                    self.get_nowait()
                    self.dropped_count += 1
                except queue.Empty:
                    continue

    def put_nowait(self, item: Any) -> None:
        self.put(item, block=False)


@dataclass(frozen=True)
class ExecutorTimeouts:
    profile: float = 8.0
    db: float = 8.0
    provider: float = 60.0
    tool: float = 20.0
    audio: float = 15.0
    persistence: float = 10.0
    bootstrap_text_wait: float = 12.0


class ServerExecutors:
    def __init__(
        self,
        *,
        profile: BoundedThreadPoolExecutor,
        db: BoundedThreadPoolExecutor,
        provider: BoundedThreadPoolExecutor,
        tool: BoundedThreadPoolExecutor,
        audio: BoundedThreadPoolExecutor,
        persistence: BoundedThreadPoolExecutor,
        timeouts: ExecutorTimeouts,
    ) -> None:
        self.profile = profile
        self.db = db
        self.provider = provider
        self.tool = tool
        self.audio = audio
        self.persistence = persistence
        self.timeouts = timeouts

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "ServerExecutors":
        concurrency = dict(config.get("concurrency") or {})
        executors = dict(concurrency.get("executors") or {})
        timeouts_cfg = dict(concurrency.get("timeouts") or {})

        def section(name: str, workers: int, queue_size: int) -> BoundedThreadPoolExecutor:
            cfg = dict(executors.get(name) or {})
            return BoundedThreadPoolExecutor(
                max_workers=int(cfg.get("max_workers", workers)),
                max_queue_size=int(cfg.get("max_queue_size", queue_size)),
                thread_name_prefix=f"xiaozhi-{name}",
            )

        timeouts = ExecutorTimeouts(
            profile=float(timeouts_cfg.get("profile", 8.0)),
            db=float(timeouts_cfg.get("db", 8.0)),
            provider=float(timeouts_cfg.get("provider", 60.0)),
            tool=float(timeouts_cfg.get("tool", 20.0)),
            audio=float(timeouts_cfg.get("audio", 15.0)),
            persistence=float(timeouts_cfg.get("persistence", 10.0)),
            bootstrap_text_wait=float(timeouts_cfg.get("bootstrap_text_wait", 12.0)),
        )

        return cls(
            profile=section("profile", 8, 100),
            db=section("db", 8, 500),
            provider=section("provider", 24, 1000),
            tool=section("tool", 16, 500),
            audio=section("audio", 16, 1000),
            persistence=section("persistence", 8, 500),
            timeouts=timeouts,
        )

    def get(self, name: str) -> BoundedThreadPoolExecutor:
        return getattr(self, name)

    def timeout_for(self, name: str) -> float:
        return float(getattr(self.timeouts, name))

    async def run_sync(
        self,
        name: str,
        func: Callable[..., Any],
        *args: Any,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Any:
        loop = asyncio.get_running_loop()
        executor = self.get(name)
        bound = functools.partial(func, *args, **kwargs)
        future = loop.run_in_executor(executor, bound)
        try:
            return await asyncio.wait_for(
                future,
                timeout=self.timeout_for(name) if timeout is None else timeout,
            )
        except asyncio.TimeoutError:
            future.cancel()
            raise

    def shutdown(self) -> None:
        for executor in (
            self.profile,
            self.db,
            self.provider,
            self.tool,
            self.audio,
            self.persistence,
        ):
            executor.shutdown(wait=False, cancel_futures=True)
