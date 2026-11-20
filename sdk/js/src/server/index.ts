/**
 * `@app-assistant/host/server`: an app's side of the app-assistant contract (docs/integration.md) on Web
 * Request/Response, for Cloudflare Workers, Next.js, Hono, Deno and Node 20+. Web Crypto and fetch only.
 */

import { type BotEvent, botFetch, readEvents } from "./bot";
import { createHandler, type HostOptions } from "./handle";
import { type Access, signToken } from "./token";

export type { BotEvent } from "./bot";
export type { AppContext, ChargeResult, DraftInput, HostOptions, SavedDraft } from "./handle";
export type { Access, TokenClaims } from "./token";
export { readEvents, signToken };
export { verifyToken } from "./token";

export interface AssistantHost {
  /** Your routes' answer, or `null` when the request isn't for `{basePath}/*` or the pack: pass it on. */
  handle(req: Request): Promise<Response | null>;
  /** A token for a signed-in user's workspace (`draft` to chat, `delete` to delete their data), 1 hour by default. */
  signToken(workspaceId: string, access: Access, ttlSeconds?: number): Promise<string>;
  /** `fetch(botUrl + path)` as the workspace: adds `Authorization: Bearer <a fresh token>`. */
  botFetch(workspaceId: string, path: string, init?: RequestInit): Promise<Response>;
  /** The events of a chat message's Server-Sent Events answer. */
  readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<BotEvent>;
}

export function createAssistantHost(opts: HostOptions): AssistantHost {
  if (typeof opts.appId !== "string" || opts.appId === "") throw new TypeError("app-assistant: appId is empty");
  if (typeof opts.secret !== "string" || opts.secret === "") throw new TypeError("app-assistant: secret is empty");
  return {
    handle: createHandler(opts),
    signToken: (workspaceId, access, ttlSeconds) =>
      signToken(opts.secret, { appId: opts.appId, workspaceId, access, ttlSeconds }),
    botFetch: (workspaceId, path, init) => botFetch(opts, workspaceId, path, init),
    readEvents,
  };
}
