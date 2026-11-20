# Switching the bot's data to Neon (one time)

Do this on your phone in the Vercel and Neon apps or sites. Do steps 1 to 5 in one sitting. Nothing here needs a secret written in a file.

The bot's data (chats, memory) and uploaded images move from Supabase to Neon, and the function moves to US East. Old Supabase data was test chats only, so it starts fresh.

## Steps

1. **Add Neon.** Vercel → the `app-assistant` project → **Storage** (or Marketplace → **Neon**) → **Create / Install**. Pick the region **AWS US East (N. Virginia)** and connect it to the project, for Production and Preview. Vercel adds `DATABASE_URL` and `DATABASE_URL_UNPOOLED` by itself. Check both are in Settings → **Environment Variables**.
2. **Add the image bucket.** Open the Neon console → your project → **Object Storage** → enable it → **Create bucket** named `app-assistant` → create a **credential** (access key). Copy the endpoint, region, access key and secret.
3. **Add the storage variables.** Vercel → `app-assistant` → Settings → **Environment Variables**. Add, for Production and Preview (mark the secrets **Sensitive**): `S3_ENDPOINT`, `S3_BUCKET` (`app-assistant`), `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`.
4. **Remove the old ones.** In the same list, delete `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.
5. **Move the function and redeploy.** Settings → **Functions** → **Function Region** → **Washington, D.C. (iad1)**. Do this now, together with the database switch: a bot in iad1 talking to Supabase in Sydney would be slow. Then **Deployments** → the latest one → **Redeploy**.

On start the bot builds its tables in Neon and sets the bucket's CORS rule. It uses `DATABASE_URL_UNPOOLED` (the direct connection) for everything, so `DATABASE_URL` is not used.

## Checks

1. **Logs.** Vercel → the deployment → **Logs**. You should see `uploads: s3` and no error. If you see one warning about CORS, set the bucket's CORS by hand in the Neon console (allow `PUT` and `GET` from your site's address) and redeploy.
2. **Chat.** Open `/demo` on the bot and send a message. A reply should stream in.
3. **Upload.** In the demo, attach an image and send it. It should upload and the bot should see it.
4. **Timing.** In the Vercel logs, search `turn timing`. A line should appear for each chat turn.
5. **HookPlaybook timing.** Once ai-mono PR #1720 is merged, `assistant_tool_timing` lines in HookPlaybook's Cloudflare logs should show `colo` near IAD.
6. **Second upload is refused.** On the real bucket, PUT a second time to the same presigned upload URL (it carries `If-None-Match`). It should be refused. If it is not, note it: that is an accepted risk (only the uploader's own file can be replaced, and the size is still capped).

## Rollback

If something is wrong and you need the old setup back:

1. Put the Supabase variables back: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, and `DATABASE_URL` set to the Supabase Session pooler string.
2. Remove `DATABASE_URL_UNPOOLED` and the `S3_*` variables.
3. Set **Function Region** back to the Supabase region.
4. **Redeploy.**

## Afterwards

- After a week with no problems, the Supabase project can be paused.
- Follow-up: read the `turn timing` and `assistant_tool_timing` logs, then fix what is slow (target: a tool call under about 300 ms, first words in about 1 s).
