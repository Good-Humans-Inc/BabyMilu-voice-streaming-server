# Supabase Chat Logging Setup

This project can now write conversation logs to Supabase using `CHAT_STORE_BACKEND=supabase`.

## 1) Create Tables in Supabase SQL Editor

Run the schema bootstrap SQL from [scripts/bootstrap_memory_schema.sql](/Users/yan/Desktop/BabyMilu/BabyMilu-voice-streaming-server/scripts/bootstrap_memory_schema.sql), or run the backfill script with `--bootstrap-only`.

```sql
-- See scripts/bootstrap_memory_schema.sql for the complete authoritative schema.
```

## 2) Set Server Environment Variables

Set these on the Python server process/container:

```bash
CHAT_STORE_BACKEND=supabase
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>

# Optional
SUPABASE_TIMEOUT_SECONDS=10
SUPABASE_USERS_TABLE=users
SUPABASE_SESSIONS_TABLE=sessions
SUPABASE_TURNS_TABLE=turns
SUPABASE_MEMORY_READ_MODEL_TABLE=memory_read_model
```

## 3) Restart the Python Server

After env changes, restart the server container/process.

## 4) Verify Writes

Look for startup/store logs and then check rows in:

- `public.users`
- `public.sessions`
- `public.turns`
- `public.memory_events`
- `public.memory_read_model`
- `public.memory_jobs`

A new device conversation should create one session row and multiple turn rows.

Current runtime write path is:

- writes: `users`, `sessions`, `turns`
- bootstraps on first user/session: `memory_read_model`
- not yet written by runtime: `memory_events`, `memory_jobs`

The current schema used by the ported chat store expects:

- `sessions.memory_status`
- `sessions.turns` as a JSON array
- `memory_read_model.profile`
- `memory_read_model.active_context`
- `memory_read_model.modality_digests`
- `memory_read_model.prompt_pack`
- `memory_read_model.stats`

If you already created the older schema, rerun the bootstrap SQL. It uses `add column if not exists` so it will repair the missing columns.

## 5) Optional Bootstrap / Backfill Commands

Bootstrap the schema only:

```bash
DATABASE_URL=postgresql://... \
python3 scripts/backfill_memory_from_sqlite.py --bootstrap-only
```

Bootstrap and backfill from the legacy local SQLite DB:

```bash
DATABASE_URL=postgresql://... \
python3 scripts/backfill_memory_from_sqlite.py \
  --sqlite-path /opt/xiaozhi-esp32-server/data/conversations.db
```

## Notes

- If `CHAT_STORE_BACKEND=supabase` is set but Supabase credentials are missing, the server falls back to SQLite and logs a warning.
- Existing Firestore usage for conversation binding/state remains unchanged.
- The Supabase service role key must only be used on backend servers.
