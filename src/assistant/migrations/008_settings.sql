-- Small values the bot keeps for itself: today the key that signs its own /v1/files links (the local storage
-- backend), generated once when FILES_SIGNING_KEY is not set.
create table settings (
    key text primary key,
    value text not null
);
