# Switching the bot's data to Neon (one time)

Do this on your phone in the Vercel and Neon apps or sites. Do steps 1 to 5 in one sitting. Nothing here needs a secret written in a file.

The bot's data (chats, memory) and uploaded images move from Supabase to Neon, and the function moves to US East. Old Supabase data was test chats only, so it starts fresh.

## Steps

0. **Save the old values.** The live project already has `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` and the five `S3_*` variables (Supabase). Copy their current values into your password manager first: Vercel can't show a Sensitive value again. Then delete `DATABASE_URL` (Settings → **Environment Variables**), so Neon can add its own.
1. **Add Neon.** Vercel → the `app-assistant` project → **Storage** (or Marketplace → **Neon**) → **Create / Install**. Pick the region **AWS US East (N. Virginia)** and connect it to the project, for Production and Preview. Leave the custom environment-variable prefix empty if Vercel allows it; if it requires one, use `NEON` (the bot also reads `NEON_DATABASE_URL` and `NEON_DATABASE_URL_UNPOOLED`). In Settings → **Environment Variables**, check that `DATABASE_URL_UNPOOLED` or `NEON_DATABASE_URL_UNPOOLED` is there and its host is not `supabase`.
2. **Add the image bucket.** Open the Neon console → your project → **Object Storage** → enable it → **Create bucket** named `app-assistant`. Then get the keys: click **Connect** at the top of the Neon sidebar → **Storage** tab → **Parameters only** → **Reveal credential**, and copy `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, `AWS_ACCESS_KEY_ID` (`nak_live_…`) and `AWS_SECRET_ACCESS_KEY` (`nsk_live_…`, keep the prefix). (For a key just for the bot: project **Settings** → **Credentials** → **Create credential** with `storage:read` and `storage:write`; the secret is shown once.)
3. **Edit the storage variables.** Vercel → `app-assistant` → Settings → **Environment Variables**. **Edit** the five that already exist (do not delete them: the bot will not start without them), for Production and Preview (keep the secrets **Sensitive**): `S3_ENDPOINT` (= `AWS_ENDPOINT_URL_S3`), `S3_BUCKET` (`app-assistant`), `S3_ACCESS_KEY` (= `AWS_ACCESS_KEY_ID`), `S3_SECRET_KEY` (= `AWS_SECRET_ACCESS_KEY`), `S3_REGION` (= `AWS_REGION`).
4. **Remove the old ones.** In the same list, delete `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (you saved them in step 0).
5. **Move the function and redeploy.** Settings → **Functions** → **Function Region** → **Washington, D.C. (iad1)**. Do this now, together with the database switch: a bot in iad1 talking to Supabase in Sydney would be slow. Then **Deployments** → the latest one → **Redeploy**.

On start the bot builds its tables in Neon and sets the bucket's CORS rule. It uses `DATABASE_URL_UNPOOLED` (the direct connection) for everything, so `DATABASE_URL` is not used.

## Checks

1. **Logs.** Vercel → the deployment → **Logs**. You should see `uploads: s3` and no error. If you see one warning about CORS, set the bucket's CORS by hand in the Neon console (allow `PUT` and `GET` from your site's address) and redeploy.
2. **Chat.** Open `/demo` on the bot and send a message. A reply should stream in.
3. **Upload.** In the demo, attach an image and send it. It should upload and the bot should see it.
4. **Timing.** In the Vercel logs, search `turn timing`. A line should appear for each chat turn.
5. **HookPlaybook timing.** Once ai-mono PR #1720 is merged, `assistant_tool_timing` lines in HookPlaybook's Cloudflare logs should show `colo` near IAD.
6. **Second upload is refused.** On the real bucket, PUT a second time to the same presigned upload URL (it carries `If-None-Match`). It should be refused. If it is not, note it: that is an accepted risk (only the uploader's own file can be replaced, and the size is still capped). If instead every upload fails, roll back and tell Claude: the write-once header may need to be switched off for Neon.

## Rollback

If something is wrong and you need the old setup back:

1. **Remove Neon from the project.** Vercel → **Storage** → the Neon database → **Projects** → the three dots → **Remove Project Connection**. This removes `DATABASE_URL` and `DATABASE_URL_UNPOOLED` and keeps the database.
2. **Put the Supabase values back** from step 0: `DATABASE_URL` (the Supabase Session pooler string), `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.
3. **Set the five `S3_*` variables back** to the Supabase values you saved. Never just delete them: `S3_BUCKET`, `S3_ACCESS_KEY` and `S3_SECRET_KEY` are required, and the bot will not start without them. If you did not save them, make a new access key in Supabase → Storage → **S3 Connection**.
4. Set **Function Region** back to the Supabase region.
5. **Redeploy**, and check the log says `uploads: supabase`.

## Afterwards

- After a week with no problems, the Supabase project can be paused.
- Follow-up: read the `turn timing` and `assistant_tool_timing` logs, then fix what is slow (target: a tool call under about 300 ms, first words in about 1 s).
