/**
 * The app's side of contract 1 (docs/integration.md): `POST {basePath}/context|drafts|material|charge`, checked
 * with the user's token, and the pack at `GET /.well-known/app-assistant.json`. Web Request/Response only.
 */

import { verifyToken } from "./token";

/** What `/context` answers. Only `language` is required. */
export interface AppContext {
  /** An ISO code such as `en` or `vi`. */
  language: string;
  name?: string;
  voice?: string;
  topics?: string[];
  recent_posts?: string[];
  patterns?: string[];
  kept_facts?: { id: string; text: string }[];
  /** In your own unit; `null` or left out when you don't count credits. At 0 or below the turn stops. */
  credits_left?: number | null;
  out_of_credits_message?: string;
}

export interface DraftInput {
  /** The first post. */
  text: string;
  /** The posts after it (`[]` for a single post). */
  thread: string[];
  /** Pictures for the first post: signed links to the bot's storage, valid 5 minutes. Copy them at once. */
  media: { url: string; alt: string; mime: string }[];
}

export interface SavedDraft {
  draft_id: string;
  edit_url: string | null;
}

export type ChargeResult = { ok: true } | { outOfCredits: string };

export interface HostOptions {
  /** Your app id: the bot's registry entry, your pack's `id`, your tokens' `iss`. */
  appId: string;
  /** The shared secret (server-only). */
  secret: string;
  /** The bot's address, for `botFetch`, e.g. `https://bot.example.com`. */
  botUrl?: string;
  /** Where the four endpoints are served. Default `/api/assistant`. */
  basePath?: string;
  /** Your pack, served at `/.well-known/app-assistant.json` (docs/app-assistant.schema.json). */
  pack?: object;
  /** The user's profile for the bot (`POST /context`). Keep it cheap: it runs on every turn. */
  context(workspaceId: string): Promise<AppContext>;
  /**
   * Save a draft the user tapped Add on, never publish it. Same `key` again (per workspace) → the same draft: return
   * the answer you gave the first time.
   */
  saveDraft(workspaceId: string, draft: DraftInput, key: string): Promise<SavedDraft>;
  /** Keep the facts a draft was written from. Same `key` again → do nothing. */
  saveMaterial(workspaceId: string, facts: string[], draftId: string | undefined, key: string): Promise<void>;
  /**
   * Take one model call's cost (US dollars) from the workspace's credits. Same `ref` again → `{ok: true}` with no
   * second charge. No credits left → `{outOfCredits: "<a sentence for the user>"}`.
   */
  charge(workspaceId: string, costUsd: number, ref: string): Promise<ChargeResult>;
  /**
   * Two requests with the same Idempotency-Key at the same time. `claim(key)` must be **atomic** (one winner) and
   * **expire on its own** after at least 60 s (2 minutes is a good choice): it answers false while the key is held
   * (→ 409 `in_flight`). `release(key)` is called when the request ends, done or failed, but it never runs when the
   * request is killed mid-work (a function timeout, a redeploy): without expiry that key would stay held forever and
   * every repeat would get 409. A KV entry or a row with an expiry time works. The key is
   * `{workspace}/{drafts|material|charge}/{Idempotency-Key}`. A request that outlives its claim can release a later
   * holder's claim: harmless, because your functions replay a finished key's answer.
   *
   * Without `once`, two requests with one key can run at the same time: your functions must then be idempotent per
   * key on their own (a unique index on the key, for example).
   */
  once?: { claim(key: string): Promise<boolean>; release?(key: string): Promise<void> };
  /**
   * Errors the package catches: one of your functions threw (`where` is `context`, `drafts`, `material` or `charge`;
   * the bot gets 500 `server`), or `release` failed (`where` is `release <key>`; the answer is unchanged). Default:
   * `console.error`.
   */
  onError?(error: unknown, where: string): void;
}

export const PACK_PATH = "/.well-known/app-assistant.json";
const IN_FLIGHT = "Still saving that. Try again in a moment.";
const MAX_BODY_CHARS = 1_000_000;
const MAX_KEY_CHARS = 200;

