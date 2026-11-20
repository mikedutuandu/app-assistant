-- A draft's posts in order (one item for a post); null for pieces written before drafts.
alter table pieces add column posts jsonb;
