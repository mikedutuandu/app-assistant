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
  {
    ...shared,
    entry: { "react/index": "src/react/index.tsx" },
    platform: "browser",
    external: ["react", "react/jsx-runtime"],
    // A client component for Next.js's App Router.
    banner: { js: '"use client";' },
  },
]);
