-- A card an app's tool showed in the chat (MCP Apps: the tool's `_meta.ui.resourceUri`, a `ui://` page the app
-- serves): the page as it was then, the tool's input and its result, so a thread read draws it again.
create table app_views (
    id text primary key,
    app text not null,
    workspace text not null,
    thread_id text not null references threads (id) on delete cascade,
    message_id text not null,
    tool text not null,
    html text not null,
    csp jsonb,
    prefers_border boolean,
    input jsonb not null,
    result jsonb not null,
    created_at timestamptz not null default now()
);
create index app_views_thread on app_views (app, workspace, thread_id, created_at);

-- A change asked in a thread goes with the thread (and so with its workspace), as its other rows do.
delete from pending_actions where thread_id not in (select id from threads);
alter table pending_actions
    add constraint pending_actions_thread_fk foreign key (thread_id) references threads (id) on delete cascade;
