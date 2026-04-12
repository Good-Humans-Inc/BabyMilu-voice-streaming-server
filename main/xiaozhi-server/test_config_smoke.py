#!/usr/bin/env python3
"""
Non-interactive config smoke test.

Validates that data/.config.yaml is correct and all key services are reachable.
Run from inside the container:
    cd /opt/xiaozhi-esp32-server
    python test_config_smoke.py

Exit code 0 = all checks passed (or only warnings).
Exit code 1 = one or more checks failed.
"""
from __future__ import annotations

import sys
import os

# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────

_results: list[tuple[str, str, str]] = []   # (status, label, detail)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

_ICONS = {PASS: "✅", FAIL: "❌", WARN: "⚠️ ", SKIP: "⏭️ "}


def _record(status: str, label: str, detail: str = "") -> None:
    _results.append((status, label, detail))
    icon = _ICONS.get(status, "  ")
    line = f"  {icon}  {label}"
    if detail:
        line += f"  —  {detail}"
    print(line)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Config file presence
# ─────────────────────────────────────────────────────────────────────────────

def _check_config_file() -> dict | None:
    import yaml

    project_dir = os.path.dirname(os.path.abspath(__file__)) + "/"
    custom_path = project_dir + "data/.config.yaml"
    base_path = project_dir + "config.yaml"

    if not os.path.exists(custom_path):
        _record(FAIL, "data/.config.yaml", "file not found")
        return None
    _record(PASS, "data/.config.yaml", "exists")

    try:
        with open(base_path, encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        with open(custom_path, encoding="utf-8") as f:
            custom = yaml.safe_load(f) or {}
    except Exception as e:
        _record(FAIL, "Config YAML parse", str(e))
        return None

    # Simple deep merge
    def _merge(a, b):
        result = dict(a)
        for k, v in b.items():
            result[k] = _merge(result[k], v) if isinstance(v, dict) and isinstance(result.get(k), dict) else v
        return result

    cfg = _merge(base, custom)
    _record(PASS, "Config YAML parse", "OK")
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 2. LLM config + live ping
# ─────────────────────────────────────────────────────────────────────────────

def _check_llm(cfg: dict) -> None:
    selected = cfg.get("selected_module", {}).get("LLM", "")
    llm_cfg = cfg.get("LLM", {}).get(selected, {})

    if not selected or not llm_cfg:
        _record(FAIL, "LLM config", f"selected_module.LLM={selected!r} not found in config")
        return

    api_key = llm_cfg.get("api_key", "")
    model = llm_cfg.get("model_name", "")
    base_url = llm_cfg.get("base_url") or llm_cfg.get("url") or ""

    if not api_key or api_key.startswith("你的") or "your" in api_key.lower():
        _record(FAIL, "LLM api_key", "placeholder value — not set")
        return
    _record(PASS, "LLM config", f"provider={selected}  model={model}")

    # Live call
    try:
        import openai
        client = openai.OpenAI(api_key=api_key, base_url=base_url or None)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Say OK"}],
            max_tokens=5,
        )
        reply = (resp.choices[0].message.content or "").strip()
        _record(PASS, "LLM live ping", f"response: {reply!r}")
    except Exception as e:
        _record(FAIL, "LLM live ping", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Firestore / GCP credentials
# ─────────────────────────────────────────────────────────────────────────────

def _check_firestore() -> None:
    try:
        from config.settings import get_gcp_credentials_path
        creds_path = get_gcp_credentials_path()
    except Exception as e:
        _record(FAIL, "GCP creds lookup", str(e))
        return

    if not creds_path:
        _record(WARN, "GCP credentials", "no credentials found — Firestore will be unavailable")
        return
    _record(PASS, "GCP credentials", creds_path)

    try:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
        from google.cloud import firestore
        client = firestore.Client()
        # Lightweight probe: fetch a non-existent doc (no read cost, just checks auth)
        client.collection("_smoke_test").document("_probe").get(timeout=5.0)
        _record(PASS, "Firestore connection", "authenticated OK")
    except Exception as e:
        err = str(e)
        if "NOT_FOUND" in err or "no document" in err.lower():
            _record(PASS, "Firestore connection", "authenticated OK (doc not found is expected)")
        else:
            _record(FAIL, "Firestore connection", err[:120])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Alarm scheduler imports
# ─────────────────────────────────────────────────────────────────────────────

def _check_alarm_imports() -> None:
    try:
        from services.alarms import firestore_client  # noqa: F401
        _record(PASS, "services.alarms.firestore_client", "import OK")
    except Exception as e:
        _record(FAIL, "services.alarms.firestore_client", str(e)[:120])

    try:
        from services.alarms import scheduler  # noqa: F401
        _record(PASS, "services.alarms.scheduler", "import OK")
    except Exception as e:
        _record(FAIL, "services.alarms.scheduler", str(e)[:120])

    try:
        from services.session_context import store  # noqa: F401
        _record(PASS, "services.session_context.store", "import OK")
    except Exception as e:
        _record(FAIL, "services.session_context.store", str(e)[:120])


# ─────────────────────────────────────────────────────────────────────────────
# 5. schedule_conversation tool sanity
# ─────────────────────────────────────────────────────────────────────────────

def _check_schedule_conversation_tool() -> None:
    try:
        from plugins_func.functions.schedule_conversation import (
            SCHEDULE_CONVERSATION_FUNCTION_DESC,
        )
        name = SCHEDULE_CONVERSATION_FUNCTION_DESC.get("function", {}).get("name", "?")
        _record(PASS, "schedule_conversation tool", f"loaded, name={name!r}")
    except Exception as e:
        _record(FAIL, "schedule_conversation tool", str(e)[:120])


# ─────────────────────────────────────────────────────────────────────────────
# 6. ASR / TTS config presence (no live call — just check config is set)
# ─────────────────────────────────────────────────────────────────────────────

def _check_asr_tts(cfg: dict) -> None:
    for service in ("ASR", "TTS"):
        selected = cfg.get("selected_module", {}).get(service, "")
        if not selected:
            _record(WARN, f"{service} config", "selected_module not set")
            continue
        svc_cfg = cfg.get(service, {}).get(selected, {})
        if not svc_cfg:
            _record(WARN, f"{service} config", f"{selected!r} block missing from config")
        else:
            _record(PASS, f"{service} config", f"provider={selected}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 64)
    print("  Config Smoke Test")
    print("=" * 64)

    print("\n── Config files ──")
    cfg = _check_config_file()
    if cfg is None:
        print("\n[ABORTED: cannot load config]\n")
        sys.exit(1)

    print("\n── LLM ──")
    _check_llm(cfg)

    print("\n── Firestore / GCP ──")
    _check_firestore()

    print("\n── Alarm service imports ──")
    _check_alarm_imports()

    print("\n── Tool imports ──")
    _check_schedule_conversation_tool()

    print("\n── ASR / TTS ──")
    _check_asr_tts(cfg)

    # Summary
    failures = [r for r in _results if r[0] == FAIL]
    warnings = [r for r in _results if r[0] == WARN]
    passed   = [r for r in _results if r[0] == PASS]

    print()
    print("=" * 64)
    print(f"  {len(passed)} passed  |  {len(warnings)} warnings  |  {len(failures)} failed")
    print("=" * 64)

    if failures:
        print()
        print("FAILED checks:")
        for _, label, detail in failures:
            print(f"  ❌  {label}: {detail}")
        print()
        sys.exit(1)
    else:
        print()
        print("All checks passed (or warned only).")
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
