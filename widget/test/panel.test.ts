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
    expect(Array.from(el.querySelectorAll("button")).map((b) => b.textContent)).toEqual(["Add to Schedule", "Shorter", "Another angle"]);
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

  it("Add to Schedule becomes Added with an Edit link", async () => {
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
    expect(add.textContent).toBe("Added to your Schedule");
    expect(saved).toEqual(["sch1"]);
    const edit = $$(".draft")[0].querySelector("a") as HTMLAnchorElement;
    expect(edit.href).toBe("https://app.test/edit/d1");
    expect(edit.textContent).toBe("Edit");
    expect(act).toHaveBeenCalledWith("t1", { action_id: "add:d1", kind: "add_draft", draft_id: "d1" });
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
    expect(add.textContent).toBe("Added to your Schedule");
  });

  it("renders a saved line quietly", async () => {
    const { $$, say } = setup({}, [thr(), { event: "saved", data: { kind: "facts", items: ["Shipped dark mode", "120 users"] } } as BotEvent]);
    await say("hi");
    const line = $$(".saved")[0];
    expect(line.textContent).toContain("Shipped dark mode; 120 users");
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
    expect(($$(".draft")[1].querySelector("button") as HTMLButtonElement).textContent).toBe("Added to your Schedule");
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
  });
});
