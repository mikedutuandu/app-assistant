-- What the bot saved from a turn ("Saved: ..."), so a reload shows it again; and which draft an app draft came from.
create table saved_lines (
    id text primary key,
    app text not null,
    workspace text not null,
    thread_id text not null references threads (id) on delete cascade,
    message_id text not null,
    items jsonb not null,
    created_at timestamptz not null default now()
);
create index saved_lines_app_workspace_thread_idx on saved_lines (app, workspace, thread_id);

alter table pieces add column added_draft_id text;
alter table pieces add column edit_url text;
