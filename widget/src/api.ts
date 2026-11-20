/** The bot's HTTP API: session tokens, SSE over fetch, XHR uploads, actions and reads. */

export interface Fact {
  id: string;
  text: string;
  numbers: string[];
  source: string;
  file_id: string | null;
}
export interface Piece {
  id: string;
  spec_kind: string;
  text: string;
  image_file_id: string | null;
  fact_ids: string[];
  dropped: string | null;
}
export type BotEvent =
  | { event: "thread"; data: { thread_id: string; title: string } }
  | { event: "transcript"; data: { file_id: string; text: string } }
  | { event: "text"; data: { delta: string } }
  | { event: "card"; data: { pieces: Piece[]; facts: Fact[] } }
  | { event: "note"; data: { text: string } }
  | { event: "done"; data: { message_id: string; cost_usd: number } }
  | { event: "failed"; data: { error: string; message: string } }
  | { event: string; data: any };

export type ActionBody =
  | { action_id: string; kind: "add_draft"; piece_id: string }
  | { action_id: string; kind: "keep_facts"; facts: Fact[] };

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
/** The session can't be renewed: the panel shows "Please sign in again." and stops. */
export class AuthLost extends Error {
  constructor() {
    super("Please sign in again.");
  }
}

const REFRESH_MARGIN_MS = 60_000;
const APP_UNREACHABLE = "Couldn't reach the app. Try again.";
const FALLBACK_TTL_MS = 5 * 60_000;

function tokenExpiryMs(token: string): number {
  try {
    const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const exp = JSON.parse(atob(part + "=".repeat((4 - (part.length % 4)) % 4))).exp;
    if (typeof exp === "number") return exp * 1000;
  } catch {
    /* not a readable JWT: use a short fallback */
  }
  return Date.now() + FALLBACK_TTL_MS;
}

/** Server-sent events, fed text chunks that may split anywhere. */
export class SseParser {
  private buf = "";
  constructor(private onEvent: (e: BotEvent) => void) {}

  feed(chunk: string): void {
    this.buf += chunk;
    for (;;) {
      const m = /\r\n\r\n|\n\n|\r\r/.exec(this.buf);
      if (!m) return;
      const block = this.buf.slice(0, m.index);
      this.buf = this.buf.slice(m.index + m[0].length);
      this.emit(block);
    }
  }

  end(): void {
    if (this.buf.trim()) this.emit(this.buf);
    this.buf = "";
  }

  private emit(block: string): void {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split(/\r\n|\n|\r/)) {
      if (!line || line.startsWith(":")) continue;
      const i = line.indexOf(":");
      const field = i < 0 ? line : line.slice(0, i);
      const value = i < 0 ? "" : line.slice(i + 1).replace(/^ /, "");
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (!data.length) return;
    try {
      this.onEvent({ event, data: JSON.parse(data.join("\n")) } as BotEvent);
    } catch {
      /* a malformed event is skipped */
    }
  }
}

interface Raw {
  status: number;
  json(): Promise<any>;
}

async function errorOf(res: Raw): Promise<ApiError> {
  let body: any = {};
  try {
    body = await res.json();
  } catch {
    /* no JSON body */
  }
  return new ApiError(res.status, body?.error ?? "server", body?.message ?? "Something went wrong. Please try again.");
}

export class BotApi {
  private token: { value: string; until: number } | null = null;
  private pending: Promise<string> | null = null;
  private base: string;

  constructor(
    base: string,
    private app: string,
    private sessionUrl: string,
  ) {
    this.base = base.replace(/\/+$/, "");
  }

  private url(path: string): string {
    return `${this.base}/v1/chat/${this.app}${path}`;
  }