type Endpoint = "context" | "drafts" | "material" | "charge";
const ENDPOINTS: readonly string[] = ["context", "drafts", "material", "charge"];

class Refused extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly userMessage?: string,
  ) {
    super(code);
  }
}

/** A function of the app's answered something the contract can't carry: a bug on the app's side, so a 500. */
class WrongAnswer extends Error {}

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

const isObject = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);
const isStrings = (v: unknown): v is string[] => Array.isArray(v) && v.every((s) => typeof s === "string");
const isText = (v: unknown): v is string => typeof v === "string" && v.trim() !== "";

/** The pack's shape, as docs/app-assistant.schema.json requires it. Throws on a pack the bot would refuse. */
export function checkPack(pack: object, appId: string): void {
  const p = pack as Record<string, unknown>;
  const fail = (why: string): never => {
    throw new TypeError(`app-assistant: the pack ${why} (docs/app-assistant.schema.json)`);
  };
  if (!isObject(p)) fail("is not an object");
  if (p.contract !== 1) fail('needs "contract": 1');
  if (typeof p.id !== "string" || !/^[a-z0-9][a-z0-9_-]{0,39}$/.test(p.id)) fail("needs an id: lowercase, up to 40");
  if (p.id !== appId) fail(`id ${JSON.stringify(p.id)} is not appId ${JSON.stringify(appId)}`);
  if (!isText(p.name)) fail("needs a name");
  if (typeof p.api_base !== "string" || !/^https?:\/\/[^/\s]+/.test(p.api_base)) fail("needs api_base, an http(s) URL");
  if (!isStrings(p.allowed_origins) || !p.allowed_origins.every((o) => /^https?:\/\/[^/\s]+\/?$/.test(o)))
    fail("needs allowed_origins, a list of scheme://host[:port]");
  if (!isText(p.guide)) fail("needs a guide");
  if (!Array.isArray(p.writes) || p.writes.length === 0 || !p.writes.every(isObject))
    fail("needs writes, at least one");
  if (!isObject(p.models)) fail("needs models");
  if (typeof p.message_usd_cap !== "number" || !(p.message_usd_cap > 0)) fail("needs message_usd_cap, above 0");
}

export function createHandler(opts: HostOptions): (req: Request) => Promise<Response | null> {
  const trimmed = (opts.basePath ?? "/api/assistant").replace(/^\/+|\/+$/g, "");
  const base = trimmed ? `/${trimmed}` : ""; // "" serves the four at the root: /context, /drafts, ...
  const pack = opts.pack;
  if (pack !== undefined) checkPack(pack, opts.appId);
  const packBody = pack === undefined ? "" : JSON.stringify(pack);

  return async function handle(req: Request): Promise<Response | null> {
    const path = new URL(req.url).pathname;
    if (path === PACK_PATH) {
      if (pack === undefined) return null;
      if (req.method !== "GET" && req.method !== "HEAD") return json(405, { error: "invalid" }, { allow: "GET" });
      return new Response(req.method === "HEAD" ? null : packBody, {
        status: 200,
        headers: { "content-type": "application/json; charset=utf-8", "cache-control": "public, max-age=300" },
      });
    }
    if (!path.startsWith(`${base}/`)) return null;
    const name = path.slice(base.length + 1);
    if (!ENDPOINTS.includes(name)) return null;
    if (req.method !== "POST") return json(405, { error: "invalid" }, { allow: "POST" });
    try {
      return json(200, await endpoint(opts, name as Endpoint, req));
    } catch (e) {
      if (e instanceof Refused)
        return json(
          e.status,
          e.userMessage === undefined ? { error: e.code } : { error: e.code, message: e.userMessage },
        );
      // The app's own error: logged on its server, never sent (it may say anything).
      report(opts, e, name);
      return json(500, { error: "server" });
    }
  };
}

function report(opts: HostOptions, error: unknown, where: string): void {
  try {
    if (opts.onError) return opts.onError(error, where);
  } catch {
    // An onError that throws falls back to the log.
  }
  console.error(`app-assistant: ${where} failed`, error);
}

