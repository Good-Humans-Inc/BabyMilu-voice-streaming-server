import queue
import threading
from typing import Any, Callable, Dict, Optional

from config.logger import setup_logging
from core.utils import vad as vad_factory

TAG = __name__

DEFAULT_VAD_POOL_SIZE = 8
DEFAULT_VAD_LEASE_TIMEOUT = 10.0
_DEFAULT_TIMEOUT = object()


class VadProviderPool:
    """Bounded lease pool for mutable VAD provider instances."""

    def __init__(
        self,
        provider_type: str,
        provider_config: Dict[str, Any],
        *,
        size: int = DEFAULT_VAD_POOL_SIZE,
        lease_timeout: float = DEFAULT_VAD_LEASE_TIMEOUT,
        logger=None,
        factory: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
    ) -> None:
        self.provider_type = provider_type
        self.provider_config = dict(provider_config or {})
        self.size = max(1, int(size))
        self.lease_timeout = max(0.1, float(lease_timeout))
        self.logger = logger or setup_logging()
        self._factory = factory or vad_factory.create_instance
        self._available = queue.Queue(maxsize=self.size)
        self._leased_ids = set()
        self._lock = threading.Lock()

        for _ in range(self.size):
            provider = self._factory(self.provider_type, dict(self.provider_config))
            self._reset_provider(provider)
            self._available.put(provider)

        self.logger.bind(tag=TAG).info(
            f"Initialized VAD provider pool type={self.provider_type} size={self.size}"
        )

    @classmethod
    def from_config(cls, config: Dict[str, Any], logger=None, factory=None):
        selected = (config.get("selected_module") or {}).get("VAD")
        if not selected:
            return None

        vad_config = (config.get("VAD") or {}).get(selected)
        if not vad_config:
            return None

        provider_type = vad_config.get("type", selected)
        size = cls._configured_int(
            config,
            vad_config,
            ("pool_size",),
            ("vad_pool_size", "vad_pool.size"),
            DEFAULT_VAD_POOL_SIZE,
        )
        lease_timeout = cls._configured_float(
            config,
            vad_config,
            ("lease_timeout", "pool_lease_timeout"),
            ("vad_lease_timeout", "vad_pool.lease_timeout"),
            DEFAULT_VAD_LEASE_TIMEOUT,
        )
        return cls(
            provider_type,
            vad_config,
            size=size,
            lease_timeout=lease_timeout,
            logger=logger,
            factory=factory,
        )

    @staticmethod
    def _nested_get(config: Dict[str, Any], path: str):
        value = config
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    @classmethod
    def _configured_int(
        cls,
        config: Dict[str, Any],
        provider_config: Dict[str, Any],
        provider_keys,
        concurrency_keys,
        default: int,
    ) -> int:
        raw = None
        for key in provider_keys:
            raw = provider_config.get(key)
            if raw is not None:
                break
        if raw is None:
            concurrency = config.get("concurrency") or {}
            for key in concurrency_keys:
                raw = cls._nested_get(concurrency, key)
                if raw is not None:
                    break
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return default

    @classmethod
    def _configured_float(
        cls,
        config: Dict[str, Any],
        provider_config: Dict[str, Any],
        provider_keys,
        concurrency_keys,
        default: float,
    ) -> float:
        raw = None
        for key in provider_keys:
            raw = provider_config.get(key)
            if raw is not None:
                break
        if raw is None:
            concurrency = config.get("concurrency") or {}
            for key in concurrency_keys:
                raw = cls._nested_get(concurrency, key)
                if raw is not None:
                    break
        try:
            return max(0.1, float(raw))
        except (TypeError, ValueError):
            return default

    def acquire(self, timeout=_DEFAULT_TIMEOUT):
        wait_timeout = self.lease_timeout if timeout is _DEFAULT_TIMEOUT else timeout
        try:
            provider = self._available.get(timeout=wait_timeout)
        except queue.Empty as exc:
            raise TimeoutError(
                f"Timed out waiting for VAD provider lease "
                f"type={self.provider_type} size={self.size}"
            ) from exc

        with self._lock:
            self._leased_ids.add(id(provider))
        return provider

    def release(self, provider) -> None:
        if provider is None:
            return

        provider_id = id(provider)
        with self._lock:
            if provider_id not in self._leased_ids:
                self.logger.bind(tag=TAG).warning(
                    f"Ignoring release for unknown VAD provider id={provider_id}"
                )
                return
            self._leased_ids.remove(provider_id)

        self._reset_provider(provider)
        self._available.put(provider)

    def _reset_provider(self, provider) -> None:
        for method_name in ("reset_states", "reset_model_states"):
            reset = getattr(provider, method_name, None)
            if not callable(reset):
                continue
            try:
                reset()
            except Exception as exc:
                self.logger.bind(tag=TAG).warning(
                    f"VAD provider state reset failed: {exc}"
                )
            return

    @property
    def available(self) -> int:
        return self._available.qsize()

    @property
    def leased(self) -> int:
        with self._lock:
            return len(self._leased_ids)
