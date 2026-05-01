# Supabase Chat Logging Setup

This project can now write conversation logs to Supabase using `CHAT_STORE_BACKEND=supabase`.

## 1) Create Tables in Supabase SQL Editor

Run this SQL in your Supabase project:

```sql
create table if not exists public.users (
  user_id text primary key,
  name text,
  device_ids text
);

create table if not exists public.sessions (
  session_id text primary key,
  user_name text,
  user_id text,
  device_id text,
  created_at timestamptz not null default now(),
  start_time timestamptz,
  end_time timestamptz,
  analysis_status text,
  conversation_id text,
  analysis_json text,
  token_usage integer not null default 0,
  last_active_at timestamptz,
  constraint sessions_user_fk foreign key (user_id) references public.users(user_id)
);

create table if not exists public.turns (
  id bigserial primary key,
  session_id text,
  turn_index integer,
  speaker text,
  text text,
  created_at timestamptz not null default now(),
  "timestamp" timestamptz
);

create table if not exists public.user_memory_events (
  id bigserial primary key,
  user_id text not null references public.users(user_id),
  device_id text,
  session_id text references public.sessions(session_id),
  event_type text not null,
  payload jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.user_memory_model (
  user_id text primary key references public.users(user_id),
  summary text,
  profile jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists public.memory_jobs (
  id bigserial primary key,
  user_id text not null references public.users(user_id),
  session_id text references public.sessions(session_id),
  status text not null,
  payload jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_sessions_user_created_at on public.sessions(user_id, created_at desc);
create index if not exists idx_turns_session_created_at on public.turns(session_id, created_at);
create index if not exists idx_user_memory_events_user_created_at on public.user_memory_events(user_id, created_at desc);
create index if not exists idx_memory_jobs_status_created_at on public.memory_jobs(status, created_at);
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
SUPABASE_MAX_RETRIES=2
SUPABASE_RETRY_BACKOFF_SECONDS=0.5
SUPABASE_USERS_TABLE=users
SUPABASE_SESSIONS_TABLE=sessions
SUPABASE_TURNS_TABLE=turns
<<<<<<< HEAD
=======
SUPABASE_MEMORY_READ_MODEL_TABLE=memory_read_model
<<<<<<< HEAD
>>>>>>> origin/main
=======
SUPABASE_CHARACTER_MEMORY_TABLE=character_memory_model
>>>>>>> 3e77efa270f10262e4f09dced04f21342d627fee
```

## 3) Restart the Python Server

After env changes, restart the server container/process.

## 4) Verify Writes

Look for startup/store logs and then check rows in:

- `public.users`
- `public.sessions`
- `public.turns`
<<<<<<< HEAD
- `public.user_memory_events`
- `public.user_memory_model`
=======
- `public.memory_events`
- `public.memory_read_model`
<<<<<<< HEAD
>>>>>>> origin/main
=======
- `public.character_memory_model`
>>>>>>> 3e77efa270f10262e4f09dced04f21342d627fee
- `public.memory_jobs`

A new device conversation should create one session row and multiple turn rows.

Current runtime write path is:

- writes: `users`, `sessions`, `turns`
<<<<<<< HEAD
- not yet written by runtime: `user_memory_events`, `user_memory_model`, `memory_jobs`
=======
- bootstraps on first user/session: `memory_read_model`
- bootstraps on first active character/session: `character_memory_model`
- not yet written by runtime: `memory_events`, `memory_jobs`

The current schema used by the ported chat store expects:

- `sessions.memory_status`
- `sessions.turns` as a JSON array
- `memory_read_model.profile`
- `memory_read_model.active_context`
- `memory_read_model.modality_digests`
- `memory_read_model.prompt_pack`
- `memory_read_model.stats`
- `character_memory_model.memory_state`
- `character_memory_model.next_starter`

Character-scoped behavior:

- `next_starter` is stored by `character_id`, not `user_id`
- switching `activeCharacterId` on a device must reload the matching `character_memory_model` row
- the runtime should not migrate or replay a starter across different characters

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
>>>>>>> origin/main

## Notes

- If `CHAT_STORE_BACKEND=supabase` is set but Supabase credentials are missing, the server falls back to SQLite and logs a warning.
- Existing Firestore usage for conversation binding/state remains unchanged.
- The Supabase service role key must only be used on backend servers.
