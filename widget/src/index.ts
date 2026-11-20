/** The one script tag any app adds: `<script src="https://bot/widget.js" data-app="…" data-session-url="…">`. */
import { BotApi } from "./api";
import { createPanel } from "./panel";

function mount(script: HTMLScriptElement) {
  const app = script.dataset.app;
  if (!app) {
    console.warn("assistant widget: data-app is missing");
    return;
  }
  if (document.getElementById("assistant-widget")) return;
  const base = new URL(script.src, location.href).origin; // the bot's own origin; the session URL is the host's
  const api = new BotApi(base, app, script.dataset.sessionUrl || "/api/assistant/session");
  createPanel({
    api,
    app,
    label: script.dataset.label || "Write with me",
    position: script.dataset.position === "left" ? "left" : "right",
  });
}

const script = (document.currentScript as HTMLScriptElement | null) ?? document.querySelector<HTMLScriptElement>("script[data-app][src*='widget.js']");
if (script) {
  if (document.body) mount(script);
  else document.addEventListener("DOMContentLoaded", () => mount(script));
}
