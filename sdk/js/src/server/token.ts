/**
 * Session tokens (docs/integration.md, "Tokens"): HS256 JWTs with `iss` = app id, `sub` = workspace,
 * `access` = "draft" | "delete", `exp - iat` at most an hour. Web Crypto only, so it runs on Workers, Node and Deno.
 */

export type Access = "draft" | "delete";

export interface TokenClaims {
  appId: string;
  workspaceId: string;
  access: Access;
  /** How long the token lives, 1 to 3600 seconds. Default 3600. */
  ttlSeconds?: number;
}

/** The longest a token may live. */
export const MAX_TTL_SECONDS = 3600;
/** Clock skew allowed on `iat` (and `nbf`) only; expiry is checked strictly, as the bot does. */
const LEEWAY_SECONDS = 30;
/** A workspace id: it becomes part of the bot's storage keys. */
const WORKSPACE_ID = /^[A-Za-z0-9:_.-]{1,200}$/;
const ACCESS: readonly string[] = ["draft", "delete"];

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64url(text: string): Uint8Array<ArrayBuffer> | null {
  if (!/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) return null;
  const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function parseJson(bytes: Uint8Array | null): Record<string, unknown> | null {
  if (!bytes) return null;
  try {
    const value: unknown = JSON.parse(decoder.decode(bytes));
    return value !== null && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function hmacKey(secret: string, usage: "sign" | "verify"): Promise<CryptoKey> {
  if (typeof secret !== "string" || secret === "") throw new TypeError("app-assistant: the shared secret is empty");
  return crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [usage]);
}

const isTime = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n);
const nowSeconds = () => Math.floor(Date.now() / 1000);

/** A token for `workspaceId`, signed with the app's shared secret. Sign on your server only. */
export async function signToken(secret: string, claims: TokenClaims): Promise<string> {
  const { appId, workspaceId, access, ttlSeconds = MAX_TTL_SECONDS } = claims;
  if (typeof appId !== "string" || appId === "") throw new TypeError("app-assistant: appId is empty");
  if (typeof workspaceId !== "string" || !WORKSPACE_ID.test(workspaceId))
    throw new TypeError("app-assistant: a workspace id is 1-200 characters of A-Z a-z 0-9 : _ . -");
  if (!ACCESS.includes(access)) throw new TypeError('app-assistant: access is "draft" or "delete"');
  if (!Number.isInteger(ttlSeconds) || ttlSeconds < 1 || ttlSeconds > MAX_TTL_SECONDS)
    throw new RangeError(`app-assistant: ttlSeconds is a whole number from 1 to ${MAX_TTL_SECONDS}`);
  const key = await hmacKey(secret, "sign");
  const iat = nowSeconds();
  const head = [
    { alg: "HS256", typ: "JWT" },
    { iss: appId, sub: workspaceId, access, iat, exp: iat + ttlSeconds },
  ]
    .map((part) => base64url(encoder.encode(JSON.stringify(part))))
    .join(".");
  const signature = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(head)));
  return `${head}.${base64url(signature)}`;
}

/**
 * The workspace of a token, checked exactly as the bot checks it: the signature with `secret`, `iss` is `appId`,
 * `access` is `access`, not expired, `exp - iat` at most an hour, a well-formed `sub`. `null` for any other token.
 */
export async function verifyToken(
  secret: string,
  token: string,
  appId: string,
  access: Access,
): Promise<{ workspaceId: string } | null> {
  if (typeof token !== "string") return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const header = parseJson(fromBase64url(parts[0]));
  const signature = fromBase64url(parts[2]);
  if (!header || header.alg !== "HS256" || !signature) return null;
  const key = await hmacKey(secret, "verify");
  const valid = await crypto.subtle.verify("HMAC", key, signature, encoder.encode(`${parts[0]}.${parts[1]}`));
  if (!valid) return null;

  const claims = parseJson(fromBase64url(parts[1]));
  if (!claims) return null;
  const { iss, sub, iat, exp, nbf } = claims;
  const now = Date.now() / 1000;
  if (!isTime(iat) || !isTime(exp) || typeof iss !== "string" || typeof sub !== "string") return null;
  if (!WORKSPACE_ID.test(sub)) return null;
  if (exp <= now || iat > now + LEEWAY_SECONDS) return null;
  if (nbf !== undefined && (!isTime(nbf) || nbf > now + LEEWAY_SECONDS)) return null;
  if (iss !== appId || claims.access !== access) return null;
  if (exp - iat > MAX_TTL_SECONDS) return null;
  return { workspaceId: sub };
}

/**
 * The bot's token on a request to your own MCP server (the pack's `actions`): the assistant calls it as the signed-in
 * user with `Authorization: Bearer <token>`, the same token the four endpoints get. Returns that user's workspace,
 * or `null` for no token or any other token. Give the assistant only the tools you want it to have, as you would for
 * a key; it runs a tool that isn't read-only only after the user taps Confirm in the chat.
 */
export async function verifyAssistantToken(
  request: Request,
  secret: string,
  appId: string,
): Promise<{ workspaceId: string } | null> {
  const [scheme, token] = (request.headers.get("authorization") ?? "").split(" ");
  if (scheme?.toLowerCase() !== "bearer" || !token) return null;
  return verifyToken(secret, token, appId, "draft");
}
