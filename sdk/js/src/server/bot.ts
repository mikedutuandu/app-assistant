/** Calling the bot from the app's server: `botFetch` with the user's token, `readEvents` for a message's stream. */

import { type Access, signToken } from "./token";

export interface BotEvent {
  /** The event's name (`thread`, `status`, `text`, `draft`, `saved`, `failed`, `done`, ...); `message` if unnamed. */
  event: string;
  /** The event's data, parsed as JSON; the raw text when it isn't JSON. */
  data: unknown;
}

/** Server-side tokens live only as long as one call needs. */
const SERVER_TTL_SECONDS = 600;

export async function botFetch(
  opts: { appId: string; secret: string; botUrl?: string },
  workspaceId: string,
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  if (!opts.botUrl) throw new TypeError("app-assistant: botFetch needs botUrl in createAssistantHost's options");
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//"))
    throw new TypeError(
      `app-assistant: botFetch's path starts with one "/" (the bot's root), got ${JSON.stringify(path)}`,
    );
  const method = (init.method ?? "GET").toUpperCase();
  // The one route that needs a delete token: DELETE /v1/apps/{app}/workspaces/{workspace}.
  const access: Access = method === "DELETE" && path.startsWith("/v1/apps/") ? "delete" : "draft";
  const token = await signToken(opts.secret, {
    appId: opts.appId,
    workspaceId,
    access,
    ttlSeconds: SERVER_TTL_SECONDS,
  });
  const headers = new Headers(init.headers);
  headers.set("authorization", `Bearer ${token}`);
  if (typeof init.body === "string" && !headers.has("content-type")) headers.set("content-type", "application/json");
  return fetch(`${opts.botUrl.replace(/\/+$/, "")}${path}`, { ...init, method, headers });
}

/**
 * The events of a Server-Sent Events body, such as `POST /v1/chat/{app}/messages`'s answer. Follows the SSE spec:
 * LF, CRLF or CR line ends, `data` lines joined with "\n", comments and unknown fields skipped, an event the stream
 * ended in the middle of dropped. Breaking out of the loop cancels the stream.
 */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<BotEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "";
  let data: string[] = [];
  let finished = false;

  function* lines(final: boolean): Generator<string> {
    for (;;) {
      const end = buffer.search(/\r\n|\r|\n/);
      if (end < 0) break;
      // A CR at the very end may be the first half of a CRLF still to come.
      if (buffer[end] === "\r" && end === buffer.length - 1 && !final) break;
      const width = buffer.startsWith("\r\n", end) ? 2 : 1;
      yield buffer.slice(0, end);
      buffer = buffer.slice(end + width);
    }
  }

  function take(line: string): BotEvent | null {
    if (line === "") {
      const out = data.length ? { event: event || "message", data: parse(data.join("\n")) } : null;
      event = "";
      data = [];
      return out;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
    return null;
  }

  try {
    for (;;) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      for (const line of lines(done)) {
        const out = take(line);
        if (out) yield out;
      }
      if (done) {
        finished = true;
        return;
      }
    }
  } finally {
    if (finished) reader.releaseLock();
    else await reader.cancel().catch(() => {});
  }
}

function parse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
