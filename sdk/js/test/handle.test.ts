import { describe, expect, it, vi } from "vitest";
import { createAssistantHost, verifyToken, type AppContext, type HostOptions } from "../src/server/index";
import { APP_ID, matches, type Scenario, SCENARIOS, SECRET, scenarioToken, WORKSPACE } from "./helpers";

const SITE = "https://app.example";
const NO_CREDITS = "You're out of credits. Top up in Billing.";
const PACK = {
  contract: 1,
  id: APP_ID,
  name: "Shipnote",
  api_base: `${SITE}/api/assistant`,
  allowed_origins: [SITE],
  guide: "A good story is one change a user can feel.",
  writes: [{ kind: "x_post", count: 1, max_chars: 280, instructions: "A short post." }],
  models: { chat: "m", writer: "m", main: "m", cheap: "m", vision: "m" },
  message_usd_cap: 0.15,
};

/** An app built from a scenario's `setup`: its profile, its credits, its finished keys and the keys in flight. */
function fakeApp(setup: Scenario["setup"] = {}) {
  const done = new Map((setup.done ?? []).map((d) => [`${d.path}|${d.key}`, d.answer]));
  const inFlight = new Set((setup.in_flight ?? []).map((f) => `${WORKSPACE}/${f.path.slice(1)}/${f.key}`));
  let credits = setup.credits_left ?? null;
  const drafts: unknown[] = [];
  const claimed: string[] = [];
  const released: string[] = [];
  const opts: HostOptions = {
    appId: APP_ID,
    secret: SECRET,
    pack: PACK,
    context: async () => (setup.context ?? { language: "en" }) as unknown as AppContext,
    saveDraft: async (ws, d, key) => {
      const earlier = done.get(`/drafts|${key}`);
      if (earlier) return earlier as { draft_id: string; edit_url: string | null };
      drafts.push({ ws, ...d });
      const id = `d_${drafts.length}`;
      return { draft_id: id, edit_url: `${SITE}/drafts/${id}` };
    },
    saveMaterial: async () => {},
    charge: async (_ws, cost, ref) => {
      if (done.has(`/charge|${ref}`)) return { ok: true };
      if (credits === null) return { ok: true };
      if (credits <= 0) return { outOfCredits: NO_CREDITS };
      credits -= cost;
      return { ok: true };
    },
    once: {
      claim: async (key) => {
        claimed.push(key);
        return !inFlight.has(key);
      },
      release: async (key) => {
        released.push(key);
      },
    },
  };
  return { opts, drafts, claimed, released };
}

