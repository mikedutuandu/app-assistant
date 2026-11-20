create table pieces (
    id text primary key,
    app text not null,
    workspace text not null,
    thread_id text not null references threads (id) on delete cascade,
    message_id text not null,
    spec_kind text not null,
    text text not null,
    image_file_id text,
    fact_ids jsonb not null,
    facts jsonb not null,
    dropped text,
    created_at timestamptz not null default now()
);
create index pieces_app_workspace_thread_idx on pieces (app, workspace, thread_id);
