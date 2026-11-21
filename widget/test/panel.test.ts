import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, AuthLost, type BotEvent } from "../src/api";
import { createPanel, type PanelApi } from "../src/panel";

const draft = { id: "d1", kind: "x_post", posts: ["Dark mode is live."], image_file_id: null };
const thread = {
  id: "d2",
  kind: "x_thread",
  posts: ["First, the hook.", "Second, the number."],
};

function setup(over: Partial<PanelApi> = {}, events: BotEvent[] = []) {
  const api = {
    send: vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => events.forEach(on)),
    upload: vi.fn(async () => ({ file_id: "f", kind: "image", thread_id: "t1" })),
    act: vi.fn(async () => ({ draft_id: "d1", edit_url: "https://app.test/edit/d1" })),
    thread: vi.fn(async () => ({ messages: [] })),
    ...over,
  } as unknown as PanelApi & { send: any; act: any; thread: any; upload: any };
  const p = createPanel({ api, app: "demo", label: "Write with me", position: "right" });
  p.open();
  const $ = (sel: string) => p.root.querySelector(sel) as HTMLElement;
  const $$ = (sel: string) => Array.from(p.root.querySelectorAll(sel)) as HTMLElement[];
  const flush = () => new Promise((r) => setTimeout(r, 0));
  const say = async (text: string) => {
    ($("textarea") as HTMLTextAreaElement).value = text;
    $(".send").click();
    await flush();
  };
  return { api, p, $, $$, say, flush };
}

beforeEach(() => {
  document.body.innerHTML = "";
  sessionStorage.clear();
  vi.stubGlobal("crypto", {}); // plain-http pages have no crypto.randomUUID: the panel must not need it
});
afterEach(() => vi.unstubAllGlobals());

