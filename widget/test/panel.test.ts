import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, AuthLost, type BotEvent } from "../src/api";
import { createPanel, type PanelApi } from "../src/panel";

const card = {
  pieces: [
    { id: "p1", spec_kind: "launch", text: "Dark mode is live.", image_file_id: null, fact_ids: ["f1"], dropped: null },
    { id: "p2", spec_kind: "launch", text: "Bad post", image_file_id: null, fact_ids: [], dropped: "It had a number that isn't in your facts." },
  ],
  facts: [{ id: "f1", text: "Shipped dark mode", numbers: [], source: "chat", file_id: null }],
};

function setup(over: Partial<PanelApi> = {}, events: BotEvent[] = []) {
  const api = {
    send: vi.fn(async (_t: any, _x: string, _f: string[], on: (e: BotEvent) => void) => events.forEach(on)),
    upload: vi.fn(async () => ({ file_id: "f", kind: "image", thread_id: "t1" })),
    act: vi.fn(async () => ({ draft_id: "d1", edit_url: "https://app.test/edit/d1" })),
    thread: vi.fn(async () => ({ messages: [], pieces: [] })),
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

  it("Add to Schedule disables while pending, shows Added, Edit link, and dispatches assistant:saved", async () => {
    const saved: string[] = [];
    window.addEventListener("assistant:saved", (e) => saved.push((e as CustomEvent).detail.draft_id));
    let release!: () => void;
    const act = vi.fn(() => new Promise((r) => (release = () => r({ draft_id: "d1", edit_url: "https://app.test/edit/d1" }))));
    const { $, $$, say, flush } = setup({ act } as any, [
      { event: "thread", data: { thread_id: "t1", title: "" } },
      { event: "card", data: card },
    ]);
    await say("hi");
    expect($$(".piece")).toHaveLength(2);
    expect($$(".piece")[1].querySelector("button")).toBeNull(); // dropped: no Add button
    expect($$(".piece")[1].textContent).toContain("isn't in your facts");
    const add = $$(".piece")[0].querySelector("button") as HTMLButtonElement;
    expect($$(".piece")[0].querySelector("a")).toBeNull();
    add.click();
    expect(add.disabled).toBe(true);
    release();
    await flush();
    expect(add.textContent).toBe("Added");
    expect(saved).toEqual(["d1"]);
    const edit = $$(".piece")[0].querySelector("a") as HTMLAnchorElement;
    expect(edit.href).toBe("https://app.test/edit/d1");
    expect(act).toHaveBeenCalledWith("t1", { action_id: "add:p1", kind: "add_draft", piece_id: "p1" });
  });

  it("no Edit link when edit_url is null; retry after failure reuses the action id", async () => {
    const act = vi.fn().mockRejectedValueOnce(new ApiError(502, "app_down", "The app couldn't be reached.")).mockResolvedValueOnce({ draft_id: "d1", edit_url: null });
    const { $$, say, flush } = setup({ act } as any, [
      { event: "thread", data: { thread_id: "t1", title: "" } },
      { event: "card", data: card },
    ]);
    await say("hi");
    const add = $$(".piece")[0].querySelector("button") as HTMLButtonElement;
    add.click();
    await flush();
    expect(add.disabled).toBe(false);
    expect($$(".piece")[0].textContent).toContain("couldn't be reached");
    add.click();
    await flush();
    expect(act.mock.calls[0][1].action_id).toBe(act.mock.calls[1][1].action_id);
    expect($$(".piece")[0].querySelector("a")).toBeNull();
    expect(add.textContent).toBe("Added");
  });

  it("Keep for next week sends the edited facts and shows Kept", async () => {
    const { $, $$, say, flush, api } = setup({}, [
      { event: "thread", data: { thread_id: "t1", title: "" } },
      { event: "card", data: card },
    ]);
    await say("hi");
    const field = $(".fact") as HTMLInputElement;
    expect(field.maxLength).toBe(500); // the server refuses a longer fact
    field.value = "Shipped dark mode on Monday";
    const keep = $$(".facts .btn")[0] as HTMLButtonElement;
    keep.click();
    await flush();
    const [, body] = api.act.mock.calls[0];
    expect(body.kind).toBe("keep_facts");
    expect(body.action_id).toBe("keep:p1");
    expect(body.facts[0]).toMatchObject({ id: "f1", text: "Shipped dark mode on Monday" });
    expect(keep.textContent).toBe("Kept");
  });

  it("Keep for next week sends at most 30 facts", async () => {
    const many = {
      ...card,
      facts: Array.from({ length: 45 }, (_, i) => ({ id: `f${i}`, text: `Fact ${i}`, numbers: [], source: "chat", file_id: null })),
    };
    const { $$, say, flush, api } = setup({}, [
      { event: "thread", data: { thread_id: "t1", title: "" } },
      { event: "card", data: many },
    ]);
    await say("hi");
    ($$(".facts .btn")[0] as HTMLButtonElement).click();
    await flush();
    const [, body] = api.act.mock.calls[0];
    expect(body.facts).toHaveLength(30);
    expect(body.facts[0].id).toBe("f0");
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
    const thread = vi.fn(async () => ({ messages: [{ role: "user", text: "hi" }, { role: "assistant", text: "Hello" }], pieces: [] }));
    const { $$, flush } = setup({ thread } as any);
    await flush();
    expect(thread).toHaveBeenCalledWith("t7");
    expect($$(".msg").map((m) => m.textContent)).toEqual(["hi", "Hello"]);
  });

  it("a card reopened from history taps with the same action ids as when it was first shown", async () => {
    sessionStorage.setItem("assistant:thread:demo", "t1");
    const thread = vi.fn(async () => ({ messages: [{ role: "assistant", text: "", card }], pieces: [] }));
    const { $, $$, flush, api } = setup({ thread } as any);
    await flush();
    ($$(".piece")[0].querySelector("button") as HTMLButtonElement).click();
    await flush();
    ($$(".facts .btn")[0] as HTMLButtonElement).click();
    await flush();
    expect(api.act.mock.calls.map((c: any[]) => [c[0], c[1].action_id])).toEqual([
      ["t1", "add:p1"],
      ["t1", "keep:p1"],
    ]);
    void $;
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
});
