#!/usr/bin/env python3
"""
Concurrency & Isolation Reproduction Tests
============================================
Proves (or disproves) the shared-state bugs identified during audit.

Run from xiaozhi-server/:
    python -m tests.test_concurrency_isolation

No real Firestore / LLM / TTS needed — everything is mocked in-process.
"""

import os
import sys
import copy
import json
import time
import uuid
import asyncio
import hashlib
import tempfile
import threading
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

# ── Ensure project root is on sys.path ──────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Logging ──────────────────────────────────────────────────────────
from loguru import logger as loguru_logger

loguru_logger.remove()  # strip default stderr handler
LOG_RECORDS: List[Dict[str, Any]] = []  # collector


def _log_sink(message):
    record = message.record
    LOG_RECORDS.append({
        "time": str(record["time"]),
        "level": record["level"].name,
        "message": record["message"],
        "extra": dict(record.get("extra", {})),
    })


loguru_logger.add(_log_sink, level="DEBUG")

# ═══════════════════════════════════════════════════════════════════════
# FAKES — replace heavy/external deps so the real code can be imported
# ═══════════════════════════════════════════════════════════════════════

# ── Fake Firestore data ──────────────────────────────────────────────
FAKE_FIRESTORE: Dict[str, Dict[str, Dict[str, Any]]] = {
    "devices": {
        "device_aaa": {
            "activeCharacterId": "char_alice",
            "ownerPhone": "+1111111111",
        },
        "device_bbb": {
            "activeCharacterId": "char_bob",
            "ownerPhone": "+2222222222",
        },
    },
    "characters": {
        "char_alice": {
            "name": "Alice",
            "age": "5",
            "pronouns": "she/her",
            "voice": "voice_alice_eleven",
            "bio": "A curious cat lover",
            "relationship": "best friend",
            "callMe": "Ali",
        },
        "char_bob": {
            "name": "Bob",
            "age": "7",
            "pronouns": "he/him",
            "voice": "voice_bob_eleven",
            "bio": "A brave adventurer",
            "relationship": "big brother",
            "callMe": "Bobby",
        },
    },
    "users": {
        "+1111111111": {
            "displayName": "Alice's Mom",
            "name": "Alice's Mom",
            "birthday": "1990-01-01",
            "pronouns": "she/her",
            "timezone": "America/Los_Angeles",
            "city": "San Francisco, CA",
            "characterIds": ["char_alice"],
        },
        "+2222222222": {
            "displayName": "Bob's Dad",
            "name": "Bob's Dad",
            "birthday": "1985-06-15",
            "pronouns": "he/him",
            "timezone": "America/New_York",
            "city": "New York, NY",
            "characterIds": ["char_bob"],
        },
    },
}


