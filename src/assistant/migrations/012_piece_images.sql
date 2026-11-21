-- A draft may carry several pictures (Mike, 21 Nov 2026: "the bot can't make a tweet with multiple images"): their
-- file ids in order. `image_file_id` keeps the first, for a draft read by code from before.
alter table pieces add column image_file_ids jsonb;
