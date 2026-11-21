-- A change the assistant asked the user to confirm (the app's own tool, called only after their tap): what it is,
-- its arguments, and what became of it (waiting, done, failed, cancelled), with the app's answer.
create table pending_actions (
    id text primary key,
    app text not null,
    workspace text not null,
    thread_id text not null,
    message_id text not null,
    tool text not null,
    title text not null,
    arguments jsonb not null,
    lines jsonb not null,
    state text not null default 'waiting',
    result text,
    created_at timestamptz not null default now(),
    done_at timestamptz
);
create index pending_actions_thread on pending_actions (app, workspace, thread_id, created_at);
