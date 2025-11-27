# Session Context Store

`services.session_context` centralizes the lifecycle of proactive, server-owned
sessions (alarms, reminders, etc.). Each session record is stored under the
`sessionContexts/{deviceId}` collection in Firestore and contains:

- `sessionType` – e.g. `"alarm"` or `"reminder"`.
- `triggeredAt`/`expiresAt` – timestamps used to deduplicate wake-ups.
- `ttlSeconds` – explicit TTL for observability and to recompute expirations.
- `sessionConfig` – arbitrary JSON payload (mode name, overrides, etc.).

Use the module-level helpers in `store.py`:

```python
from datetime import timedelta
from services.session_context import store

session = store.create_session(
    device_id="ABC123",
    session_type="alarm",
    ttl=timedelta(minutes=5),
    session_config={"mode": "morning_alarm"},
)
```

Consumers can call `store.get_session(device_id)` to hydrate active context
and the store automatically deletes expired entries. This shared abstraction
lets any proactive experience reserve a session slot without reinventing
deduplication or TTL semantics.