describe("panel", () => {
  // Mike, 21 Nov 2026: typing "c" in Ask StreakPlan opened the board's New video dialog. The page's shortcuts saw the
  // key with the widget's host element as its target (a shadow root hides the textarea), so "not while typing" missed.
  it("keys typed in the chat stay in the chat: the page's shortcuts never see them, Enter still sends", async () => {
    const seen: string[] = [];
    const onWindow = (e: Event) => seen.push(`window:${(e as KeyboardEvent).key}`);
    const onDocument = (e: Event) => seen.push(`document:${(e as KeyboardEvent).key}`);
    window.addEventListener("keydown", onWindow);
    document.addEventListener("keydown", onDocument);
    window.addEventListener("keyup", onWindow);
    try {
      const { $, api, flush } = setup();
      const box = $("textarea") as HTMLTextAreaElement;
      for (const type of ["keydown", "keypress", "keyup"])
        box.dispatchEvent(new KeyboardEvent(type, { key: "c", bubbles: true, composed: true }));
      expect(seen).toEqual([]);
      box.value = "hello";
      box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, composed: true, cancelable: true }));
      await flush();
      expect(api.send).toHaveBeenCalledTimes(1);
      expect(seen).toEqual([]);
    } finally {
      window.removeEventListener("keydown", onWindow);
      document.removeEventListener("keydown", onDocument);
      window.removeEventListener("keyup", onWindow);
    }
  });

  it("streams text, shows transcript, and dispatches assistant:note", async () => {
    const notes: string[] = [];
    window.addEventListener("assistant:note", (e) => notes.push((e as CustomEvent).detail.text));
    const { $, $$, say } = setup({}, [
      { event: "thread", data: { thread_id: "t9", title: "x" } },
      { event: "transcript", data: { file_id: "v", text: "shipped it" } },
      { event: "text", data: { delta: "Nice. " } },
      { event: "text", data: { delta: "How many users?" } },
      { event: "note", data: { text: "Checking your app" } },
    ]);
    await say("shipped dark mode");
    expect($$(".bot")[0].textContent).toBe("Nice. How many users?");
    expect($(".heard").textContent).toBe("Heard: shipped it");
    expect(notes).toEqual(["Checking your app"]);
    expect(sessionStorage.getItem("assistant:thread:demo")).toBe("t9");
    expect($(".list").getAttribute("aria-live")).toBe("polite");
  });

  const thr = (id = "t1") => ({ event: "thread", data: { thread_id: id, title: "" } }) as BotEvent;
  const dr = (d: any = draft) => ({ event: "draft", data: d }) as BotEvent;

  it("renders a draft with its three actions", async () => {
    const { $$, say } = setup({}, [thr(), dr()]);
    await say("hi");
    const el = $$(".draft")[0];
    expect(el.querySelector(".post")!.textContent).toBe("Dark mode is live.");
    expect(Array.from(el.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Save draft", "Shorter", "Another angle"]);
  });

  it("renders a thread as numbered posts", async () => {
    const { $$, say } = setup({}, [thr(), dr(thread)]);
    await say("hi");
    const posts = $$(".draft")[0].querySelectorAll("ol > li");
    expect(Array.from(posts).map((l) => l.textContent)).toEqual(["First, the hook.", "Second, the number."]);
  });

  it("Shorter sends the message with the draft id", async () => {
    const { $$, say, flush, api } = setup({}, [thr(), dr()]);
    await say("hi");
    ($$(".draft button")[1] as HTMLButtonElement).click();
    await flush();
    expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send.mock.calls[1][1]).toBe("Make it shorter");
    expect(api.send.mock.calls[1][2]).toEqual([]);
    expect(api.send.mock.calls[1][5]).toBe("d1");
    ($$(".draft button")[2] as HTMLButtonElement).click();
    await flush();
    expect(api.send.mock.calls[2][1]).toBe("Try another angle");
    expect(api.send.mock.calls[2][5]).toBe("d1");
  });

  it("the draft's actions are disabled while a turn runs", async () => {
    let finish!: () => void;
    const send = vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => {
      if (send.mock.calls.length === 1) return [thr(), dr()].forEach(on);
      await new Promise<void>((r) => (finish = r));
    });
    const { $$, say, flush } = setup({ send } as any);
    await say("hi");
    const [add, shorter] = $$(".draft button") as HTMLButtonElement[];
    shorter.click();
    await flush();
    expect(add.disabled && shorter.disabled).toBe(true);
    shorter.click();
    expect(send).toHaveBeenCalledTimes(2);
    finish();
    await flush();
    expect(add.disabled || shorter.disabled).toBe(false);
  });

  it("Save draft becomes Saved with an Edit link", async () => {
    const saved: string[] = [];
    window.addEventListener("assistant:saved", (e) => saved.push((e as CustomEvent).detail.draft_id));
    let release!: () => void;
    const act = vi.fn(() => new Promise((r) => (release = () => r({ draft_id: "sch1", edit_url: "https://app.test/edit/d1" }))));
    const { $$, say, flush } = setup({ act } as any, [thr(), dr()]);
    await say("hi");
    const add = $$(".draft button")[0] as HTMLButtonElement;
    expect($$(".draft")[0].querySelector("a")).toBeNull();
    add.click();
    expect(add.disabled).toBe(true);
    release();
    await flush();
    expect(add.textContent).toBe("Saved");
    expect(saved).toEqual(["sch1"]);
    const edit = $$(".draft")[0].querySelector("a") as HTMLAnchorElement;
    expect(edit.href).toBe("https://app.test/edit/d1");
    expect(edit.textContent).toBe("Edit");
    expect(act).toHaveBeenCalledWith("t1", { action_id: "add:d1", kind: "add_draft", draft_id: "d1" });
  });

  it("the Add button's spoken name starts with its visible text and follows it once added", async () => {
    let release!: () => void;
    const act = vi.fn(() => new Promise((r) => (release = () => r({ draft_id: "s", edit_url: null }))));
    const { $$, say, flush } = setup({ act } as any, [thr(), dr()]);
    await say("hi");
    const add = $$(".draft button")[0] as HTMLButtonElement;
    const label = () => add.getAttribute("aria-label") ?? "";
    expect(label().startsWith(add.textContent!)).toBe(true);
    add.click();
    expect(label()).toMatch(/^Saving/);
    release();
    await flush();
    expect(add.textContent).toBe("Saved");
    expect(label().startsWith("Saved")).toBe(true);
    expect(label()).not.toMatch(/^Save /);
  });

  it("no Edit link when edit_url is null; retry after failure reuses the action id", async () => {
    const act = vi.fn().mockRejectedValueOnce(new ApiError(502, "app_down", "The app couldn't be reached.")).mockResolvedValueOnce({ draft_id: "s", edit_url: null });
    const { $$, say, flush } = setup({ act } as any, [thr(), dr()]);
    await say("hi");
    const add = $$(".draft button")[0] as HTMLButtonElement;
    add.click();
    await flush();
    expect(add.disabled).toBe(false);
    expect($$(".draft")[0].textContent).toContain("couldn't be reached");
    add.click();
    await flush();
    expect(act.mock.calls[0][1].action_id).toBe(act.mock.calls[1][1].action_id);
    expect($$(".draft")[0].querySelector("a")).toBeNull();
    expect(add.textContent).toBe("Saved");
  });

  it("renders a saved line quietly", async () => {
    const { $$, say } = setup({}, [thr(), { event: "saved", data: { kind: "facts", items: ["Shipped dark mode.", "120 users"] } } as BotEvent]);
    await say("hi");
    const line = $$(".saved")[0];
    expect(line.textContent).toContain("Shipped dark mode; 120 users");
    expect(line.textContent).not.toMatch(/[.;]{2}/);
    expect(line.querySelector("svg")).not.toBeNull();
    expect(line.querySelector("button, input")).toBeNull();
    expect(line.classList.contains("msg")).toBe(false);
  });

  it("no facts box or Keep button anywhere", async () => {
    const { $$, say } = setup({}, [thr(), dr(), { event: "saved", data: { kind: "facts", items: ["A"] } } as BotEvent]);
    await say("hi");
    expect($$(".facts, .fact, .card, .piece")).toHaveLength(0);
    expect($$("button").some((b) => /keep|what i used/i.test(b.textContent ?? ""))).toBe(false);
  });

  it("disables send while a turn streams", async () => {
    let finish!: () => void;
    const send = vi.fn(() => new Promise<void>((r) => (finish = r)));
    const { $, say, flush } = setup({ send } as any);
    await say("one");
    expect(($(".send") as HTMLButtonElement).disabled).toBe(true);
    await say("two");
    expect(send).toHaveBeenCalledTimes(1);
    finish();
    await flush();
    expect(($(".send") as HTMLButtonElement).disabled).toBe(false);
  });

  it("shows 'Please sign in again.' on AuthLost and stops", async () => {
    const send = vi.fn().mockRejectedValue(new AuthLost());
    const { $, say } = setup({ send } as any);
    await say("hello");
    expect($(".err").textContent).toBe("Please sign in again.");
    expect(($(".send") as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the server's message on a file chip, and Escape closes", async () => {
    const upload = vi.fn().mockRejectedValue(new ApiError(400, "kind", "PDFs aren't accepted here."));
    const { $, $$, p, flush } = setup({ upload } as any);
    const input = $("input[type=file]") as HTMLInputElement;
    expect(input.accept).toBe("image/*,application/pdf,audio/*");
    Object.defineProperty(input, "files", { value: [new File(["x"], "a.pdf", { type: "application/pdf" })] });
    input.dispatchEvent(new Event("change"));
    await flush();
    expect($(".chip").textContent).toContain("PDFs aren't accepted here.");
    expect($$(".chip button")[0].getAttribute("aria-label")).toBe("Remove a.pdf");
    $(".panel").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(($(".panel") as HTMLElement).hidden).toBe(true);
    void p;
  });

  it("reopens a stored thread from history", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t7");
    const thread = vi.fn(async () => ({ messages: [{ role: "user", text: "hi" }, { role: "assistant", text: "Hello" }] }));
    const { $$, flush } = setup({ thread } as any);
    await flush();
    expect(thread).toHaveBeenCalledWith("t7");
    expect($$(".msg").map((m) => m.textContent)).toEqual(["hi", "Hello"]);
  });

  it("reload renders drafts from the thread", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t1");
    const thread = vi.fn(async () => ({
      messages: [
        { role: "user", text: "hi" },
        {
          role: "assistant",
          text: "Here you go.",
          drafts: [
            { id: "d1", kind: "x_post", posts: ["One."], added: false, edit_url: null },
            { id: "d2", kind: "x_thread", posts: ["A", "B"], added: true, edit_url: "https://app.test/e/2" },
          ],
          saved: ["Shipped dark mode"],
        },
      ],
    }));
    const { $$, flush, api } = setup({ thread } as any);
    await flush();
    expect($$(".bot")[0].textContent).toBe("Here you go.");
    expect($$(".draft")).toHaveLength(2);
    expect($$(".draft")[1].querySelectorAll("li")).toHaveLength(2);
    expect($$(".draft")[1].querySelector("a")!.getAttribute("href")).toBe("https://app.test/e/2");
    expect(($$(".draft")[1].querySelector("button") as HTMLButtonElement).textContent).toBe("Saved");
    // In the order the turn showed them live (21 Nov 2026: a reload drew the question above the draft): what it kept,
    // the drafts, then its words.
    const order = $$(".saved, .draft, .msg.bot, .msg.user").map((e) => e.className.split(" ").pop());
    expect(order).toEqual(["user", "saved", "draft", "draft", "bot"]);
    expect($$(".saved")[0].textContent).toContain("Shipped dark mode");
    ($$(".draft")[0].querySelector("button") as HTMLButtonElement).click();
    await flush();
    expect(api.act.mock.calls[0]).toEqual(["t1", { action_id: "add:d1", kind: "add_draft", draft_id: "d1" }]);
  });


  it("keeps the text and files when the message is refused before it streams", async () => {
    const send = vi.fn().mockRejectedValueOnce(new ApiError(0, "network", "Couldn't reach the app. Try again."));
    const { $, $$, say, flush } = setup({ send } as any);
    const input = $("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["x"], "shot.png", { type: "image/png" })], configurable: true });
    input.dispatchEvent(new Event("change"));
    await flush();
    await say("shipped dark mode");
    expect(($("textarea") as HTMLTextAreaElement).value).toBe("shipped dark mode");
    expect($$(".chip").map((c) => c.querySelector(".name")!.textContent)).toEqual(["shot.png"]);
    expect($$(".msg.user")).toHaveLength(0);
    expect($(".err").textContent).toBe("Couldn't reach the app. Try again.");
    expect(($(".send") as HTMLButtonElement).disabled).toBe(false);
    send.mockImplementationOnce(async () => {});
    $(".send").click();
    await flush();
    expect(send.mock.calls[1].slice(0, 3)).toEqual(["t1", "shipped dark mode", ["f"]]);
    expect(($("textarea") as HTMLTextAreaElement).value).toBe("");
    expect($$(".chip")).toHaveLength(0);
  });

  it("does not put the text back once the server accepted the message", async () => {
    const send = vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => {
      on({ event: "thread", data: { thread_id: "t1", title: "" } });
      throw new ApiError(0, "network", "Lost the connection.");
    });
    const { $, $$, say } = setup({ send } as any);
    await say("hello");
    expect(($("textarea") as HTMLTextAreaElement).value).toBe("");
    expect($$(".msg.user").map((m) => m.textContent)).toEqual(["hello"]);
  });

  it("shows that the answer was cut off when the stream ends early", async () => {
    const send = vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => {
      on({ event: "text", data: { delta: "Reading your PDF" } });
      throw new ApiError(200, "cut_off", "The answer was cut off. Try again.");
    });
    const { $, $$, say } = setup({ send } as any);
    await say("here is my pdf");
    expect($(".err").textContent).toBe("The answer was cut off. Try again.");
    expect($$(".msg.user").map((m) => m.textContent)).toEqual(["here is my pdf"]);
    expect(($(".send") as HTMLButtonElement).disabled).toBe(false);
  });

  it("keeps the stored thread when reopening it fails for a passing reason", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t7");
    const thread = vi.fn().mockRejectedValue(new ApiError(0, "network", "Couldn't reach the app. Try again."));
    const { $, flush } = setup({ thread } as any);
    await flush();
    expect($(".err").textContent).toBe("Couldn't reach the app. Try again.");
    expect(sessionStorage.getItem("assistant:thread:demo")).toBe("t7");
    expect(($(".send") as HTMLButtonElement).disabled).toBe(false);
  });

  describe("working indicator", () => {
    // A send that stays open until the test lets it go, so the stream can be fed event by event.
    function held() {
      let on!: (e: BotEvent) => void;
      let finish!: () => void;
      let fail!: (e: unknown) => void;
      const send = vi.fn(
        (_t: any, _x: string, _f: string[], cb: (e: BotEvent) => void) =>
          new Promise<void>((res, rej) => {
            on = cb;
            finish = res;
            fail = rej;
          }),
      );
      return { send, emit: (e: BotEvent) => on(e), finish: () => finish(), fail: (e: unknown) => fail(e) };
    }
    const status = (step: string): BotEvent => ({ event: "status", data: { step } });

    it("appears right after the message is sent, as one polite status at the end of the list", async () => {
      const h = held();
      const { $, $$, say } = setup({ send: h.send as any });
      await say("hello");
      const w = $$(".working");
      expect(w).toHaveLength(1);
      expect(w[0].textContent).toContain("Thinking…");
      expect(w[0].getAttribute("role")).toBe("status");
      expect(w[0].getAttribute("aria-live")).toBe("polite");
      expect($(".list").lastElementChild).toBe(w[0]);
      expect($(".list").children[$(".list").children.length - 2].classList.contains("user")).toBe(true);
      expect(w[0].querySelectorAll(".dot")).toHaveLength(3);
    });

    it("changes its label on a status event and keeps it for an unknown code", async () => {
      const h = held();
      const { $$, say } = setup({ send: h.send as any });
      await say("hello");
      const label = () => $$(".working")[0].textContent;
      const expected: [string, string][] = [
        ["reading", "Reading your files…"],
        ["listening", "Listening to your voice note…"],
        ["thinking", "Thinking…"],
        ["writing", "Writing your posts…"],
        ["checking", "Checking every number…"],
      ];
      for (const [step, text] of expected) {
        h.emit(status(step));
        expect(label()).toContain(text);
        expect($$(".working")).toHaveLength(1);
      }
      h.emit(status("something-new"));
      expect(label()).toContain("Checking every number…");
      h.finish();
    });

    it("goes away when the first text arrives", async () => {
      const h = held();
      const { $$, say } = setup({ send: h.send as any });
      await say("hello");
      h.emit({ event: "text", data: { delta: "Hi" } });
      expect($$(".working")).toHaveLength(0);
      expect($$(".bot")[0].textContent).toBe("Hi");
      h.finish();
    });

    it("goes away when a draft arrives", async () => {
      const h = held();
      const { $$, say } = setup({ send: h.send as any });
      await say("hello");
      h.emit(status("writing"));
      h.emit({ event: "draft", data: draft } as BotEvent);
      expect($$(".working")).toHaveLength(0);
      h.finish();
    });

    it("goes away when the turn fails", async () => {
      const h = held();
      const { $, $$, say } = setup({ send: h.send as any });
      await say("hello");
      h.emit({ event: "failed", data: { error: "server", message: "Oops" } });
      expect($$(".working")).toHaveLength(0);
      expect($(".err").textContent).toBe("Oops");
      h.finish();
    });

    it("shows again at the end on a later status, and the next text starts a new bubble below", async () => {
      const h = held();
      const { $, $$, say } = setup({ send: h.send as any });
      await say("hello");
      h.emit({ event: "text", data: { delta: "First." } });
      h.emit(status("writing"));
      expect($$(".working")).toHaveLength(1);
      expect($(".list").lastElementChild).toBe($$(".working")[0]);
      expect($$(".working")[0].textContent).toContain("Writing your posts…");
      h.emit({ event: "text", data: { delta: "Second." } });
      expect($$(".working")).toHaveLength(0);
      expect($$(".bot").map((b) => b.textContent)).toEqual(["First.", "Second."]);
      h.finish();
    });

    it("a new bubble after a draft does not start with the bot's blank lines", async () => {
      const h = held();
      const { $$, say } = setup({ send: h.send as any });
      await say("hello");
      h.emit({ event: "text", data: { delta: "Love it." } });
      h.emit(status("writing"));
      h.emit({ event: "draft", data: draft } as BotEvent);
      h.emit({ event: "text", data: { delta: "\n\n" } });
      expect($$(".bot")).toHaveLength(1); // blank lines alone open no bubble
      h.emit({ event: "text", data: { delta: "Here's a first go." } });
      h.emit({ event: "text", data: { delta: "\n\nWant it shorter" } });
      // The second answer's paragraphs, drawn as paragraphs as they stream in.
      const paras = (b: Element) => Array.from(b.querySelectorAll("p")).map((p) => p.textContent);
      expect($$(".bot").map(paras)).toEqual([["Love it."], ["Here's a first go.", "Want it shorter"]]);
      h.finish();
    });

    it("is removed when the send finishes without any event", async () => {
      const h = held();
      const { $$, say, flush } = setup({ send: h.send as any });
      await say("hello");
      h.finish();
      await flush();
      expect($$(".working")).toHaveLength(0);
    });

    it("is removed when the send errors", async () => {
      const h = held();
      const { $$, say, flush } = setup({ send: h.send as any });
      await say("hello");
      h.fail(new ApiError(500, "server", "Boom"));
      await flush();
      expect($$(".working")).toHaveLength(0);
    });

    it("has CSS that stops the dots moving for reduced motion but keeps them visible", () => {
      const { p } = setup();
      const css = p.root.querySelector("style")!.textContent!;
      expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)[^}]*\.dot[^}]*animation: none/s);
    });

    // Mike, 21 Nov 2026: on StreakPlan's phone layout the open button sat on the app's tab bar.
    it("lets the host lift the open button above its own bottom bar", () => {
      const { p } = setup();
      const css = p.root.querySelector("style")!.textContent!;
      expect(css).toMatch(/\.launcher \{[^}]*bottom: calc\(20px \+ var\(--assistant-bottom, 0px\)\)/);
    });
  });
});

