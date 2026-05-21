from __future__ import annotations

import pathlib
import sys

import pytest

sys_path = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(sys_path))

from core.vad_pool import VadProviderPool


class _Logger:
    def bind(self, **_kwargs):
        return self

    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


class _FakeProvider:
    def __init__(self):
        self.reset_count = 0

    def reset_states(self):
        self.reset_count += 1


def test_vad_provider_pool_leases_distinct_instances_and_blocks_when_empty():
    created = []

    def factory(_provider_type, _config):
        provider = _FakeProvider()
        created.append(provider)
        return provider

    pool = VadProviderPool(
        "fake",
        {},
        size=2,
        lease_timeout=0.01,
        logger=_Logger(),
        factory=factory,
    )

    first = pool.acquire()
    second = pool.acquire()

    assert first is created[0]
    assert second is created[1]
    assert first is not second
    assert pool.available == 0
    assert pool.leased == 2

    with pytest.raises(TimeoutError):
        pool.acquire(timeout=0.01)


def test_vad_provider_pool_resets_provider_before_reuse():
    provider = _FakeProvider()
    pool = VadProviderPool(
        "fake",
        {},
        size=1,
        logger=_Logger(),
        factory=lambda *_args: provider,
    )

    assert provider.reset_count == 1

    leased = pool.acquire()
    pool.release(leased)

    assert provider.reset_count == 2
    assert pool.available == 1
    assert pool.leased == 0
    assert pool.acquire() is provider


def test_vad_provider_pool_reads_selected_vad_config():
    created_args = []

    def factory(provider_type, provider_config):
        created_args.append((provider_type, provider_config))
        return _FakeProvider()

    pool = VadProviderPool.from_config(
        {
            "selected_module": {"VAD": "SileroVAD"},
            "VAD": {"SileroVAD": {"type": "silero", "pool_size": 3}},
            "concurrency": {"vad_pool": {"lease_timeout": 0.5}},
        },
        logger=_Logger(),
        factory=factory,
    )

    assert pool.size == 3
    assert pool.lease_timeout == 0.5
    assert created_args == [
        ("silero", {"type": "silero", "pool_size": 3}),
        ("silero", {"type": "silero", "pool_size": 3}),
        ("silero", {"type": "silero", "pool_size": 3}),
    ]
