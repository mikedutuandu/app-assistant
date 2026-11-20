import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { signToken, verifyToken } from "../src/server/index";
import { APP_ID, mint, SECRET } from "./helpers";

const now = () => Math.floor(Date.now() / 1000);
const claims = (over: Record<string, unknown> = {}) => ({
  iss: APP_ID,
  sub: "ws_1",
  access: "draft",
  iat: now(),
  exp: now() + 600,
  ...over,
});

afterEach(() => vi.useRealTimers());

describe("signToken and verifyToken", () => {
  it("round trip", async () => {
    const token = await signToken(SECRET, { appId: APP_ID, workspaceId: "ws_42", access: "draft", ttlSeconds: 900 });
    expect(await verifyToken(SECRET, token, APP_ID, "draft")).toEqual({ workspaceId: "ws_42" });
    expect(await verifyToken(SECRET, token, APP_ID, "delete")).toBeNull();
    expect(await verifyToken("another-secret-not-real", token, APP_ID, "draft")).toBeNull();
    expect(await verifyToken(SECRET, token, "another-app", "draft")).toBeNull();
  });

  it("signs the contract's claims: an hour at most, integer times", async () => {
    const token = await signToken(SECRET, { appId: APP_ID, workspaceId: "ws:7", access: "delete" });
    const payload = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString());
    expect(payload).toEqual({ iss: APP_ID, sub: "ws:7", access: "delete", iat: payload.iat, exp: payload.iat + 3600 });
    expect(Number.isInteger(payload.iat)).toBe(true);
    const header = JSON.parse(Buffer.from(token.split(".")[0], "base64url").toString());
    expect(header).toEqual({ alg: "HS256", typ: "JWT" });
  });

  it("refuses to sign what the bot would refuse", async () => {
    const ok = { appId: APP_ID, workspaceId: "ws_1", access: "draft" as const };
    await expect(signToken(SECRET, { ...ok, ttlSeconds: 3601 })).rejects.toThrow();
    await expect(signToken(SECRET, { ...ok, ttlSeconds: 0 })).rejects.toThrow();
    await expect(signToken(SECRET, { ...ok, workspaceId: "a/b" })).rejects.toThrow();
    await expect(signToken(SECRET, { ...ok, workspaceId: "" })).rejects.toThrow();
    await expect(signToken("", ok)).rejects.toThrow();
    await expect(signToken(SECRET, { ...ok, access: "admin" as "draft" })).rejects.toThrow();
  });

  it("verifies a token minted elsewhere", async () => {
    expect(await verifyToken(SECRET, mint(claims()), APP_ID, "draft")).toEqual({ workspaceId: "ws_1" });
  });

  it.each([
    ["expired", claims({ iat: now() - 7200, exp: now() - 1 })],
    ["longer than an hour", claims({ exp: now() + 3601 })],
    ["another app", claims({ iss: "other" })],
    ["delete access", claims({ access: "delete" })],
    ["no access", claims({ access: undefined })],
    ["no sub", claims({ sub: undefined })],
    ["a sub with a slash", claims({ sub: "ws/1" })],
    ["a sub too long", claims({ sub: "a".repeat(201) })],
    ["no iat", claims({ iat: undefined })],
    ["a string exp", claims({ exp: String(now() + 600) })],
    ["a boolean iat", claims({ iat: true })],
    ["issued in the future", claims({ iat: now() + 120, exp: now() + 600 })],
    ["not yet valid (nbf)", claims({ nbf: now() + 120 })],
  ])("refuses a token: %s", async (_name, c) => {
    expect(await verifyToken(SECRET, mint(c), APP_ID, "draft")).toBeNull();
  });

  it("allows 30 s of clock skew on iat", async () => {
    expect(await verifyToken(SECRET, mint(claims({ iat: now() + 20 })), APP_ID, "draft")).toEqual({
      workspaceId: "ws_1",
    });
  });

  it.each([
    ["alg none", mint(claims(), SECRET, { alg: "none" }).split(".").slice(0, 2).join(".") + "."],
    ["alg HS512 header", mint(claims(), SECRET, { alg: "HS512" })],
    ["another secret", mint(claims(), "another-secret-not-real")],
    ["two parts", mint(claims()).split(".").slice(0, 2).join(".")],
    ["garbage", "not.a.token"],
    ["empty", ""],
    [
      "a tampered payload",
      (() => {
        const [h, , s] = mint(claims()).split(".");
        return `${h}.${Buffer.from(JSON.stringify(claims({ sub: "ws_2" }))).toString("base64url")}.${s}`;
      })(),
    ],
  ])("refuses a malformed or forged token: %s", async (_name, token) => {
    expect(await verifyToken(SECRET, token, APP_ID, "draft")).toBeNull();
  });
});

describe("the bot and the package accept each other's tokens", () => {
  // Written by tests/contract/test_sdk_token_fixture.py from the bot's own mint_session (UPDATE_SDK_FIXTURES=1).
  const bot = JSON.parse(readFileSync(new URL("./fixtures/bot-token.json", import.meta.url), "utf8"));

  it("a token from the bot's mint_session verifies", async () => {
    vi.useFakeTimers({ now: (bot.now + 5) * 1000 });
    expect(await verifyToken(bot.secret, bot.token, bot.app_id, "draft")).toEqual({ workspaceId: bot.workspace });
    expect(await verifyToken(bot.secret, bot.token, bot.app_id, "delete")).toBeNull();
    vi.setSystemTime((bot.claims.exp + 1) * 1000);
    expect(await verifyToken(bot.secret, bot.token, bot.app_id, "draft")).toBeNull();
  });

  // The bot's pytest (test_sdk_token_fixture.py) checks these tokens with verify_session and verify_delete_token.
  // HS256 is deterministic, so this test pins the committed file to what signToken makes; UPDATE_FIXTURES=1 rewrites it.
  it("the package's tokens for the bot are what signToken makes", async () => {
    const url = new URL("./fixtures/package-token.json", import.meta.url);
    const base = { secret: bot.secret, app_id: bot.app_id, workspace: bot.workspace, now: bot.now, ttl_seconds: 3600 };
    vi.useFakeTimers({ now: base.now * 1000 });
    const sign = (access: "draft" | "delete") =>
      signToken(base.secret, { appId: base.app_id, workspaceId: base.workspace, access, ttlSeconds: base.ttl_seconds });
    const made = { ...base, draft: await sign("draft"), delete: await sign("delete") };
    if (process.env.UPDATE_FIXTURES) writeFileSync(url, `${JSON.stringify(made, null, 2)}\n`);
    expect(existsSync(url), "run once with UPDATE_FIXTURES=1 to write it").toBe(true);
    expect(JSON.parse(readFileSync(url, "utf8"))).toEqual(made);
  });
});
