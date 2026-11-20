-- The last good copy of each registered app's served pack (apps/registry.yaml), so a cold instance can still chat
-- while the app's pack route is down. `pack` is the JSON as the app served it; the owner's ceilings apply on read.
create table remote_packs (
    app text primary key,
    pack jsonb not null,
    fetched_at timestamptz not null
);