  private async getToken(fresh = false): Promise<string> {
    if (!fresh && this.token && Date.now() < this.token.until) return this.token.value;
    if (this.pending) return this.pending;
    this.pending = (async () => {
      try {
        let res: Response;
        try {
          // A JSON body, so an app's session route that only takes JSON (HookPlaybook answers 415 otherwise) accepts it.
          res = await fetch(this.sessionUrl, {
            method: "POST",
            credentials: "include",
            headers: { "content-type": "application/json" },
            body: "{}",
          });
        } catch {
          throw new ApiError(0, "network", APP_UNREACHABLE); // offline, or the app is down: try again later
        }
        // Only the app saying "not signed in" ends the session; anything else may pass.
        if (res.status === 401 || res.status === 403) throw new AuthLost();
        if (!res.ok) throw new ApiError(res.status, "app_down", APP_UNREACHABLE);
        let token: unknown;
        try {
          token = (await res.json())?.token;
        } catch {
          token = undefined; // e.g. redirected to a sign-in page
        }
        if (typeof token !== "string" || !token) throw new AuthLost();
        this.token = { value: token, until: tokenExpiryMs(token) - REFRESH_MARGIN_MS };
        return token;
      } catch (e) {
        this.token = null;
        throw e instanceof AuthLost || e instanceof ApiError ? e : new AuthLost();
      } finally {
        this.pending = null;
      }
    })();
    return this.pending;
  }

  /** Runs a call with a token; one retry with a fresh token only for a 401 `{"error":"expired"}`. */
  private async authed<T extends Raw>(call: (token: string) => Promise<T>): Promise<T> {
    let res = await call(await this.getToken());
    if (res.status !== 401) return res;
    let body: any = null;
    try {
      body = await res.json();
    } catch {
      /* ignore */
    }
    if (body?.error !== "expired") throw new AuthLost();
    this.token = null;
    res = await call(await this.getToken(true));
    if (res.status === 401) throw new AuthLost();
    return res;
  }

  private async request(method: string, path: string, body?: unknown): Promise<any> {
    const res = await this.authed((token) =>
      fetch(this.url(path), {
        method,
        headers: { Authorization: `Bearer ${token}`, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      }),
    );
    if (!res.ok) throw await errorOf(res);
    return res.json();
  }

  /** Streams one message's events. `onAccepted` runs once the server has taken the message, before any event. */
  async send(
    threadId: string | null,
    text: string,
    fileIds: string[],
    onEvent: (e: BotEvent) => void,
    onAccepted?: () => void,
  ): Promise<void> {
    const res = await this.authed((token) =>
      fetch(this.url("/messages"), {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ thread_id: threadId, text, file_ids: fileIds }),
      }),
    );
    if (!res.ok) throw await errorOf(res);
    if (!res.body) throw new ApiError(res.status, "server", "Something went wrong. Please try again.");
    onAccepted?.();
    const parser = new SseParser(onEvent);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    parser.feed(decoder.decode());
    parser.end();
  }

  async upload(
    threadId: string | null,
    file: File,
    onProgress: (fraction: number) => void,
  ): Promise<{ file_id: string; kind: string; thread_id: string }> {
    const xhrCall = (token: string) =>
      new Promise<Raw & { text: string }>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", this.url("/files"));
        xhr.setRequestHeader("Authorization", `Bearer ${token}`);
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable && e.total > 0) onProgress(e.loaded / e.total);
        };
        xhr.onerror = () => reject(new ApiError(0, "network", "Couldn't upload. Check your connection."));
        xhr.onload = () =>
          resolve({
            status: xhr.status,
            text: xhr.responseText,
            json: async () => JSON.parse(xhr.responseText),
          });
        const form = new FormData();
        form.append("file", file, file.name);
        if (threadId) form.append("thread_id", threadId);
        xhr.send(form);
      });
    const res = await this.authed(xhrCall);
    if (res.status < 200 || res.status >= 300) throw await errorOf(res);
    return res.json();
  }

  act(threadId: string, body: ActionBody): Promise<any> {
    return this.request("POST", `/threads/${encodeURIComponent(threadId)}/actions`, body);
  }

  threads(): Promise<{ id: string; title: string; updated_at: string }[]> {
    return this.request("GET", "/threads");
  }

  thread(id: string): Promise<{ messages: { role: "user" | "assistant"; text: string; card?: any }[]; pieces: any[] }> {
    return this.request("GET", `/threads/${encodeURIComponent(id)}`);
  }
}
