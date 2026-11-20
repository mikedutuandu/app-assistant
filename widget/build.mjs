import { build } from "esbuild";

await build({
  entryPoints: ["src/index.ts"],
  outfile: "../src/assistant/static/widget.js",
  bundle: true,
  format: "iife",
  minify: true,
  target: "es2020",
  legalComments: "none",
});
console.log("built ../src/assistant/static/widget.js");
