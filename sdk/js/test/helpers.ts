/** Test-only helpers: a JWT minted with Node's own HMAC (independent of the package) and the scenarios' `match`. */
import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";

export const SECRET = "test-secret-not-real-0000000000000000000000000000000000000000000";
export const APP_ID = "shipnote";
export const WORKSPACE = "ws_1";

const b64url = (s: string | Buffer) => Buffer.from(s).toString("base64url");

export function mint(
  claims: Record<string, unknown>,
  secret = SECRET,
  header: Record<string, unknown> = { alg: "HS256", typ: "JWT" },
): string {
  const head = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(claims))}`;
  return `${head}.${createHmac("sha256", secret).update(head).digest("base64url")}`;
}

/** A token as a scenario's `setup.token` names it: valid, expired, wrong_secret, wrong_iss, delete_access, too_long. */
export function scenarioToken(kind = "valid"): string {
  const now = Math.floor(Date.now() / 1000);
  const [iat, exp] =
    kind === "expired" ? [now - 7200, now - 3600] : kind === "too_long" ? [now, now + 3700] : [now, now + 600];
  return mint(
    {
      iss: kind === "wrong_iss" ? "another-app" : APP_ID,
      sub: WORKSPACE,
      access: kind === "delete_access" ? "delete" : "draft",
      iat,
      exp,
    },
    kind === "wrong_secret" ? "another-secret-not-real-000000000000000000000000000000000000000" : SECRET,
  );
}

/** docs/integration.md, "Checking your app": an answer against a scenario's `match`. */
export function matches(answer: unknown, template: unknown): boolean {
  if (template === "<string>") return typeof answer === "string" && answer !== "";
  if (template === "<string|null>") return answer === null || typeof answer === "string";
  if (Array.isArray(template))
    return (
      Array.isArray(answer) && answer.length === template.length && template.every((t, i) => matches(answer[i], t))
    );
  if (template !== null && typeof template === "object") {
    if (answer === null || typeof answer !== "object" || Array.isArray(answer)) return false;
    const a = answer as Record<string, unknown>;
    return Object.entries(template).every(([k, v]) => k in a && matches(a[k], v));
  }
  return answer === template;
}

export interface Scenario {
  name: string;
  setup: {
    token?: string;
    context?: Record<string, unknown>;
    credits_left?: number | null;
    done?: { path: string; key: string; answer: unknown }[];
    in_flight?: { path: string; key: string }[];
    broken?: boolean;
    app_only?: boolean;
    schedule_note?: string;
  };
  request: { method: string; path: string; headers: Record<string, string | null>; body: unknown };
  response_status: number | null;
  match: unknown;
}

export const SCENARIOS: Scenario[] = JSON.parse(
  readFileSync(new URL("../../../tests/contract/scenarios.json", import.meta.url), "utf8"),
);
