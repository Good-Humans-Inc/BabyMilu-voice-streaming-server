create table if not exists public.users (
  user_id text primary key,
  name text,
  device_ids text
);

create table if not exists public.sessions (
  session_id text primary key,
  user_name text,
  user_id text references public.users(user_id),
  device_id text,
  created_at timestamptz,
  start_time timestamptz,
  end_time timestamptz,
  analysis_status text,
  memory_status text,
  turns jsonb not null default '[]'::jsonb,
  conversation_id text,
  analysis_json jsonb,
  token_usage integer not null default 0,
  last_active_at timestamptz
);

create table if not exists public.turns (
  id bigserial primary key,
  session_id text references public.sessions(session_id) on delete cascade,
  turn_index integer,
  speaker text,
  text text,
  created_at timestamptz not null default now(),
  "timestamp" timestamptz
);

create table if not exists public.memory_events (
  id bigserial primary key,
  user_id text not null references public.users(user_id),
  device_id text,
  session_id text references public.sessions(session_id),
  event_type text not null,
  payload jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.memory_read_model (
  user_id text primary key references public.users(user_id),
  summary text,
  profile jsonb not null default '{}'::jsonb,
  active_context jsonb not null default '{}'::jsonb,
  modality_digests jsonb not null default '{}'::jsonb,
  prompt_pack jsonb not null default '{}'::jsonb,
  stats jsonb not null default '{}'::jsonb,
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

alter table public.users
  add column if not exists name text,
  add column if not exists device_ids text;

alter table public.sessions
  add column if not exists user_name text,
  add column if not exists user_id text references public.users(user_id),
  add column if not exists device_id text,
  add column if not exists created_at timestamptz,
  add column if not exists start_time timestamptz,
  add column if not exists end_time timestamptz,
  add column if not exists analysis_status text,
  add column if not exists memory_status text,
  add column if not exists turns jsonb not null default '[]'::jsonb,
  add column if not exists conversation_id text,
  add column if not exists analysis_json jsonb,
  add column if not exists token_usage integer not null default 0,
  add column if not exists last_active_at timestamptz;

alter table public.turns
  add column if not exists turn_index integer,
  add column if not exists speaker text,
  add column if not exists text text,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists "timestamp" timestamptz;

alter table public.memory_events
  add column if not exists device_id text,
  add column if not exists session_id text references public.sessions(session_id),
  add column if not exists payload jsonb,
  add column if not exists created_at timestamptz not null default now();

alter table public.memory_read_model
  add column if not exists summary text,
  add column if not exists profile jsonb not null default '{}'::jsonb,
  add column if not exists active_context jsonb not null default '{}'::jsonb,
  add column if not exists modality_digests jsonb not null default '{}'::jsonb,
  add column if not exists prompt_pack jsonb not null default '{}'::jsonb,
  add column if not exists stats jsonb not null default '{}'::jsonb,
  add column if not exists updated_at timestamptz not null default now();

alter table public.memory_jobs
  add column if not exists payload jsonb,
  add column if not exists error text,
  add column if not exists created_at timestamptz not null default now(),
  add column if not exists updated_at timestamptz not null default now();

create index if not exists idx_sessions_user_created_at
  on public.sessions(user_id, created_at desc);

create index if not exists idx_sessions_memory_status_created_at
  on public.sessions(memory_status, created_at asc);

create index if not exists idx_turns_session_created_at
  on public.turns(session_id, created_at);

create index if not exists idx_memory_events_user_created_at
  on public.memory_events(user_id, created_at desc);

create index if not exists idx_memory_jobs_status_created_at
  on public.memory_jobs(status, created_at);
