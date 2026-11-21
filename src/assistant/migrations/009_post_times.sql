-- A draft's time to post: the one the bot picked (`post_at`), the app's free times it was offered with
-- (`post_times`, `[{at, label}]`), and when the app scheduled it, once it did (`scheduled_at`).
alter table pieces add column post_at text;
alter table pieces add column post_times jsonb;
alter table pieces add column scheduled_at text;
