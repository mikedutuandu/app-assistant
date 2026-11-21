// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cardPolicy, mountAppView, sandboxedPage, type AppViewData } from "../src/view/index";

// MCP Apps: an app's card (its own HTML page) in a sandboxed frame, talking JSON-RPC over postMessage.
const VIEW: AppViewData = {
  id: "v1",
  tool: "list_posts",
  html: "<div id=card></div>",
  input: { limit: 3 },
  result: { content: [{ type: "text", text: "1 post" }], structuredContent: { posts: [{ text: "hi" }] } },
  csp: { resourceDomains: ["https://pbs.twimg.com", "javascript:alert(1)", "http://plain.example"] },
};

afterEach(() => {
  document.body.innerHTML = "";
});

function mounted(opts = {}) {
  const box = document.createElement("div");
  document.body.append(box);
  const view = mountAppView(box, VIEW, opts);
  const sent: any[] = [];
  vi.spyOn(view.frame.contentWindow!, "postMessage").mockImplementation((m: any) => void sent.push(m));
  const fromCard = (data: unknown) =>
    window.dispatchEvent(new MessageEvent("message", { data, source: view.frame.contentWindow }));
  return { view, sent, fromCard };
}

describe("the card's frame", () => {
  it("is sandboxed with no origin of its own, and its policy comes first and only widens by declared https domains", () => {
    const { view } = mounted();
    expect(view.frame.getAttribute("sandbox")).toBe("allow-scripts");
    const page = sandboxedPage(VIEW);
    expect(page.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true);
    const policy = cardPolicy(VIEW.csp);
    expect(policy).toContain("default-src 'none'");
    expect(policy).toContain("img-src data: blob: https://pbs.twimg.com");
    expect(policy).toContain("connect-src 'none'");
    expect(policy).not.toContain("javascript:");
    expect(policy).not.toContain("http://plain.example");
  });
});

describe("the card's messages", () => {
  it("answers ui/initialize, then sends the tool's input and result once the card says it is ready", () => {
    const { sent, fromCard } = mounted();
    fromCard({ jsonrpc: "2.0", id: 1, method: "ui/initialize", params: { protocolVersion: "2026-01-26" } });
    expect(sent[0]).toMatchObject({ id: 1, result: { protocolVersion: "2026-01-26", hostContext: { displayMode: "inline" } } });
    expect(sent[0].result.hostContext.toolInfo.tool.name).toBe("list_posts");
    fromCard({ jsonrpc: "2.0", method: "ui/notifications/initialized" });
    fromCard({ jsonrpc: "2.0", method: "ui/notifications/initialized" }); // once only
    expect(sent.slice(1)).toEqual([
      { jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: { limit: 3 } } },
      { jsonrpc: "2.0", method: "ui/notifications/tool-result", params: VIEW.result },
    ]);
  });

  it("takes its height (up to the most), opens http(s) links only, and passes a message on", () => {
    const onMessage = vi.fn();
    const onOpenLink = vi.fn();
    const { view, sent, fromCard } = mounted({ onMessage, onOpenLink, maxHeight: 400 });
    fromCard({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { height: 212.4 } });
    expect(view.frame.style.height).toBe("213px");
    fromCard({ jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { height: 9000 } });
    expect(view.frame.style.height).toBe("400px");
    fromCard({ jsonrpc: "2.0", id: 2, method: "ui/open-link", params: { url: "https://x.com/a/status/1" } });
    fromCard({ jsonrpc: "2.0", id: 3, method: "ui/open-link", params: { url: "javascript:alert(1)" } });
    expect(onOpenLink).toHaveBeenCalledTimes(1);
    expect(sent.find((m) => m.id === 3).error.code).toBe(-32000);
    fromCard({ jsonrpc: "2.0", id: 4, method: "ui/message", params: { role: "user", content: { type: "text", text: "Reply to it" } } });
    expect(onMessage).toHaveBeenCalledWith("Reply to it");
  });

  it("refuses what it doesn't run, and ignores messages from anything but its own frame", () => {
    const { sent, fromCard, view } = mounted();
    fromCard({ jsonrpc: "2.0", id: 5, method: "tools/call", params: { name: "delete_post" } });
    expect(sent.find((m) => m.id === 5).error.code).toBe(-32601);
    window.dispatchEvent(
      new MessageEvent("message", { data: { jsonrpc: "2.0", id: 6, method: "ui/initialize" }, source: window }),
    );
    expect(sent.find((m) => m.id === 6)).toBeUndefined();
    view.destroy();
    expect(document.querySelector("iframe")).toBeNull();
  });
});