// A message shows what it sent (21 Nov 2026: a picture showed only until a reload, a voice note never played).
describe("a message's files", () => {
  it("a sent picture, voice note and PDF show in the message at once, from the browser's own copies", async () => {
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: (f: File) => `blob:${f.name}` }));
    const upload = vi
      .fn()
      .mockResolvedValueOnce({ file_id: "i1", kind: "image", thread_id: "t1" })
      .mockResolvedValueOnce({ file_id: "v1", kind: "voice", thread_id: "t1" })
      .mockResolvedValueOnce({ file_id: "p1", kind: "pdf", thread_id: "t1" });
    const { $, $$, say, flush } = setup({ upload } as any, [{ event: "thread", data: { thread_id: "t1", title: "" } } as BotEvent]);
    const input = $("input[type=file]") as HTMLInputElement;
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [
        new File(["x"], "shot.png", { type: "image/png" }),
        new File(["x"], "voice.webm", { type: "audio/webm" }),
        new File(["x"], "deck.pdf", { type: "application/pdf" }),
      ],
    });
    input.dispatchEvent(new Event("change"));
    await flush();
    await flush();
    await say("look");
    const mine = $$(".msg.user")[0];
    expect(mine.querySelector("img")!.getAttribute("src")).toBe("blob:shot.png");
    expect(mine.querySelector("audio")!.getAttribute("src")).toBe("blob:voice.webm");
    expect(mine.querySelector("a.file")!.getAttribute("href")).toBe("blob:deck.pdf");
    expect(mine.textContent).toContain("look");
  });

  it("after a reload, each message's files come back through the bot's short links", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t1");
    const thread = vi.fn(async () => ({
      messages: [
        { role: "user", text: "look", files: [{ id: "i1", kind: "image" }, { id: "v1", kind: "voice" }, { id: "p1", kind: "pdf" }] },
        { role: "assistant", text: "Nice." },
      ],
    }));
    const fileLink = vi.fn(async (_t: string, id: string) => ({ url: `https://files.test/${id}`, mime: "x", kind: "x" }));
    const { $$, flush } = setup({ thread, fileLink } as any);
    await flush();
    await flush();
    const mine = $$(".msg.user")[0];
    expect(fileLink).toHaveBeenCalledWith("t1", "i1");
    expect(mine.querySelector("img")!.getAttribute("src")).toBe("https://files.test/i1");
    expect(mine.querySelector("audio")!.getAttribute("src")).toBe("https://files.test/v1");
    expect(mine.querySelector("a.file")!.getAttribute("href")).toBe("https://files.test/p1");
  });
});


