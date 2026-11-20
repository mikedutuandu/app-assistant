import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, AuthLost, BotApi, SseParser, type BotEvent } from "../src/api";

function jwt(exp: number): string {
  const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  return `${b64({ alg: "HS256" })}.${b64({ exp })}.sig`;
}
const nowSec = () => Math.floor(Date.now() / 1000);
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
function sse(chunks: string[], status = 200): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream({
    start(c) {
      for (const ch of chunks) c.enqueue(enc.encode(ch));
      c.close();
    },
  });
  return new Response(stream, { status, headers: { "content-type": "text/event-stream" } });
}

type Handler = (url: string, init: RequestInit) => Response | Promise<Response>;
let calls: { url: string; init: RequestInit }[];
let tokens: string[];
let handler: Handler;

beforeEach(() => {
  calls = [];
  tokens = [];
  vi.stubGlobal("fetch", async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    if (url === "/session") {
      const t = tokens.shift() ?? jwt(nowSec() + 3600);
      return json({ token: t });
    }
    return handler(url, init);
  });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const api = () => new BotApi("https://bot.test", "demo", "/session");
const auth = (c: { init: RequestInit }) => (c.init.headers as Record<string, string>).Authorization;

describe("BotApi", () => {
  it("refreshes token once on 401 expired then succeeds", async () => {
    tokens = ["old", "fresh"];
    handler = (_u, init) =>
      (init.headers as Record<string, string>).Authorization === "Bearer old" ? json({ error: "expired" }, 401) : json([{ id: "t1" }]);
    const out = await api().threads();
    expect(out).toEqual([{ id: "t1" }]);
    expect(calls.filter((c) => c.url === "/session")).toHaveLength(2);
    expect(auth(calls[calls.length - 1])).toBe("Bearer fresh");
    expect(calls[0].init.credentials).toBe("include");
    expect(calls[0].init.method).toBe("POST");
  });

  it("does not loop when refresh also 401s", async () => {
    handler = () => json({ error: "expired" }, 401);
    await expect(api().threads()).rejects.toBeInstanceOf(AuthLost);
    expect(calls.filter((c) => c.url !== "/session")).toHaveLength(2);
    expect(calls.filter((c) => c.url === "/session")).toHaveLength(2);
  });

  it("does not retry a 401 that is not expired", async () => {
    handler = () => json({ error: "invalid" }, 401);
    await expect(api().threads()).rejects.toBeInstanceOf(AuthLost);
    expect(calls.filter((c) => c.url !== "/session")).toHaveLength(1);
    expect(calls.filter((c) => c.url === "/session")).toHaveLength(1);
  });

  it("parses SSE events split across chunks", async () => {
    handler = () =>
      sse(['event: thread\ndata: {"thread_id":"a",', '"title":"T"}\n\nevent: te', 'xt\ndata: {"delta":"Hi"}\n', "\n"]);
    const got: BotEvent[] = [];
    await api().send(null, "hello", [], (e) => got.push(e));
    expect(got).toEqual([
      { event: "thread", data: { thread_id: "a", title: "T" } },
      { event: "text", data: { delta: "Hi" } },
    ]);
    const body = JSON.parse(calls[1].init.body as string);
    expect(body).toEqual({ thread_id: null, text: "hello", file_ids: [] });
  });

  it("throws the server's error for a non-2xx before streaming", async () => {
    handler = () => json({ error: "too_long", message: "Too long." }, 400);
    await expect(api().send("t", "x", [], () => {})).rejects.toMatchObject({ code: "too_long", message: "Too long.", status: 400 });
    await expect(api().send("t", "x", [], () => {})).rejects.toBeInstanceOf(ApiError);
  });

  it("caches the token until 60 s before exp, then refreshes", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    const exp = nowSec() + 3600;
    tokens = [jwt(exp), jwt(exp + 3600)];
    handler = () => json([]);
    const a = api();
    await a.threads();
    vi.setSystemTime(Date.now() + (3600 - 61) * 1000);
    await a.threads();
    expect(calls.filter((c) => c.url === "/session")).toHaveLength(1);
    vi.setSystemTime(Date.now() + 2 * 1000); // now 59 s before exp
    await a.threads();
    expect(calls.filter((c) => c.url === "/session")).toHaveLength(2);
  });

  it("add_draft retry reuses the same action_id", async () => {
    let n = 0;
    handler = () => (++n === 1 ? json({ error: "server", message: "x" }, 502) : json({ draft_id: "d", edit_url: null }));
    const a = api();
    const body = { action_id: "abc-1", kind: "add_draft" as const, piece_id: "p1" };
    await expect(a.act("t1", body)).rejects.toBeInstanceOf(ApiError);
    await expect(a.act("t1", body)).resolves.toEqual({ draft_id: "d", edit_url: null });
    const posts = calls.filter((c) => c.url.endsWith("/actions"));
    expect(posts).toHaveLength(2);
    expect(posts[0].url).toBe("https://bot.test/v1/chat/demo/threads/t1/actions");
    expect(JSON.parse(posts[0].init.body as string).action_id).toBe("abc-1");
    expect(JSON.parse(posts[1].init.body as string).action_id).toBe("abc-1");
  });
});

describe("SseParser", () => {
  it("handles several events in one chunk, CRLF, comments and multi-line data", () => {
    const got: BotEvent[] = [];
    const p = new SseParser((e) => got.push(e));
    p.feed(": ping\r\n\r\nevent: note\r\ndata: {\"text\":\"a\"}\r\n\r\nevent: done\ndata: {\"message_id\":\"m\",\ndata: \"cost_usd\":0}\n\n");
    expect(got.map((e) => e.event)).toEqual(["note", "done"]);
    expect(got[1].data).toEqual({ message_id: "m", cost_usd: 0 });
  });
});