function post(path: string, body: unknown, headers: Record<string, string> = {}, token = scenarioToken()) {
  return new Request(`${SITE}/api/assistant${path}`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

describe("every app-side scenario in tests/contract/scenarios.json", () => {
  const appSide = SCENARIOS.filter((s) => s.match !== null && !s.setup.broken);

  it("covers the app's side of the contract", () => {
    expect(appSide.length).toBeGreaterThanOrEqual(25);
  });

  it.each(appSide.map((s) => [s.name, s] as const))("%s", async (_name, s) => {
    const host = createAssistantHost(fakeApp(s.setup).opts);
    const token = scenarioToken(s.setup.token);
    const headers = new Headers();
    for (const [k, v] of Object.entries(s.request.headers)) if (v !== null) headers.set(k, v.replace("<token>", token));
    const res = await host.handle(
      new Request(`${SITE}/api/assistant${s.request.path}`, {
        method: s.request.method,
        headers,
        body: JSON.stringify(s.request.body),
      }),
    );
    expect(res).not.toBeNull();
    expect(res!.status).toBe(s.response_status);
    expect(res!.headers.get("content-type")).toMatch(/^application\/json/);
    const json = await res!.json();
    expect(matches(json, s.match), JSON.stringify(json)).toBe(true);
  });
});

describe("handle", () => {
  it("answers null for paths that aren't its own", async () => {
    const host = createAssistantHost(fakeApp().opts);
    for (const url of [
      "/",
      "/api/assistant",
      "/api/assistant/other",
      "/api/assistantx/context",
      "/api/assistant/context/x",
    ])
      expect(await host.handle(new Request(`${SITE}${url}`, { method: "POST", body: "{}" }))).toBeNull();
    expect(await host.handle(new Request(`${SITE}/.well-known/other.json`))).toBeNull();
  });

  it("serves the pack at /.well-known/app-assistant.json", async () => {
    const host = createAssistantHost(fakeApp().opts);
    const res = (await host.handle(new Request(`${SITE}/.well-known/app-assistant.json`)))!;
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toMatch(/^application\/json/);
    expect(res.headers.get("cache-control")).toContain("max-age=300");
    expect(await res.json()).toEqual(PACK);
  });

  it("has no pack route without a pack", async () => {
    const host = createAssistantHost({ ...fakeApp().opts, pack: undefined });
    expect(await host.handle(new Request(`${SITE}/.well-known/app-assistant.json`))).toBeNull();
  });

  it.each([
    ["no contract", { ...PACK, contract: undefined }],
    ["contract 2", { ...PACK, contract: 2 }],
    ["another app's id", { ...PACK, id: "other" }],
    ["no api_base", { ...PACK, api_base: undefined }],
    ["an api_base that isn't a URL", { ...PACK, api_base: "/api/assistant" }],
    ["no allowed_origins", { ...PACK, allowed_origins: undefined }],
    ["no name", { ...PACK, name: "" }],
    ["no guide", { ...PACK, guide: undefined }],
    ["no writes", { ...PACK, writes: [] }],
    ["no models", { ...PACK, models: undefined }],
    ["no message_usd_cap", { ...PACK, message_usd_cap: 0 }],
  ])("refuses a pack with %s when it is created", (_name, pack) => {
    expect(() => createAssistantHost({ ...fakeApp().opts, pack })).toThrow(/pack/);
  });

  it("answers 405 for another method on its routes", async () => {
    const host = createAssistantHost(fakeApp().opts);
    const res = (await host.handle(new Request(`${SITE}/api/assistant/context`)))!;
    expect(res.status).toBe(405);
    expect(res.headers.get("allow")).toBe("POST");
    const pack = (await host.handle(new Request(`${SITE}/.well-known/app-assistant.json`, { method: "POST" })))!;
    expect(pack.status).toBe(405);
  });

  it("serves under another basePath", async () => {
    const host = createAssistantHost({ ...fakeApp().opts, basePath: "/bot/" });
    const res = (await host.handle(
      new Request(`${SITE}/bot/context`, {
        method: "POST",
        headers: { authorization: `Bearer ${scenarioToken()}` },
        body: "{}",
      }),
    ))!;
    expect(res.status).toBe(200);
    expect(await host.handle(post("/context", {}))).toBeNull();
  });

  it('serves at the root with basePath "/"', async () => {
    const host = createAssistantHost({ ...fakeApp().opts, basePath: "/" });
    const req = new Request(`${SITE}/context`, {
      method: "POST",
      headers: { authorization: `Bearer ${scenarioToken()}` },
      body: "{}",
    });
    expect((await host.handle(req))!.status).toBe(200);
  });

  it("claims each key per workspace and path, and frees it after", async () => {
    const app = fakeApp();
    const host = createAssistantHost(app.opts);
    const res = (await host.handle(
      post("/drafts", { text: "Hi.", thread: [], media: [] }, { "idempotency-key": "add:1" }),
    ))!;
    expect(res.status).toBe(200);
    expect(app.claimed).toEqual([`${WORKSPACE}/drafts/add:1`]);
    expect(app.released).toEqual([`${WORKSPACE}/drafts/add:1`]);
  });

  it("passes the draft and the key to saveDraft", async () => {
    const saveDraft = vi.fn(async () => ({ draft_id: "d_9", edit_url: undefined as unknown as null }));
    const host = createAssistantHost({ ...fakeApp().opts, saveDraft });
    const media = [{ url: "https://bot.example.com/f?sig=1", alt: "", mime: "image/png" }];
    const res = (await host.handle(
      post("/drafts", { text: "One.", thread: ["Two."], media, extra: 1 }, { "idempotency-key": "k1" }),
    ))!;
    expect(await res.json()).toEqual({ draft_id: "d_9", edit_url: null });
    expect(saveDraft).toHaveBeenCalledWith(WORKSPACE, { text: "One.", thread: ["Two."], media }, "k1");
  });

  it("passes facts, the draft id (or undefined) and the key to saveMaterial", async () => {
    const saveMaterial = vi.fn(async () => {});
    const host = createAssistantHost({ ...fakeApp().opts, saveMaterial });
    await host.handle(post("/material", { facts: ["A"], draft_id: "d_1" }, { "idempotency-key": "k1" }));
    await host.handle(post("/material", { facts: ["B"], draft_id: null }, { "idempotency-key": "k2" }));
    await host.handle(post("/material", { facts: [] }, { "idempotency-key": "k3" }));
    expect(saveMaterial.mock.calls).toEqual([
      [WORKSPACE, ["A"], "d_1", "k1"],
      [WORKSPACE, ["B"], undefined, "k2"],
      [WORKSPACE, [], undefined, "k3"],
    ]);
  });

  /** An in-memory `once` as the README asks: an atomic claim that expires on its own after `ttl` ms. */
  function expiringOnce(ttl: number) {
    let now = 0;
    const held = new Map<string, number>();
    return {
      tick: (ms: number) => (now += ms),
      once: {
        claim: async (key: string) => {
          const until = held.get(key);
          if (until !== undefined && until > now) return false;
          held.set(key, now + ttl);
          return true;
        },
        release: async (key: string) => {
          held.delete(key);
        },
      },
    };
  }

  it("a claim left by a request killed before release expires, and then the repeat succeeds", async () => {
    const clock = expiringOnce(120_000);
    let calls = 0;
    const host = createAssistantHost({
      ...fakeApp().opts,
      once: clock.once,
      // The first call never ends: the platform killed the request mid-work, so release never runs.
      saveDraft: async () => (++calls === 1 ? new Promise<never>(() => {}) : { draft_id: "d_1", edit_url: null }),
    });
    const draft = () => post("/drafts", { text: "Hi.", thread: [], media: [] }, { "idempotency-key": "add:1" });
    void host.handle(draft());
    await new Promise((r) => setTimeout(r, 0));
    expect((await host.handle(draft()))!.status).toBe(409);
    clock.tick(120_001);
    const res = (await host.handle(draft()))!;
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ draft_id: "d_1", edit_url: null });
  });

  it("two requests with one key at once: one runs, the other is 409; a finished key replays its answer", async () => {
    const clock = expiringOnce(120_000);
    const saved = new Map<string, { draft_id: string; edit_url: null }>();
    let made = 0;
    const host = createAssistantHost({
      ...fakeApp().opts,
      once: clock.once,
      saveDraft: async (ws, _d, key) => {
        await new Promise((r) => setTimeout(r, 5));
        const earlier = saved.get(`${ws}/${key}`);
        if (earlier) return earlier;
        const answer = { draft_id: `d_${++made}`, edit_url: null };
        saved.set(`${ws}/${key}`, answer);
        return answer;
      },
    });
    const draft = () => post("/drafts", { text: "Hi.", thread: [], media: [] }, { "idempotency-key": "add:1" });
    const [a, b] = await Promise.all([host.handle(draft()), host.handle(draft())]);
    expect([a!.status, b!.status].sort()).toEqual([200, 409]);
    const again = (await host.handle(draft()))!;
    expect(again.status).toBe(200);
    expect(await again.json()).toEqual({ draft_id: "d_1", edit_url: null });
    expect(made).toBe(1);
  });

  it.each([
    ["a saved draft stays 200", "/drafts", { text: "Hi.", thread: [], media: [] }, {}, 200],
    ["out of credits stays 402", "/charge", { cost_usd: 0.01, ref: "r1" }, { credits_left: 0 }, 402],
  ])("a release that fails never changes the answer: %s", async (_name, path, body, setup, status) => {
    const onError = vi.fn();
    const host = createAssistantHost({
      ...fakeApp(setup).opts,
      onError,
      once: { claim: async () => true, release: async () => Promise.reject(new Error("kv is down")) },
    });
    const res = (await host.handle(post(path, body, { "idempotency-key": "k1" })))!;
    expect(res.status).toBe(status);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
    expect(onError.mock.calls[0][1]).toBe(`release ${WORKSPACE}/${path.slice(1)}/k1`);
  });

  it("reports a function's error to onError instead of console.error", async () => {
    const onError = vi.fn();
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const boom = new Error("boom");
    const host = createAssistantHost({ ...fakeApp().opts, onError, context: async () => Promise.reject(boom) });
    expect((await host.handle(post("/context", {})))!.status).toBe(500);
    expect(onError).toHaveBeenCalledWith(boom, "context");
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });

  it("works without once", async () => {
    const host = createAssistantHost({ ...fakeApp().opts, once: undefined });
    const res = (await host.handle(post("/charge", { cost_usd: 0.01, ref: "r1" }, { "idempotency-key": "r1" })))!;
    expect(await res.json()).toEqual({ ok: true });
  });

  it("402 with the app's sentence when out of credits, and frees the key", async () => {
    const app = fakeApp({ credits_left: 0 });
    const host = createAssistantHost(app.opts);
    const res = (await host.handle(post("/charge", { cost_usd: 0.01, ref: "r1" }, { "idempotency-key": "r1" })))!;
    expect(res.status).toBe(402);
    expect(await res.json()).toEqual({ error: "out_of_credits", message: NO_CREDITS });
    expect(app.released).toEqual([`${WORKSPACE}/charge/r1`]);
  });

  it("a function that throws is 500 server with no message, and frees the key", async () => {
    const app = fakeApp();
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const host = createAssistantHost({
      ...app.opts,
      saveDraft: async () => {
        throw new Error("db password is hunter2");
      },
    });
    const res = (await host.handle(
      post("/drafts", { text: "Hi.", thread: [], media: [] }, { "idempotency-key": "k" }),
    ))!;
    expect(res.status).toBe(500);
    const text = await res.text();
    expect(JSON.parse(text)).toEqual({ error: "server" });
    expect(text).not.toContain("hunter2");
    expect(app.released).toEqual([`${WORKSPACE}/drafts/k`]);
    error.mockRestore();
  });

  it.each([
    ["context without a language", { context: async () => ({ name: "Mai" }) as unknown as AppContext }, "/context", {}],
    [
      "saveDraft without a draft_id",
      { saveDraft: async () => ({ id: "d_1" }) as never },
      "/drafts",
      { text: "Hi.", thread: [], media: [] },
    ],
    [
      "charge with neither ok nor outOfCredits",
      { charge: async () => ({}) as never },
      "/charge",
      { cost_usd: 0.01, ref: "r" },
    ],
  ])("an app function's wrong answer is 500 server: %s", async (_name, over, path, body) => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const host = createAssistantHost({ ...fakeApp().opts, ...over });
    const res = (await host.handle(post(path, body, { "idempotency-key": "k" })))!;
    expect(res.status).toBe(500);
    expect(await res.json()).toEqual({ error: "server" });
    error.mockRestore();
  });

  it.each([
    ["a body that isn't JSON", "/drafts", "not json", "k"],
    ["a body that is a list", "/drafts", "[]", "k"],
    ["a context body that isn't JSON", "/context", "{", "k"],
    ["a draft without text", "/drafts", { thread: [], media: [] }, "k"],
    ["a draft of spaces", "/drafts", { text: "  ", thread: [], media: [] }, "k"],
    ["a thread that isn't a list of posts", "/drafts", { text: "Hi.", thread: [1], media: [] }, "k"],
    ["media without a mime", "/drafts", { text: "Hi.", thread: [], media: [{ url: "u", alt: "" }] }, "k"],
    ["no Idempotency-Key on /drafts", "/drafts", { text: "Hi.", thread: [], media: [] }, null],
    ["an Idempotency-Key over 200 characters", "/drafts", { text: "Hi.", thread: [], media: [] }, "k".repeat(201)],
    ["facts that aren't sentences", "/material", { facts: [1] }, "k"],
    ["a draft_id that isn't a string", "/material", { facts: [], draft_id: 3 }, "k"],
    ["no Idempotency-Key on /material", "/material", { facts: [] }, null],
    ["a negative cost", "/charge", { cost_usd: -1, ref: "r" }, "r"],
    ["a cost that is a string", "/charge", { cost_usd: "0.1", ref: "r" }, "r"],
    ["no ref", "/charge", { cost_usd: 0.1 }, "r"],
    ["no Idempotency-Key on /charge", "/charge", { cost_usd: 0.1, ref: "r" }, null],
  ])("400 invalid for %s, and claims nothing", async (_name, path, body, key) => {
    const app = fakeApp();
    const host = createAssistantHost(app.opts);
    const res = (await host.handle(post(path, body, key === null ? {} : { "idempotency-key": key })))!;
    expect(res.status).toBe(400);
    expect((await res.json()).error).toBe("invalid");
    expect(app.claimed).toEqual([]);
  });

  it("checks the token before the body", async () => {
    const host = createAssistantHost(fakeApp().opts);
    const res = (await host.handle(post("/drafts", "not json", {}, scenarioToken("expired"))))!;
    expect(res.status).toBe(401);
    expect(await res.json()).toEqual({ error: "unauthorized" });
    const basic = (await host.handle(post("/context", {}, { authorization: `Basic ${scenarioToken()}` })))!;
    expect(basic.status).toBe(401);
  });

  it("signs tokens for its own app", async () => {
    const host = createAssistantHost(fakeApp().opts);
    const token = await host.signToken("ws_9", "draft", 120);
    expect(await verifyToken(SECRET, token, APP_ID, "draft")).toEqual({ workspaceId: "ws_9" });
    const payload = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
    expect(payload.exp - payload.iat).toBe(120);
  });
});