async function endpoint(opts: HostOptions, name: Endpoint, req: Request): Promise<unknown> {
  const [scheme, token] = (req.headers.get("authorization") ?? "").split(/\s+/, 2);
  const session =
    scheme?.toLowerCase() === "bearer" && token ? await verifyToken(opts.secret, token, opts.appId, "draft") : null;
  if (!session) throw new Refused(401, "unauthorized");
  const ws = session.workspaceId;

  const text = await req.text();
  if (text.length > MAX_BODY_CHARS) throw new Refused(400, "invalid", "The body is too large.");
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    throw new Refused(400, "invalid", "The body is not JSON.");
  }
  if (!isObject(body)) throw new Refused(400, "invalid", "The body must be a JSON object.");

  if (name === "context") {
    const context = await opts.context(ws);
    if (!isObject(context) || !isText(context.language)) throw new WrongAnswer("context() must return {language, ...}");
    return context;
  }

  const key = req.headers.get("idempotency-key") ?? "";
  if (!key || key.length > MAX_KEY_CHARS) throw new Refused(400, "invalid", "Idempotency-Key is missing or too long.");
  const work = prepare(opts, name, ws, body, key);
  const slot = `${ws}/${name}/${key}`;
  if (opts.once && !(await opts.once.claim(slot))) throw new Refused(409, "in_flight", IN_FLIGHT);
  try {
    return await work();
  } finally {
    // A failed release must not turn a saved draft (200) or an out-of-credits answer (402) into a 500.
    await Promise.resolve()
      .then(() => opts.once?.release?.(slot))
      .catch((e) => report(opts, e, `release ${slot}`));
  }
}

/** Checks the body (400 before anything is claimed), then returns the work to do once the key is claimed. */
function prepare(
  opts: HostOptions,
  name: Exclude<Endpoint, "context">,
  ws: string,
  body: Record<string, unknown>,
  key: string,
) {
  if (name === "drafts") {
    const { text, thread, media } = body;
    if (!isText(text)) throw new Refused(400, "invalid", "The draft is empty.");
    if (!isStrings(thread)) throw new Refused(400, "invalid", "thread must be a list of posts.");
    const fields = ["url", "alt", "mime"];
    if (!Array.isArray(media) || !media.every((m) => isObject(m) && fields.every((f) => typeof m[f] === "string")))
      throw new Refused(400, "invalid", "media must be a list of {url, alt, mime}.");
    const draft: DraftInput = {
      text,
      thread,
      media: media.map((m: Record<string, string>) => ({ url: m.url, alt: m.alt, mime: m.mime })),
    };
    return async () => {
      const saved = await opts.saveDraft(ws, draft, key);
      if (!isObject(saved) || !isText(saved.draft_id))
        throw new WrongAnswer("saveDraft() must return {draft_id, edit_url}");
      const editUrl = saved.edit_url ?? null;
      if (editUrl !== null && typeof editUrl !== "string")
        throw new WrongAnswer("saveDraft()'s edit_url is a string or null");
      return { draft_id: saved.draft_id, edit_url: editUrl };
    };
  }
  if (name === "material") {
    const { facts, draft_id } = body;
    if (!isStrings(facts)) throw new Refused(400, "invalid", "facts must be a list of sentences.");
    if (draft_id !== undefined && draft_id !== null && typeof draft_id !== "string")
      throw new Refused(400, "invalid", "draft_id must be a string.");
    return async () => {
      await opts.saveMaterial(ws, facts, draft_id ?? undefined, key);
      return { ok: true };
    };
  }
  const { cost_usd, ref } = body;
  if (typeof cost_usd !== "number" || !Number.isFinite(cost_usd) || cost_usd < 0)
    throw new Refused(400, "invalid", "cost_usd must be a number, 0 or more.");
  if (!isText(ref)) throw new Refused(400, "invalid", "ref is missing.");
  return async () => {
    const result: unknown = await opts.charge(ws, cost_usd, ref);
    if (isObject(result) && result.ok === true) return { ok: true };
    if (isObject(result) && isText(result.outOfCredits)) throw new Refused(402, "out_of_credits", result.outOfCredits);
    throw new WrongAnswer("charge() must return {ok: true} or {outOfCredits: message}");
  };
}
