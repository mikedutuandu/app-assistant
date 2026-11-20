-- A demo draft's later posts when it is a thread (text stays the first post); null for a single post.
alter table demo_drafts add column posts jsonb;