describe("a draft with a time to post", () => {
  const TUE = "2099-11-24T02:30:00Z";
  const WED = "2099-11-25T12:00:00Z";
  const timed = {
    id: "d1",
    kind: "x_post",
    posts: ["We moved into a new office."],
    post_at: WED,
    post_times: [
      { at: TUE, label: "Tue 9:30 AM" },
      { at: WED, label: "Wed 7:00 PM" },
    ],
  };
  const thr = { event: "thread", data: { thread_id: "t1", title: "" } } as BotEvent;
  const dr = (d: any = timed) => ({ event: "draft", data: d }) as BotEvent;
  const buttons = (el: HTMLElement) => Array.from(el.querySelectorAll("button")).map((b) => b.textContent);

  it("offers the app's free times, the bot's pick first chosen, and Schedule before Save draft", async () => {
    const { $$, say } = setup({}, [thr, dr()]);
    await say("hi");
    const el = $$(".draft")[0];
    const select = el.querySelector("select") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => [o.value, o.textContent])).toEqual([
      [TUE, "Tue 9:30 AM"],
      [WED, "Wed 7:00 PM"],
    ]);
    expect(select.value).toBe(WED);
    // Any app, any kind of writing: a neutral word unless the app gives its own (data-schedule-label).
    expect(select.closest("label")!.firstChild!.textContent).toBe("Publish at ");
    expect(buttons(el)).toEqual(["Schedule", "Save draft", "Shorter", "Another angle"]);
  });

  it("the time's label is the app's own word when it gives one", async () => {
    const api = {
      send: vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => [thr, dr()].forEach(on)),
      act: vi.fn(),
      thread: vi.fn(async () => ({ messages: [] })),
      upload: vi.fn(),
    } as unknown as PanelApi;
    const p = createPanel({ api, app: "demo", label: "Write", position: "right", scheduleLabel: "Send at" });
    p.open();
    (p.root.querySelector("textarea") as HTMLTextAreaElement).value = "hi";
    (p.root.querySelector(".send") as HTMLElement).click();
    await new Promise((r) => setTimeout(r, 0));
    expect(p.root.querySelector(".draft label")!.firstChild!.textContent).toBe("Send at ");
  });

  it("Schedule sends the chosen time and says when, once the app scheduled it", async () => {
    const act = vi.fn(async () => ({ draft_id: "p1", edit_url: null, scheduled_at: TUE }));
    const { $$, say, flush } = setup({ act } as any, [thr, dr()]);
    await say("hi");
    const el = $$(".draft")[0];
    const select = el.querySelector("select") as HTMLSelectElement;
    select.value = TUE;
    select.dispatchEvent(new Event("change"));
    (el.querySelector("button") as HTMLButtonElement).click();
    await flush();
    expect(act).toHaveBeenCalledWith("t1", { action_id: "add:d1", kind: "add_draft", draft_id: "d1", schedule_at: TUE });
    expect(buttons(el)).toEqual(["Scheduled for Tue 9:30 AM", "Shorter", "Another angle"]);
    expect(select.disabled).toBe(true);
  });

  it("a time the app couldn't take leaves it saved as a draft, with the app's why", async () => {
    const act = vi.fn(async () => ({ draft_id: "p1", edit_url: null, scheduled_at: null, schedule_note: "That time is taken now." }));
    const { $$, say, flush } = setup({ act } as any, [thr, dr()]);
    await say("hi");
    const el = $$(".draft")[0];
    (el.querySelector("button") as HTMLButtonElement).click();
    await flush();
    expect(buttons(el)).toEqual(["Saved", "Shorter", "Another angle"]);
    expect(el.querySelector(".note")!.textContent).toBe("Saved as a draft: That time is taken now.");
  });

  it("Save draft only saves it, without a time", async () => {
    const act = vi.fn(async () => ({ draft_id: "p1", edit_url: null }));
    const { $$, say, flush } = setup({ act } as any, [thr, dr()]);
    await say("hi");
    const el = $$(".draft")[0];
    (el.querySelectorAll("button")[1] as HTMLButtonElement).click();
    await flush();
    expect(act).toHaveBeenCalledWith("t1", { action_id: "add:d1", kind: "add_draft", draft_id: "d1" });
    expect(buttons(el)).toEqual(["Saved", "Shorter", "Another angle"]);
  });

  it("times already past are not offered, and with none left there is no Schedule", async () => {
    const old = { ...timed, post_at: "2001-01-01T00:00:00Z", post_times: [{ at: "2001-01-01T00:00:00Z", label: "Mon 1:00 AM" }] };
    const { $$, say } = setup({}, [thr, dr(old)]);
    await say("hi");
    const el = $$(".draft")[0];
    expect(el.querySelector("select")).toBeNull();
    expect(buttons(el)).toEqual(["Save draft", "Shorter", "Another angle"]);
  });

  it("a draft read back after it was scheduled says when", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t1");
    const thread = vi.fn(async () => ({
      messages: [
        { role: "user", text: "hi" },
        { role: "assistant", text: "", drafts: [{ ...timed, added: true, edit_url: null, scheduled_at: TUE }] },
      ],
    }));
    const { $$, flush } = setup({ thread } as any);
    await flush();
    expect(buttons($$(".draft")[0])).toEqual(["Scheduled for Tue 9:30 AM", "Shorter", "Another angle"]);
  });
});

