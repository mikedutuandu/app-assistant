import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { beforeAll, describe, expect, it } from "vitest";

const root = fileURLToPath(new URL("..", import.meta.url));

// Builds the package, then reads what apps import: ./server must run on Cloudflare Workers, with Web APIs only.
beforeAll(() => {
  execFileSync(process.execPath, [fileURLToPath(new URL("../node_modules/.bin/tsup", import.meta.url))], {
    cwd: root,
    stdio: "pipe",
  });
}, 120_000);

describe("the built ./server bundle", () => {
  const server = () => readFileSync(`${root}/dist/server/index.js`, "utf8");

  it("imports nothing: no node: modules, no require, no Buffer, no process", () => {
    const code = server();
    expect(code).not.toMatch(/node:/);
    expect(code).not.toMatch(/\brequire\s*\(/);
    expect(code).not.toMatch(/\bimport\s*(?:[\w*{][^;]*?\bfrom\s*)?["']/);
    expect(code).not.toMatch(/\bimport\s*\(/);
    expect(code).not.toMatch(/\bBuffer\b/);
    expect(code).not.toMatch(/\bprocess\./);
  });

  it("exports what the README says", async () => {
    const mod = await import(`${root}/dist/server/index.js`);
    expect(Object.keys(mod).sort()).toEqual(["createAssistantHost", "readEvents", "signToken", "verifyAssistantToken", "verifyToken"]);
    const types = readFileSync(`${root}/dist/server/index.d.ts`, "utf8");
    expect(types).toContain("createAssistantHost");
  });

  it("./react imports only react and is a client component", () => {
    const code = readFileSync(`${root}/dist/react/index.js`, "utf8");
    expect(code.trimStart().startsWith('"use client"')).toBe(true);
    const imports = [...code.matchAll(/\bfrom\s*["']([^"']+)["']/g)].map((m) => m[1]);
    expect(imports.length).toBeGreaterThan(0);
    expect(
      imports.every((i) => i === "react" || i === "react/jsx-runtime"),
      imports.join(),
    ).toBe(true);
  });
});
