import { afterEach, describe, expect, it, vi } from "vitest";
import { createAssistantHost, type HostOptions, verifyToken } from "../src/server/index";
import { APP_ID, SECRET } from "./helpers";

const base: HostOptions = {
  appId: APP_ID,
  secret: SECRET,
  botUrl: "https://bot.example.com/",
  context: async () => ({ language: "en" }),
  saveDraft: async () => ({ draft_id: "d", edit_url: null }),
  saveMaterial: async () => {},
  charge: async () => ({ ok: true }),
};

afterEach(() => vi.unstubAllGlobals());

function stubFetch() {
  const fetch = vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("botFetch", () => {
  it("calls the bot as the workspace with a draft token", async () => {
    const fetch = stubFetch();
    const host = createAssistantHost(base);
    const res = await host.botFetch("ws_1", "/v1/chat/shipnote/messages", {
      method: "POST",
      body: JSON.stringify({ text: "hi" }),
    });
    expect(res.status).toBe(200);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("https://bot.example.com/v1/chat/shipnote/messages");
    const headers = new Headers(init!.headers);
    expect(headers.get("content-type")).toBe("application/json");
    const token = headers.get("authorization")!.replace(/^Bearer /, "");
    expect(await verifyToken(SECRET, token, APP_ID, "draft")).toEqual({ workspaceId: "ws_1" });
    expect(init!.method).toBe("POST");
  });

  it("keeps the caller's headers", async () => {
    const fetch = stubFetch();
    await createAssistantHost(base).botFetch("ws_1", "/v1/chat/shipnote/threads", {
      headers: { accept: "text/event-stream", "content-type": "text/plain" },
      body: "x",
      method: "POST",
    });
    const headers = new Headers(fetch.mock.calls[0][1]!.headers);
    expect(headers.get("accept")).toBe("text/event-stream");
    expect(headers.get("content-type")).toBe("text/plain");
  });

  it("uses a delete token for deleting a workspace", async () => {
    const fetch = stubFetch();
    await createAssistantHost(base).botFetch("ws_1", "/v1/apps/shipnote/workspaces/ws_1", { method: "DELETE" });
    const token = new Headers(fetch.mock.calls[0][1]!.headers).get("authorization")!.replace(/^Bearer /, "");
    expect(await verifyToken(SECRET, token, APP_ID, "delete")).toEqual({ workspaceId: "ws_1" });
  });

  it("needs botUrl and a path from the bot's root", async () => {
    stubFetch();
    await expect(createAssistantHost({ ...base, botUrl: undefined }).botFetch("ws_1", "/v1/x")).rejects.toThrow(
      /botUrl/,
    );
    await expect(createAssistantHost(base).botFetch("ws_1", "https://evil.example/v1/x")).rejects.toThrow(/path/);
    await expect(createAssistantHost(base).botFetch("ws_1", "//evil.example/v1/x")).rejects.toThrow(/path/);
  });
});
