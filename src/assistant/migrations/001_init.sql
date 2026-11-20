create table threads (
    id text primary key,
    app text not null,
    workspace text not null,
    title text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index threads_app_workspace_updated_idx on threads (app, workspace, updated_at desc);

create table files (
    id text primary key,
    app text not null,
    workspace text not null,
    thread_id text references threads (id) on delete cascade,
    kind text not null,
    mime text not null,
    bytes int not null,
    storage_key text not null,
    reading jsonb,
    created_at timestamptz not null default now(),
    expires_at timestamptz
);
create index files_app_workspace_idx on files (app, workspace);
create index files_expires_at_idx on files (expires_at) where expires_at is not null;

create table counters (
    key text primary key,
    value numeric not null,
    window_ends timestamptz not null
);

create table actions (
    id text primary key,
    app text not null,
    workspace text not null,
    thread_id text references threads (id) on delete cascade,
    kind text not null,
    result jsonb not null,
    created_at timestamptz not null default now()
);
create index actions_app_workspace_idx on actions (app, workspace);
