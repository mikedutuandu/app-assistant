/**
 * `@app-assistant/host/view`: draws an app's card in a chat (MCP Apps, the `io.modelcontextprotocol/ui` extension).
 *
 * A read's card is the app's own HTML page (its tool's `_meta.ui.resourceUri`), which the bot sends with the tool's
 * input and result (the `view` event, and a thread read's `views`). The page runs in a sandboxed frame with no origin
 * of its own (`sandbox="allow-scripts"`, no `allow-same-origin`): it can't reach the page around it, its cookies or
 * its storage. A Content-Security-Policy built from the domains the app declared (nothing else) is set first in the
 * page. It talks to the chat only through `postMessage` JSON-RPC, as the MCP Apps spec says: `ui/initialize`, then
 * the tool's input and result, its height, links to open and a message to send.
 */

export interface AppViewData {
  /** The card's id in the thread. */
  id?: string;
  /** The app's tool the card shows. */
  tool: string;
  /** The app's page (`text/html;profile=mcp-app`). */
  html: string;
  /** What the tool was asked. */
  input?: Record<string, unknown>;
  /** What it answered: an MCP `CallToolResult` (`content`, `structuredContent`). */
  result?: Record<string, unknown>;
  /** The domains the page may load from or call (MCP Apps' `_meta.ui.csp`). */
  csp?: { connectDomains?: string[]; resourceDomains?: string[]; frameDomains?: string[]; baseUriDomains?: string[] };
  prefers_border?: boolean;
}

export interface AppViewOptions {
  /** The card asked to send a message as the user (`ui/message`). Without it, such a request is refused. */
  onMessage?: (text: string) => void;
  /** A link the card asked to open; by default a new tab, http(s) only. */
  onOpenLink?: (url: string) => void;
  /** The card's tallest, in pixels (it scrolls inside past that). Default 560. */
  maxHeight?: number;
  locale?: string;
  /** Light or dark, as the card is told (`hostContext.theme`). Default: the device's setting. */
  theme?: "light" | "dark";
}

export interface MountedView {
  frame: HTMLIFrameElement;
  destroy(): void;
}

