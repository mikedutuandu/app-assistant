import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "../src/index";

function script(attrs: Record<string, string>): HTMLScriptElement {
  const s = document.createElement("script");
  s.src = "https://bot.test/widget.js";
  for (const [k, v] of Object.entries(attrs)) s.setAttribute(k, v);
  return s;
}
const panelOf = () =>
  document.getElementById("assistant-widget")!.shadowRoot!.querySelector(".panel") as HTMLElement;

beforeEach(() => {
  document.body.innerHTML = "";
  sessionStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe("mount", () => {
  it("opens the panel when the page dispatches assistant:open", () => {
    mount(script({ "data-app": "hookplaybook", "data-session-url": "/api/assistant/session" }));
    expect(panelOf().hidden).toBe(true);
    window.dispatchEvent(new CustomEvent("assistant:open"));
    expect(panelOf().hidden).toBe(false);
  });

  it("listens once: a second mount on the same page adds no second panel", () => {
    mount(script({ "data-app": "hookplaybook" }));
    mount(script({ "data-app": "hookplaybook" }));
    expect(document.querySelectorAll("#assistant-widget")).toHaveLength(1);
    window.dispatchEvent(new CustomEvent("assistant:open"));
    expect(panelOf().hidden).toBe(false);
  });

  it("does nothing without data-app", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    mount(script({}));
    window.dispatchEvent(new CustomEvent("assistant:open"));
    expect(document.getElementById("assistant-widget")).toBeNull();
    warn.mockRestore();
  });
  it("the open button says what it is by default: Write with AI", () => {
    mount(script({ "data-app": "demo" }));
    const launcher = document.getElementById("assistant-widget")!.shadowRoot!.querySelector(".launcher")!;
    expect(launcher.textContent).toBe("Write with AI");
  });
});

