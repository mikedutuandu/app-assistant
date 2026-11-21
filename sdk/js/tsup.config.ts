import { defineConfig } from "tsup";

const shared = {
  format: ["esm" as const],
  dts: true,
  target: "es2022",
  sourcemap: false,
  splitting: false,
  clean: false,
};

// prebuild empties dist/. ./server runs anywhere (Workers, Node, Deno): platform "neutral", no dependencies.
export default defineConfig([
  { ...shared, entry: { "server/index": "src/server/index.ts" }, platform: "neutral" },
  // ./view: an app's card (MCP Apps) in a sandboxed frame, for a chat that isn't React.
  { ...shared, entry: { "view/index": "src/view/index.ts" }, platform: "browser" },
  {
    ...shared,
    entry: { "react/index": "src/react/index.tsx" },
    platform: "browser",
    external: ["react", "react/jsx-runtime"],
    // A client component for Next.js's App Router.
    banner: { js: '"use client";' },
  },
]);