const APPS_PROTOCOL = "2026-01-26";
const HOST_INFO = { name: "app-assistant", version: "1" };
/** A domain an app may declare: https, a host (a leading `*.` allowed), an optional port. Anything else is dropped. */
const DOMAIN = /^https:\/\/(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(:\d{1,5})?$/i;

const domains = (list: unknown): string[] =>
  Array.isArray(list) ? list.filter((d): d is string => typeof d === "string" && DOMAIN.test(d)).slice(0, 20) : [];

/** The policy the card's page runs under: MCP Apps' default, widened only by the domains the app declared. */
export function cardPolicy(csp: AppViewData["csp"]): string {
  const resources = domains(csp?.resourceDomains);
  const connect = domains(csp?.connectDomains);
  const frames = domains(csp?.frameDomains);
  const list = (base: string[], extra: string[]) => [...base, ...extra].join(" ") || "'none'";
  return [
    "default-src 'none'",
    `script-src ${list(["'unsafe-inline'"], resources)}`,
    `style-src ${list(["'unsafe-inline'"], resources)}`,
    `img-src ${list(["data:", "blob:"], resources)}`,
    `media-src ${list(["data:", "blob:"], resources)}`,
    `font-src ${list(["data:"], resources)}`,
    `connect-src ${list([], connect)}`,
    `frame-src ${list([], frames)}`,
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
  ].join("; ");
}

const escapeAttr = (s: string) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");

/** The page with its policy first: a later policy in the page can only narrow it, never widen it. */
export function sandboxedPage(view: AppViewData): string {
  return `<meta http-equiv="Content-Security-Policy" content="${escapeAttr(cardPolicy(view.csp))}">${view.html}`;
}

const safeUrl = (url: unknown): string | null => {
  if (typeof url !== "string") return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
};

/** The device's light or dark setting (light where it can't be read). */
const deviceTheme = (): "light" | "dark" =>
  typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";

/** Draws `view` in `container`. Call `destroy()` when the card leaves the chat. */
export function mountAppView(container: HTMLElement, view: AppViewData, opts: AppViewOptions = {}): MountedView {
  const maxHeight = opts.maxHeight ?? 560;
  const frame = document.createElement("iframe");
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("title", `${view.tool.replace(/_/g, " ")} card`);
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.setAttribute("loading", "lazy");
  frame.style.cssText = "display:block;width:100%;height:120px;border:0;background:transparent;";
  frame.srcdoc = sandboxedPage(view);
  container.append(frame);

  let ready = false;
  const post = (message: Record<string, unknown>) => frame.contentWindow?.postMessage({ jsonrpc: "2.0", ...message }, "*");
  const answer = (id: unknown, result: unknown) => post({ id, result });
  const refuse = (id: unknown, message: string, code = -32601) => post({ id, error: { code, message } });

  const onMessage = (event: MessageEvent) => {
    if (event.source !== frame.contentWindow || !frame.contentWindow) return;
    const msg = event.data as { jsonrpc?: string; id?: unknown; method?: unknown; params?: any };
    if (!msg || msg.jsonrpc !== "2.0" || typeof msg.method !== "string") return; // answers to us, or noise
    const { id, method, params } = msg;
    const isRequest = id !== undefined && id !== null;
    switch (method) {
      case "ui/initialize":
        if (!isRequest) return;
        answer(id, {
          protocolVersion: typeof params?.protocolVersion === "string" ? params.protocolVersion : APPS_PROTOCOL,
          hostCapabilities: { openLinks: {}, ...(opts.onMessage ? { message: {} } : {}) },
          hostInfo: HOST_INFO,
          hostContext: {
            toolInfo: { tool: { name: view.tool } },
            theme: opts.theme ?? deviceTheme(),
            displayMode: "inline",
            availableDisplayModes: ["inline"],
            containerDimensions: { maxHeight },
            locale: opts.locale ?? (typeof navigator !== "undefined" ? navigator.language : "en"),
            timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            platform: "web",
          },
        });
        return;
      case "ui/notifications/initialized":
        if (ready) return;
        ready = true;
        post({ method: "ui/notifications/tool-input", params: { arguments: view.input ?? {} } });
        post({ method: "ui/notifications/tool-result", params: view.result ?? { content: [] } });
        return;
      case "ui/notifications/size-changed": {
        const h = Number(params?.height);
        if (Number.isFinite(h) && h > 0) frame.style.height = `${Math.min(Math.ceil(h), maxHeight)}px`;
        return;
      }
      case "ui/open-link": {
        const url = safeUrl(params?.url);
        if (!url) return isRequest ? refuse(id, "Only http(s) links can be opened.", -32000) : undefined;
        if (opts.onOpenLink) opts.onOpenLink(url);
        else window.open(url, "_blank", "noopener,noreferrer");
        return isRequest ? answer(id, {}) : undefined;
      }
      case "ui/message": {
        const text = params?.content?.type === "text" ? params.content.text : null;
        if (!opts.onMessage || typeof text !== "string" || !text.trim())
          return isRequest ? refuse(id, "This chat can't take a message from a card.", -32000) : undefined;
        opts.onMessage(text.trim().slice(0, 2000));
        return isRequest ? answer(id, {}) : undefined;
      }
      case "ui/update-model-context":
      case "ping":
        return isRequest ? answer(id, {}) : undefined;
      case "ui/request-display-mode":
        return isRequest ? answer(id, { mode: "inline" }) : undefined;
      default:
        // tools/call, resources/read and anything else: this host doesn't run them for a card (yet).
        return isRequest ? refuse(id, `${method} is not supported here.`) : undefined;
    }
  };
  window.addEventListener("message", onMessage);
  return {
    frame,
    destroy() {
      window.removeEventListener("message", onMessage);
      frame.remove();
    },
  };
}