describe("the app's own tools", () => {
  const thr = { event: "thread", data: { thread_id: "t1", title: "" } } as BotEvent;
  const card = { id: "p1", tool: "move_item", title: "Move an item", lines: ["Id: i7", "Day: Friday"] };
  const confirmEv = { event: "confirm", data: card } as BotEvent;
  const buttons = (el: Element) => Array.from(el.querySelectorAll("button")).map((b) => b.textContent);

  it("shows a change as a card with what will change, Confirm and Cancel", async () => {
    const { $$, say } = setup({}, [thr, confirmEv]);
    await say("move it");
    const el = $$(".confirm")[0];
    expect(el.querySelector(".title")!.textContent).toBe("Move an item");
    expect(Array.from(el.querySelectorAll("li")).map((l) => l.textContent)).toEqual(["Id: i7", "Day: Friday"]);
    expect(buttons(el)).toEqual(["Confirm", "Cancel"]);
  });

  it("Confirm runs it once and says it is done, with the app's words", async () => {
    const act = vi.fn(async () => ({ state: "done", result: "Moved to Friday." }));
    const { $$, say, flush } = setup({ act } as any, [thr, confirmEv]);
    await say("move it");
    const el = $$(".confirm")[0];
    (el.querySelector("button") as HTMLButtonElement).click();
    await flush();
    expect(act).toHaveBeenCalledWith("t1", { action_id: "confirm:p1", kind: "confirm", pending_id: "p1" });
    expect(buttons(el)).toEqual([]);
    expect(el.querySelector(".state")!.textContent).toBe("Done. Moved to Friday.");
  });

  it("Cancel drops it", async () => {
    const act = vi.fn(async () => ({ state: "cancelled", result: null }));
    const { $$, say, flush } = setup({ act } as any, [thr, confirmEv]);
    await say("move it");
    (Array.from($$(".confirm")[0].querySelectorAll("button"))[1] as HTMLButtonElement).click();
    await flush();
    expect(act).toHaveBeenCalledWith("t1", { action_id: "confirm:p1", kind: "cancel", pending_id: "p1" });
    expect($$(".confirm")[0].querySelector(".state")!.textContent).toBe("Cancelled.");
  });

  it("a change that didn't work says so, and one read back after a reload shows its state", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t1");
    const thread = vi.fn(async () => ({
      messages: [
        { role: "user", text: "move it" },
        {
          role: "assistant",
          text: "Tap Confirm.",
          confirms: [
            { ...card, state: "failed", result: "That day is full." },
            { ...card, id: "p2", state: "expired", result: null },
            { ...card, id: "p3", state: "waiting", result: null },
          ],
        },
      ],
    }));
    const { $$, flush } = setup({ thread } as any);
    await flush();
    const states = $$(".confirm").map((c) => c.querySelector(".state")?.textContent ?? buttons(c).join("|"));
    expect(states).toEqual(["Didn't work: That day is full.", "Too old to confirm now. Ask again.", "Confirm|Cancel"]);
  });

  it("an empty chat shows the app's starters; a tap sends it", async () => {
    const config = vi.fn(async () => ({ starters: ["How did I do?", "Plan my week"] }));
    const { $$, api, flush } = setup({ config } as any, [thr]);
    await flush();
    const starters = $$(".starter");
    expect(starters.map((s) => s.textContent)).toEqual(["How did I do?", "Plan my week"]);
    starters[0].click();
    await flush();
    expect(api.send.mock.calls[0][1]).toBe("How did I do?");
    expect($$(".starter")).toHaveLength(0);
  });

  it("an answer the cap cut short offers Continue, which sends Continue and goes away", async () => {
    const send = vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => {
      on(thr);
      on({ event: "text", data: { delta: "I stopped here." } } as BotEvent);
      on({ event: "done", data: { message_id: "m1", cost_usd: 0.1, capped: true } } as BotEvent);
    });
    const { $$, api, say, flush } = setup({ send } as any);
    await say("how did I do?");
    const [b] = $$(".continue");
    expect(b.textContent).toBe("Continue");
    b.click();
    await flush();
    expect(api.send.mock.calls[1][1]).toBe("Continue");
  });

  it("a reloaded thread offers Continue only under its last answer, when that one was cut short", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t1");
    const thread = vi.fn(async () => ({
      messages: [
        { role: "user", text: "how did I do?" },
        { role: "assistant", text: "I stopped here.", capped: true },
      ],
    }));
    const { $$, flush } = setup({ thread } as any);
    await flush();
    expect($$(".continue")).toHaveLength(1);
  });

  // Mike, 21 Nov 2026 ("refer Claude chat"): lists and bold drawn, Copy under a whole answer, New chat and Past chats.
  it("draws an answer's list and bold, and offers Copy once the turn ends", async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    const send = vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => {
      on(thr);
      on({ event: "text", data: { delta: "You have **9** videos:\n- 5 in Day 1–5\n- 1 intro" } } as BotEvent);
      expect($$(".acts")).toHaveLength(0); // not while it streams
      on({ event: "done", data: { message_id: "m1", cost_usd: 0.01 } } as BotEvent);
    });
    const { $, $$, say, flush } = setup({ send } as any);
    await say("how many videos?");
    expect($(".bot strong").textContent).toBe("9");
    expect(Array.from($(".bot").querySelectorAll("li")).map((l) => l.textContent)).toEqual(["5 in Day 1–5", "1 intro"]);
    ($(".acts .copy") as HTMLButtonElement).click();
    await flush();
    expect(writeText).toHaveBeenCalledWith("You have **9** videos:\n- 5 in Day 1–5\n- 1 intro");
    vi.unstubAllGlobals();
  });

  it("New chat empties the window and starts a new thread; Past chats opens an old one", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t1");
    const thread = vi.fn(async (id: string) => ({ messages: [{ role: "assistant", text: id === "t1" ? "Old one." : "Older." }] }));
    const threads = vi.fn(async () => [
      { id: "t1", title: "This week", updated_at: "2026-11-21T08:00:00Z" },
      { id: "t0", title: "Last week", updated_at: "2026-11-14T08:00:00Z" },
    ]);
    const { $, $$, p, flush } = setup({ thread, threads } as any);
    await flush();
    expect($(".bot").textContent).toBe("Old one.");
    (p.root.querySelector('[aria-label="New chat"]') as HTMLButtonElement).click();
    await flush();
    expect($$(".bot")).toHaveLength(0);
    expect(sessionStorage.getItem("assistant:thread:demo")).toBeNull();
    (p.root.querySelector('[aria-label="Past chats"]') as HTMLButtonElement).click();
    await flush();
    const past = $$(".past");
    expect(past.map((b) => b.querySelector(".past-title")!.textContent)).toEqual(["This week", "Last week"]);
    past[1].click();
    await flush();
    expect(thread).toHaveBeenLastCalledWith("t0");
    expect($(".bot").textContent).toBe("Older.");
    expect(($(".history") as HTMLElement).hidden).toBe(true);
  });

  // MCP Apps (Mike, 21 Nov 2026: "show the data as SuperX does"): an app's card in a sandboxed frame, before the words.
  it("draws an app's card in a sandboxed frame before the words, live and after a reload", async () => {
    const view = { id: "v1", tool: "list_posts", html: "<p>card</p>", input: {}, result: { content: [] } };
    const send = vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => {
      on(thr);
      on({ event: "view", data: view } as BotEvent);
      on({ event: "text", data: { delta: "That one did best." } } as BotEvent);
    });
    const { $, $$, say } = setup({ send } as any);
    await say("show my posts");
    const frame = $(".view iframe") as HTMLIFrameElement;
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    expect(frame.srcdoc).toContain("<p>card</p>");
    const order = $$(".view, .msg.bot").map((e) => (e.classList.contains("view") ? "view" : "bot"));
    expect(order).toEqual(["view", "bot"]);

    document.body.innerHTML = "";
    sessionStorage.setItem("assistant:thread:demo", "t1");
    const thread = vi.fn(async () => ({
      messages: [
        { role: "user", text: "show my posts" },
        { role: "assistant", text: "That one did best.", views: [view] },
      ],
    }));
    const again = setup({ thread } as any);
    await again.flush();
    expect(again.$$(".view iframe")).toHaveLength(1);
    expect(again.$$(".view, .msg.bot").map((e) => e.className)).toEqual(["view framed", "msg bot"]);
  });

  it("says it is checking the app while a read runs", async () => {
    let release!: () => void;
    const send = vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => {
      on(thr);
      on({ event: "status", data: { step: "reading_app" } } as BotEvent);
      await new Promise<void>((r) => (release = r));
    });
    const { $, say } = setup({ send } as any);
    await say("how did I do?");
    expect($(".working .label").textContent).toBe("Checking your app…");
    release();
  });
});