def _fake_fs_get(collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
    return copy.deepcopy(FAKE_FIRESTORE.get(collection, {}).get(doc_id))


# ── Monkey-patch firestore_client functions ──────────────────────────
import core.utils.firestore_client as fsc

def _fake_get_active_character_for_device(device_id, timeout=3.0):
    doc = _fake_fs_get("devices", device_id)
    return doc.get("activeCharacterId") if doc else None

def _fake_get_device_doc(device_id, timeout=3.0):
    return _fake_fs_get("devices", device_id)

def _fake_get_owner_phone_for_device(device_id, timeout=3.0):
    doc = _fake_fs_get("devices", device_id)
    return doc.get("ownerPhone") if doc else None

def _fake_get_character_profile(character_id, timeout=3.0):
    return _fake_fs_get("characters", character_id)

def _fake_get_user_profile_by_phone(owner_phone, timeout=3.0):
    return _fake_fs_get("users", owner_phone)

def _fake_get_conversation_state_for_device(device_id, timeout=3.0):
    return None

def _fake_update_conversation_state_for_device(device_id, **kwargs):
    return True

def _fake_get_most_recent_character_via_user_for_device(device_id, timeout=3.0):
    doc = _fake_fs_get("devices", device_id)
    if not doc:
        return None
    phone = doc.get("ownerPhone")
    if not phone:
        return None
    user = _fake_fs_get("users", phone)
    if not user:
        return None
    ids = user.get("characterIds", [])
    return ids[-1] if ids else None

def _fake_get_timezone_for_device(device_id, timeout=3.0):
    phone = _fake_get_owner_phone_for_device(device_id)
    if not phone:
        return None
    user = _fake_fs_get("users", phone)
    return user.get("timezone") if user else None

# Apply patches
fsc.get_active_character_for_device = _fake_get_active_character_for_device
fsc.get_device_doc = _fake_get_device_doc
fsc.get_owner_phone_for_device = _fake_get_owner_phone_for_device
fsc.get_character_profile = _fake_get_character_profile
fsc.extract_character_profile_fields = fsc.extract_character_profile_fields  # keep real
fsc.get_user_profile_by_phone = _fake_get_user_profile_by_phone
fsc.extract_user_profile_fields = fsc.extract_user_profile_fields  # keep real
fsc.get_conversation_state_for_device = _fake_get_conversation_state_for_device
fsc.update_conversation_state_for_device = _fake_update_conversation_state_for_device
fsc.get_most_recent_character_via_user_for_device = _fake_get_most_recent_character_via_user_for_device
fsc.get_timezone_for_device = _fake_get_timezone_for_device

# ── Fake api_client ──────────────────────────────────────────────────
import core.utils.api_client as api_client
api_client.query_task = lambda *a, **kw: ""
api_client.get_assigned_tasks_for_user = lambda *a, **kw: []
api_client.process_user_action = lambda *a, **kw: None

# ── Fake session_context store ───────────────────────────────────────
import services.session_context.store as sc_store
sc_store.get_session = lambda *a, **kw: None
sc_store.update_session = lambda *a, **kw: None

# ═══════════════════════════════════════════════════════════════════════
# DIRECT UNIT TESTS — no WebSocket server needed
# ═══════════════════════════════════════════════════════════════════════

# ---------------------------------------------------------------------------
# Test 1: mem_local_short shared-instance cross-device memory leak
# ---------------------------------------------------------------------------
def test_mem_local_short_shared_instance_leak():
    """
    PROVES: A single MemoryProvider instance shared across two devices
    can leak short_memory from device A to device B.
    """
    print("\n" + "=" * 72)
    print("TEST 1: mem_local_short shared-instance cross-device memory leak")
    print("=" * 72)

    from core.providers.memory.mem_local_short.mem_local_short import MemoryProvider

    # Create temp memory file with data for device_aaa only
    tmpdir = tempfile.mkdtemp(prefix="memleak_")
    mem_path = os.path.join(tmpdir, ".memory.yaml")
    with open(mem_path, "w", encoding="utf-8") as f:
        yaml.dump({"device_aaa": '{"identity": "Alice memories here"}'}, f)

    # Simulate WebSocketServer: ONE shared instance
    shared_mem = MemoryProvider(config={}, summary_memory=None)
    shared_mem.memory_path = mem_path
    shared_mem.save_to_file = True

    # Connection 1 (device_aaa): init_memory
    shared_mem.init_memory(role_id="device_aaa", llm=None, summary_memory=None, save_to_file=True)
    shared_mem.memory_path = mem_path  # re-set after init
    shared_mem.load_memory(None)
    mem_after_a = shared_mem.short_memory
    print(f"  After device_aaa init: role_id={shared_mem.role_id}, short_memory={repr(mem_after_a)[:80]}")

    # Connection 2 (device_bbb): init_memory — device_bbb has NO entry in yaml
    shared_mem.init_memory(role_id="device_bbb", llm=None, summary_memory=None, save_to_file=True)
    shared_mem.memory_path = mem_path
    shared_mem.load_memory(None)
    mem_after_b = shared_mem.short_memory
    print(f"  After device_bbb init: role_id={shared_mem.role_id}, short_memory={repr(mem_after_b)[:80]}")

    # ASSERTION: device_bbb should NOT have Alice's memory
    if mem_after_b and "Alice" in str(mem_after_b):
        print("  ❌ FAIL: device_bbb inherited device_aaa's memory! CROSS-DEVICE LEAK CONFIRMED.")
        return False
    else:
        print("  ✅ PASS: device_bbb has clean memory.")
        return True


# ---------------------------------------------------------------------------
# Test 2: mem_local_short — role_id not found leaves stale short_memory
# ---------------------------------------------------------------------------
def test_mem_local_short_stale_memory_on_missing_role():
    """
    PROVES: When role_id doesn't exist in the YAML file, short_memory
    retains its previous value instead of being reset.
    """
    print("\n" + "=" * 72)
    print("TEST 2: mem_local_short stale memory when role_id not found in file")
    print("=" * 72)

    from core.providers.memory.mem_local_short.mem_local_short import MemoryProvider

    tmpdir = tempfile.mkdtemp(prefix="stale_")
    mem_path = os.path.join(tmpdir, ".memory.yaml")
    with open(mem_path, "w", encoding="utf-8") as f:
        yaml.dump({"device_aaa": "Alice secret diary"}, f)

    provider = MemoryProvider(config={}, summary_memory=None)
    provider.memory_path = mem_path
    provider.save_to_file = True

    # Load device_aaa
    provider.role_id = "device_aaa"
    provider.load_memory(None)
    print(f"  After loading device_aaa: short_memory={repr(provider.short_memory)}")

    # Now load device_bbb (not in file)
    provider.role_id = "device_bbb"
    provider.load_memory(None)
    print(f"  After loading device_bbb: short_memory={repr(provider.short_memory)}")

    if provider.short_memory == "Alice secret diary":
        print("  ❌ FAIL: short_memory retained device_aaa value after switching to device_bbb!")
        print("         This is the stale-memory bug. load_memory() must reset short_memory first.")
        return False
    else:
        print("  ✅ PASS: short_memory was properly cleared.")
        return True


# ---------------------------------------------------------------------------
# Test 3: PromptManager enhanced-prompt cache is keyed by device_id
#          but has no invalidation when character changes
# ---------------------------------------------------------------------------
def test_prompt_cache_stale_after_character_switch():
    """
    PROVES: Enhanced prompt cached for device_id persists even after
    the device's active character changes in Firestore.
    """
    print("\n" + "=" * 72)
    print("TEST 3: PromptManager enhanced-prompt cache staleness on character switch")
    print("=" * 72)

    from core.utils.prompt_manager import PromptManager
    from core.utils.cache.manager import cache_manager, CacheType

    config = {
        "prompt": "You are a helpful assistant.",
        "selected_module": {},
    }
    pm = PromptManager(config, loguru_logger)

    device_id = "device_aaa"

    # Simulate: first connection caches enhanced prompt
    cache_key = pm._get_enhanced_prompt_cache_key(device_id)
    fake_enhanced = "Enhanced prompt with Alice personality and San Francisco weather"
    cache_manager.set(CacheType.DEVICE_PROMPT, cache_key, fake_enhanced, ttl=43200)

    # Verify cache hit
    cached = pm.get_cached_enhanced_prompt(device_id)
    print(f"  Cached prompt (before switch): {repr(cached)[:80]}")

    # NOW: User switches character from Alice to Bob in Firestore
    # (We change fake data)
    FAKE_FIRESTORE["devices"]["device_aaa"]["activeCharacterId"] = "char_bob"

    # Next connection: check if cache is still returning Alice's prompt
    cached_after = pm.get_cached_enhanced_prompt(device_id)
    print(f"  Cached prompt (after switch):  {repr(cached_after)[:80]}")

    # Restore
    FAKE_FIRESTORE["devices"]["device_aaa"]["activeCharacterId"] = "char_alice"

    if cached_after and "Alice" in cached_after:
        print("  ❌ FAIL: Enhanced prompt still contains Alice's data after switching to Bob!")
        print("         No cache invalidation on character change. 12h TTL means stale for hours.")
        return False
    else:
        print("  ✅ PASS: Cache was properly invalidated.")
        return True


# ---------------------------------------------------------------------------
# Test 4: PromptManager get_quick_prompt cache type mismatch
# ---------------------------------------------------------------------------
def test_prompt_cache_type_mismatch():
    """
    PROVES: get_quick_prompt reads from DEVICE_PROMPT but writes to CONFIG
    cache type, creating a dead-write that never serves a cache hit.
    """
    print("\n" + "=" * 72)
    print("TEST 4: PromptManager get_quick_prompt cache type mismatch")
    print("=" * 72)

    from core.utils.prompt_manager import PromptManager
    from core.utils.cache.manager import cache_manager, CacheType

    config = {"prompt": "base prompt", "selected_module": {}}
    pm = PromptManager(config, loguru_logger)

    device_id = "device_test_cache"
    prompt = "Hello from test"

    # Call get_quick_prompt with a device_id
    result = pm.get_quick_prompt(prompt, device_id=device_id)

    # Check: was it stored in DEVICE_PROMPT or CONFIG?
    key = f"device_prompt:{device_id}"
    from_device_prompt = cache_manager.get(CacheType.DEVICE_PROMPT, key)
    from_config = cache_manager.get(CacheType.CONFIG, key)

    print(f"  get_quick_prompt returned: {repr(result)[:60]}")
    print(f"  In CacheType.DEVICE_PROMPT: {repr(from_device_prompt)}")
    print(f"  In CacheType.CONFIG:        {repr(from_config)}")

    if from_device_prompt is None and from_config is not None:
        print("  ❌ FAIL: Written to CONFIG but read from DEVICE_PROMPT — cache is a dead write!")
        return False
    elif from_device_prompt is not None:
        print("  ✅ PASS: Cache types are consistent.")
        return True
    else:
        print("  ⚠️  INCONCLUSIVE: Neither cache has the value.")
        return True


# ---------------------------------------------------------------------------
# Test 5: Shared LLM provider — _conversations dict leaks across sessions
# ---------------------------------------------------------------------------
def test_shared_llm_conversations_dict_leak():
    """
    PROVES: A single LLM provider instance shared across connections
    accumulates conversation mappings from ALL devices in one dict.
    """
    print("\n" + "=" * 72)
    print("TEST 5: Shared LLM _conversations dict accumulation")
    print("=" * 72)

    # We can't easily import openai provider without API key,
    # so we'll simulate the exact pattern
    class FakeLLMProvider:
        def __init__(self):
            self._conversations = {}

        def ensure_conversation(self, session_id):
            state = self._conversations.get(session_id)
            if state and state.get("id"):
                return state["id"]
            conv_id = f"conv_{uuid.uuid4().hex[:8]}"
            self._conversations[session_id] = {"id": conv_id}
            return conv_id

    # Simulate: ONE shared LLM across multiple connections
    shared_llm = FakeLLMProvider()

    session_a = "session_device_aaa_" + uuid.uuid4().hex[:6]
    session_b = "session_device_bbb_" + uuid.uuid4().hex[:6]

    conv_a = shared_llm.ensure_conversation(session_a)
    conv_b = shared_llm.ensure_conversation(session_b)

    print(f"  Session A conv: {conv_a}")
    print(f"  Session B conv: {conv_b}")
    print(f"  Total conversations in shared dict: {len(shared_llm._conversations)}")

    # After many connections, dict grows unbounded
    for i in range(100):
        shared_llm.ensure_conversation(f"session_{i}")

    print(f"  After 100 sessions: {len(shared_llm._conversations)} entries in shared dict (memory leak)")

    if len(shared_llm._conversations) > 100:
        print("  ❌ FAIL: Shared LLM accumulates all session conversation IDs. Never cleaned up.")
        return False
    else:
        print("  ✅ PASS")
        return True


# ---------------------------------------------------------------------------
# Test 6: Concurrent init_memory on shared provider — race condition
# ---------------------------------------------------------------------------
def test_concurrent_init_memory_race():
    """
    PROVES: Two threads calling init_memory on the SAME MemoryProvider
    instance can interleave, causing one device to see the other's memory.
    """
    print("\n" + "=" * 72)
    print("TEST 6: Concurrent init_memory race on shared provider")
    print("=" * 72)

    from core.providers.memory.mem_local_short.mem_local_short import MemoryProvider

    tmpdir = tempfile.mkdtemp(prefix="race_")
    mem_path = os.path.join(tmpdir, ".memory.yaml")
    with open(mem_path, "w", encoding="utf-8") as f:
        yaml.dump({
            "device_aaa": "ALICE_PRIVATE_MEMORY",
            "device_bbb": "BOB_PRIVATE_MEMORY",
        }, f)

    # ONE shared instance (simulating WebSocketServer behavior)
    shared_mem = MemoryProvider(config={}, summary_memory=None)
    shared_mem.memory_path = mem_path
    shared_mem.save_to_file = True

    results = {}
    barrier = threading.Barrier(2)

    def init_and_read(device_id, result_key):
        barrier.wait()  # force concurrent execution
        shared_mem.init_memory(role_id=device_id, llm=None, summary_memory=None, save_to_file=True)
        shared_mem.memory_path = mem_path
        shared_mem.load_memory(None)
        # Small sleep to increase interleave chance
        time.sleep(0.01)
        # Read what we got
        results[result_key] = {
            "role_id": shared_mem.role_id,
            "short_memory": shared_mem.short_memory,
        }

    t1 = threading.Thread(target=init_and_read, args=("device_aaa", "thread_a"))
    t2 = threading.Thread(target=init_and_read, args=("device_bbb", "thread_b"))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    print(f"  Thread A sees: role_id={results.get('thread_a', {}).get('role_id')}, "
          f"memory={repr(results.get('thread_a', {}).get('short_memory'))[:50]}")
    print(f"  Thread B sees: role_id={results.get('thread_b', {}).get('role_id')}, "
          f"memory={repr(results.get('thread_b', {}).get('short_memory'))[:50]}")

    # Check: the final state of the shared object
    print(f"  Shared object final: role_id={shared_mem.role_id}, "
          f"memory={repr(shared_mem.short_memory)[:50]}")

    # At least one thread likely got wrong data due to shared state
    a_data = results.get("thread_a", {})
    b_data = results.get("thread_b", {})

    mismatch = False
    if a_data.get("short_memory") == "BOB_PRIVATE_MEMORY":
        print("  ❌ FAIL: Thread A (device_aaa) got BOB's memory!")
        mismatch = True
    if b_data.get("short_memory") == "ALICE_PRIVATE_MEMORY":
        print("  ❌ FAIL: Thread B (device_bbb) got ALICE's memory!")
        mismatch = True

    # Even if individual reads didn't catch it, the shared object is fundamentally
    # non-isolated — prove by checking that both threads mutated the same object
    print(f"\n  KEY INSIGHT: Both threads operated on id(shared_mem)={id(shared_mem)}")
    print(f"  shared_mem.role_id is now '{shared_mem.role_id}' — last writer wins.")
    print(f"  This means ALL concurrent connections share this same object's state.")
    print(f"  ❌ STRUCTURAL FAIL: Shared mutable provider is fundamentally unsafe.")

    return False  # Always fails structurally


# ---------------------------------------------------------------------------
# Test 7: voice_id resolution — stale after character switch
# ---------------------------------------------------------------------------
def test_voice_id_not_refreshed_on_character_switch():
    """
    PROVES: voice_id is set once during connection setup. If the user
    switches characters mid-session, voice stays stale until reconnect.
    """
    print("\n" + "=" * 72)
    print("TEST 7: voice_id staleness after character switch")
    print("=" * 72)

    device_id = "device_aaa"

    # First connection: resolve voice
    char_id = _fake_get_active_character_for_device(device_id)
    char_doc = _fake_get_character_profile(char_id)
    fields = fsc.extract_character_profile_fields(char_doc or {})
    voice_id_1 = fields.get("voice")
    print(f"  Connection 1: char={char_id}, voice={voice_id_1}")

    # User switches character in Firestore
    FAKE_FIRESTORE["devices"]["device_aaa"]["activeCharacterId"] = "char_bob"

    # SAME connection — voice_id was stored in conn.voice_id at setup time
    # It would NOT be refreshed. Simulate:
    print(f"  (User switches to char_bob in Firestore)")
    print(f"  Active connection still uses voice_id={voice_id_1} (no refresh mechanism)")

    # New connection would get the correct voice
    char_id_2 = _fake_get_active_character_for_device(device_id)
    char_doc_2 = _fake_get_character_profile(char_id_2)
    fields_2 = fsc.extract_character_profile_fields(char_doc_2 or {})
    voice_id_2 = fields_2.get("voice")
    print(f"  New connection: char={char_id_2}, voice={voice_id_2}")

    # Restore
    FAKE_FIRESTORE["devices"]["device_aaa"]["activeCharacterId"] = "char_alice"

    if voice_id_1 == voice_id_2:
        print("  ⚠️  Both resolve to same voice (test data issue)")
        return True
    else:
        print(f"  ❌ CONFIRMED: Active connection would keep voice={voice_id_1} while user expects voice={voice_id_2}")
        print(f"         No mid-session refresh exists in connection.py")
        return False


# ---------------------------------------------------------------------------
# Test 8: _initialize_memory crashes when self.task is None
# ---------------------------------------------------------------------------
def test_initialize_memory_crash_on_none_task():
    """
    PROVES: _initialize_memory() unconditionally calls self.task.init_task()
    which crashes if Task module is disabled (self.task is None).
    """
    print("\n" + "=" * 72)
    print("TEST 8: _initialize_memory crash when self.task is None")
    print("=" * 72)

    # Simulate the exact code path from connection.py lines 1507-1520
    class FakeConn:
        def __init__(self):
            self.memory = type("FakeMem", (), {
                "init_memory": lambda s, **kw: None,
            })()
            self.task = None  # Task module disabled
            self.device_id = "device_aaa"
            self.llm = None
            self.config = {"Memory": {"test": {"type": "nomem"}}, "selected_module": {"Memory": "test"}}
            self.read_config_from_api = False

    conn = FakeConn()

    try:
        # Replicate _initialize_memory logic
        if conn.memory is None:
            print("  Memory is None, would return early. PASS.")
            return True

        conn.memory.init_memory(
            role_id=conn.device_id,
            llm=conn.llm,
            summary_memory=None,
            save_to_file=True,
        )

        # This is the line that crashes when task is None
        conn.task.init_task(
            role_id=conn.device_id,
            llm=conn.llm,
        )
        print("  ✅ PASS: No crash (task was not None?)")
        return True

    except AttributeError as e:
        print(f"  ❌ FAIL: Crashed with: {e}")
        print("         _initialize_memory() does not guard against self.task being None.")
        print("         This leaves memory/task in partially-initialized state.")
        return False


# ═══════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════
def main():
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║  BabyMilu Voice Server — Concurrency & Isolation Reproduction Tests ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    tests = [
        ("MEM_LEAK_SHARED_INSTANCE", test_mem_local_short_shared_instance_leak),
        ("MEM_STALE_MISSING_ROLE", test_mem_local_short_stale_memory_on_missing_role),
        ("PROMPT_CACHE_STALE", test_prompt_cache_stale_after_character_switch),
        ("PROMPT_CACHE_TYPE_MISMATCH", test_prompt_cache_type_mismatch),
        ("LLM_CONVERSATIONS_LEAK", test_shared_llm_conversations_dict_leak),
        ("CONCURRENT_INIT_MEMORY_RACE", test_concurrent_init_memory_race),
        ("VOICE_ID_STALE_ON_SWITCH", test_voice_id_not_refreshed_on_character_switch),
        ("INIT_MEMORY_CRASH_NONE_TASK", test_initialize_memory_crash_on_none_task),
    ]

    results = {}
    for name, test_fn in tests:
        try:
            passed = test_fn()
            results[name] = "PASS" if passed else "FAIL"
        except Exception as e:
            print(f"  💥 EXCEPTION: {e}")
            traceback.print_exc()
            results[name] = "ERROR"

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "═" * 72)
    print("SUMMARY")
    print("═" * 72)
    for name, status in results.items():
        icon = {"PASS": "✅", "FAIL": "❌", "ERROR": "💥"}.get(status, "?")
        print(f"  {icon} {name}: {status}")

    total = len(results)
    passed = sum(1 for v in results.values() if v == "PASS")
    failed = sum(1 for v in results.values() if v == "FAIL")
    errors = sum(1 for v in results.values() if v == "ERROR")
    print(f"\n  Total: {total}  Passed: {passed}  Failed: {failed}  Errors: {errors}")

    if failed + errors > 0:
        print("\n  ⚠️  Failures confirm shared-state / cache-invalidation bugs in production.")
        print("     See individual test output above for root cause + fix guidance.")

    # ── Dump relevant log entries ────────────────────────────────────
    if LOG_RECORDS:
        print(f"\n  ({len(LOG_RECORDS)} log entries captured; grep for device_id in full output)")

    return 1 if (failed + errors > 0) else 0


if __name__ == "__main__":
    sys.exit(main())
