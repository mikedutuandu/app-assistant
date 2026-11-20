create table demo_drafts (
    id text primary key,
    workspace text not null,
    text text not null,
    image_key text,
    idem_key text not null,
    created_at timestamptz not null default now(),
    unique (workspace, idem_key)
);
create index demo_drafts_created_at_idx on demo_drafts (created_at);

create table demo_facts (
    id text primary key,
    workspace text not null,
    text text not null,
    draft_id text,
    idem_key text not null,
    created_at timestamptz not null default now(),
    unique (workspace, idem_key, text)
);
create index demo_facts_created_at_idx on demo_facts (created_at);
