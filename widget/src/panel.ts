/** The button and the chat panel, in a shadow root. Server and user text only ever goes in through textContent. */
import { type ActionBody, ApiError, AuthLost, type BotApi, type BotEvent, type Confirm, type Draft } from "./api";
import { startRecording, type Recording } from "./recorder";
import { icon, type IconName } from "./icons";
import { renderMarkdown } from "./markdown";
import { CSS } from "./styles";

export type PanelApi = Pick<BotApi, "send" | "upload" | "act" | "thread" | "fileLink"> &
  Partial<Pick<BotApi, "config" | "threads">>;
export interface PanelOptions {
  api: PanelApi;
  app: string;
  label: string;
  position: "left" | "right";
  /** The word before a draft's time picker, in the app's words ("Post at", "Send at"). Default "Publish at". */
  scheduleLabel?: string;
  parent?: HTMLElement;
}

const SIGN_IN = "Please sign in again.";
/** What a reloaded message calls a file it sent (the thread keeps no file names). */
const FILE_NAMES: Record<string, string> = { image: "Picture", voice: "Voice note", pdf: "PDF" };
/** A picked file's kind, as the bot names it. */
const kindOf = (f: File) =>
  f.type.startsWith("image/") ? "image" : f.type.startsWith("audio/") ? "voice" : f.type === "application/pdf" ? "pdf" : "file";
/** What the bot says it is doing, by the server's `status` step. A step not listed here changes nothing. */
const STEP_LABELS: Record<string, string> = {
  reading: "Reading your files…",
  listening: "Listening to your voice note…",
  thinking: "Thinking…",
  writing: "Writing your posts…",
  checking: "Checking every number…",
  reading_app: "Checking your app…",
};
/** What a change's card says once it is not waiting any more. */
const CHANGE_STATES: Record<string, (result: string) => string> = {
  done: (r) => (r ? `Done. ${r}` : "Done."),
  failed: (r) => (r ? `Didn't work: ${r}` : "Didn't work."),
  cancelled: () => "Cancelled.",
  expired: () => "Too old to confirm now. Ask again.",
};
const RESULT_SHOWN = 240; // the app's answer under a done change, at most
const SHORTER = "Make it shorter";
const ANOTHER_ANGLE = "Try another angle";
const CHECK_ICON = icon("check", 14);
const MAX_RECORD_MS = 5 * 60_000;

type Child = Node | string | null | undefined | false;
function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Record<string, any> = {},
  ...kids: Child[]
): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2).toLowerCase(), v);
    else e.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids) if (kid) e.append(kid);
  return e;
}

function safeHref(url: string): string | null {
  try {
    const u = new URL(url, location.href);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

function errorText(e: unknown): string {
  if (e instanceof AuthLost) return SIGN_IN;
  if (e instanceof ApiError) return e.message;
  return "Something went wrong. Please try again.";
}

interface Chip {
  file: File;
  state: "uploading" | "ready" | "error";
  fileId?: string;
  error?: string;
  el: HTMLElement;
  bar: HTMLProgressElement;
  note: HTMLElement;
}

export function createPanel(opts: PanelOptions) {
  const { api, app } = opts;
  const storeKey = `assistant:thread:${app}`;
  let threadId: string | null = null;
  let busy = false;
  let signedOut = false;
  let loaded = false;
  let recording: Recording | null = null;
  let recordTimer: ReturnType<typeof setTimeout> | undefined;
  let holding = false;
  const chips: Chip[] = [];
  let uploads: Promise<unknown> = Promise.resolve();
  const imageUrls = new Map<string, string>(); // file id -> local preview of a picture sent from here

  const store = {
    get(): string | null {
      try {
        return sessionStorage.getItem(storeKey);
      } catch {
        return null;
      }
    },
    set(id: string | null) {
      try {
        if (id) sessionStorage.setItem(storeKey, id);
        else sessionStorage.removeItem(storeKey);
      } catch {
        /* storage may be blocked */
      }
    },
  };
  const setThread = (id: string) => {
    threadId = id;
    store.set(id);
  };

  // ---- shell ----
  const host = h("div", { id: "assistant-widget" });
  const root = host.attachShadow({ mode: "open" });
  const wrap = h("div", { class: opts.position === "left" ? "left" : "right" });
  // An icon button: the icon is ours (a fixed string), its name is for screen readers and the tooltip.
  const iconButton = (cls: string, name: IconName, label: string, size = 18) => {
    const b = h("button", { class: `icon ${cls}`.trim(), type: "button", "aria-label": label, title: label });
    b.innerHTML = icon(name, size);
    return b;
  };
  const launcher = h("button", { class: "launcher", type: "button", "aria-haspopup": "dialog", "aria-expanded": "false" });
  launcher.innerHTML = icon("sparkle", 18);
  launcher.append(h("span", { text: opts.label }));
  const list = h("div", { class: "list", role: "log", "aria-live": "polite", "aria-label": "Conversation" });
  const history = h("div", { class: "history", hidden: true, "aria-label": "Past chats" });
  const status = h("div", { class: "status", "aria-live": "polite" });
  const chipsEl = h("div", { class: "chips" });
  const input = h("textarea", { rows: 1, placeholder: "Write a message…", "aria-label": "Your message", maxlength: 4000 });
  const fileInput = h("input", { type: "file", accept: "image/*,application/pdf,audio/*", hidden: true, tabindex: -1 });
  fileInput.multiple = true;
  const attach = iconButton("", "paperclip", "Attach a file");
  const mic = iconButton("mic", "mic", "Hold to record a voice note");
  const send = iconButton("send", "send", "Send", 18);
  const closeBtn = iconButton("", "close", "Close");
  const newChatBtn = iconButton("", "newChat", "New chat");
  const historyBtn = iconButton("", "history", "Past chats");
  historyBtn.setAttribute("aria-expanded", "false");
  if (!api.threads) historyBtn.hidden = true;
  const title = h("h2", { text: opts.label });
  const panel = h(
    "div",
    { class: "panel", role: "dialog", "aria-label": opts.label, hidden: true },
    h("header", {}, title, h("div", { class: "head-tools" }, newChatBtn, historyBtn, closeBtn)),
    list,
    history,
    h(
      "div",
      { class: "foot" },
      status,
      h(
        "div",
        { class: "composer" },
        chipsEl,
        input,
        h("div", { class: "tools" }, attach, mic, h("span", { class: "grow" }), send),
        fileInput,
      ),
    ),
  );
  wrap.append(launcher, panel);
  root.append(h("style", { text: CSS }), wrap);
  (opts.parent ?? document.body).append(host);

  // ---- helpers ----
  const scroll = () => {
    list.scrollTop = list.scrollHeight;
  };
  const addMsg = (cls: string, text: string): HTMLElement => {
    const m = h("div", { class: cls, text });
    list.append(m);
    scroll();
    return m;
  };
  const showError = (text: string) => addMsg("err", text);

  // The assistant's words: no bubble, full width, drawn as the reader expects (markdown.ts: lists, bold, links), as
  // they stream in. What it said as written is kept for Copy.
  const said = new WeakMap<HTMLElement, string>();
  const addBot = (text: string): HTMLElement => {
    const m = h("div", { class: "msg bot" });
    list.append(m);
    setBot(m, text);
    return m;
  };
  const setBot = (m: HTMLElement, text: string) => {
    said.set(m, text);
    m.replaceChildren(renderMarkdown(text));
    scroll();
  };
  /** Copy, quietly under an answer once it is whole. */
  function addActions(m: HTMLElement) {
    const copy = iconButton("copy", "copy", "Copy", 16);
    copy.addEventListener("click", () => {
      const text = said.get(m) ?? m.textContent ?? "";
      void navigator.clipboard?.writeText(text).then(
        () => {
          copy.innerHTML = icon("check", 16);
          copy.setAttribute("aria-label", "Copied");
          setTimeout(() => {
            copy.innerHTML = icon("copy", 16);
            copy.setAttribute("aria-label", "Copy");
          }, 1500);
        },
        () => undefined,
      );
    });
    m.after(h("div", { class: "acts" }, copy));
  }

  // A picture, full size over the chat; Escape or a tap closes it.
  function viewImage(src: string, alt: string) {
    const closeView = iconButton("view-close", "close", "Close picture", 20);
    const img = h("img", { src, alt });
    const view = h("div", { class: "viewer", role: "dialog", "aria-label": alt || "Picture" }, img, closeView);
    const done = () => {
      view.remove();
      panel.removeEventListener("keydown", onKey, true);
    };
    const onKey = (e: Event) => {
      if ((e as KeyboardEvent).key === "Escape") {
        e.stopPropagation();
        done();
      }
    };
    view.addEventListener("click", done);
    panel.addEventListener("keydown", onKey, true);
    panel.append(view);
    closeView.focus();
  }

  // A user's message with what it sent (21 Nov 2026: a picture showed only until a reload, a voice note never played):
  // a picture as a small image, a voice note as a player, anything else as a link that opens it; then the words.
  function addUserMsg(text: string, files: { name: string; kind: string; src?: string }[]): HTMLElement {
    const m = h("div", { class: "msg user" });
    files.forEach((f, i) => {
      const box = h("div", { class: "sent", "data-i": String(i), "data-kind": f.kind });
      box.append(fileView(f.kind, f.name, f.src));
      m.append(box);
    });
    if (text) m.append(h("div", { text }));
    list.append(m);
    scroll();
    return m;
  }
  function fileView(kind: string, name: string, src?: string): HTMLElement {
    if (kind === "image") {
      const img = h("img", { alt: name, ...(src ? { src } : {}) });
      const b = h("button", { class: "thumb", type: "button", "aria-label": `Open ${name}` }, img);
      b.addEventListener("click", () => img.src && viewImage(img.src, name));
      return b;
    }
    if (kind === "voice") return h("audio", { controls: "", preload: "metadata", "aria-label": name, ...(src ? { src } : {}) });
    const a = h("a", { class: "file", target: "_blank", rel: "noreferrer", ...(src ? { href: src } : {}) });
    a.innerHTML = icon("file", 16);
    a.append(h("span", { text: name }));
    return a;
  }
  function setFileSrc(msg: HTMLElement, i: number, url: string) {
    const box = msg.querySelector(`.sent[data-i="${i}"]`);
    const el = box?.firstElementChild;
    if (!el) return;
    if (el.tagName === "A") el.setAttribute("href", url);
    else if (el.tagName === "BUTTON") el.querySelector("img")?.setAttribute("src", url);
    else el.setAttribute("src", url);
  }

  // The "bot is working" line: one element at the end of the list while the bot has said nothing yet.
  let working: HTMLElement | null = null;
  const workingLabel = h("span", { class: "label", text: STEP_LABELS.thinking });
  const showWorking = (step?: string) => {
    const label = step && STEP_LABELS[step];
    if (label) workingLabel.textContent = label; // an unknown step keeps the label it has
    working ??= h(
      "div",
      { class: "working", role: "status", "aria-live": "polite" },
      h("span", { class: "dots", "aria-hidden": "true" }, h("i", { class: "dot" }), h("i", { class: "dot" }), h("i", { class: "dot" })),
      workingLabel,
    );
    list.append(working); // appending an element already in the list moves it to the end
    scroll();
  };
  const hideWorking = () => {
    working?.remove();
    working = null;
  };

  function refresh() {
    const uploading = chips.some((c) => c.state === "uploading");
    send.disabled = busy || signedOut || uploading;
    attach.disabled = busy || signedOut;
    mic.disabled = busy || signedOut;
    input.disabled = signedOut;
    for (const sync of syncers) sync();
  }
  function stop(text: string) {
    signedOut = true;
    showError(text);
    refresh();
  }

  // ---- open / close ----
  function open() {
    panel.hidden = false;
    launcher.hidden = true;
    launcher.setAttribute("aria-expanded", "true");
    input.focus();
    if (!loaded) {
      loaded = true;
      const saved = store.get();
      if (saved) void restore(saved);
      else void showStarters();
    }
  }
  function close() {
    panel.hidden = true;
    launcher.hidden = false;
    launcher.setAttribute("aria-expanded", "false");
    launcher.focus();
  }
  launcher.addEventListener("click", open);
  closeBtn.addEventListener("click", close);

  // ---- a new chat, and the past ones ----
  /** An empty chat on screen (the thread is kept by the bot; Past chats opens it again). */
  function clearChat() {
    list.replaceChildren();
    syncers.clear();
    startersEl = null;
    continueEl = null;
    working = null;
    status.textContent = "";
  }
  function newChat() {
    if (busy) return;
    showList();
    clearChat();
    threadId = null;
    store.set(null);
    void showStarters();
    input.focus();
  }
  newChatBtn.addEventListener("click", newChat);

  function showList() {
    history.hidden = true;
    list.hidden = false;
    historyBtn.setAttribute("aria-expanded", "false");
  }
  async function showHistory() {
    if (!api.threads) return;
    if (!history.hidden) return showList();
    historyBtn.setAttribute("aria-expanded", "true");
    list.hidden = true;
    history.hidden = false;
    history.replaceChildren(h("p", { class: "quiet", text: "Loading your chats…" }));
    try {
      const threads = await api.threads();
      if (!threads.length) {
        history.replaceChildren(h("p", { class: "quiet", text: "No past chats yet." }));
        return;
      }
      history.replaceChildren(
        ...threads.map((t) => {
          const when = new Date(t.updated_at);
          const b = h(
            "button",
            { class: `past${t.id === threadId ? " current" : ""}`, type: "button" },
            h("span", { class: "past-title", text: t.title || "Chat" }),
            h("span", {
              class: "past-when",
              text: Number.isNaN(when.getTime()) ? "" : when.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
            }),
          );
          b.addEventListener("click", () => {
            if (busy) return;
            showList();
            if (t.id === threadId) return;
            clearChat();
            void restore(t.id);
          });
          return b;
        }),
      );
    } catch (e) {
      if (e instanceof AuthLost) return stop(SIGN_IN);
      history.replaceChildren(h("p", { class: "quiet", text: errorText(e) }));
    }
  }
  historyBtn.addEventListener("click", () => void showHistory());

  panel.addEventListener("keydown", (e) => {
    if ((e as KeyboardEvent).key === "Escape") {
      e.stopPropagation();
      close();
    }
  });

  async function restore(id: string) {
    try {
      const t = await api.thread(id);
      setThread(id);
      for (const m of t.messages) {
        if (m.role === "user") {
          const shown = addUserMsg(
            m.text,
            (m.files ?? []).map((f) => ({ name: FILE_NAMES[f.kind] ?? "File", kind: f.kind })),
          );
          // Each file's short link, asked for once the message is on screen; one that can't be had stays a name.
          (m.files ?? []).forEach((f, i) => {
            api.fileLink(id, f.id).then(
              (link) => setFileSrc(shown, i, link.url),
              () => undefined,
            );
          });
          continue;
        }
        // In the order a turn shows them live: what it kept, its drafts, its words, then a change it asked for (the
        // card comes after the words that explain it).
        if (m.saved?.length) renderSaved(m.saved);
        for (const d of m.drafts ?? []) renderDraft(d);
        if (m.text) addActions(addBot(m.text));
        for (const c of m.confirms ?? []) renderConfirm(c);
      }
      const last = t.messages[t.messages.length - 1];
      if (last?.role === "assistant" && last.capped) showContinue();
    } catch (e) {
      if (e instanceof AuthLost) stop(SIGN_IN);
      else if (e instanceof ApiError && e.status === 404) store.set(null); // the thread is gone: start fresh
      else {
        showError(errorText(e));
        loaded = false; // a passing failure: try again next time the panel opens
      }
    }
  }

  // ---- the app's starters, before the first message ----
  let startersEl: HTMLElement | null = null;
  async function showStarters() {
    if (!api.config) return;
    let starters: string[] = [];
    try {
      starters = (await api.config()).starters ?? [];
    } catch {
      return; // no starters is fine: the chat works the same
    }
    if (!starters.length || threadId || list.children.length) return;
    startersEl = h(
      "div",
      { class: "empty" },
      h("p", { class: "hello", text: "What can I help with?" }),
      h(
        "div",
        { class: "starters" },
        ...starters.map((text) => {
          const b = h("button", { class: "starter", type: "button", text });
          b.addEventListener("click", () => void sendTurn(text, []));
          return b;
        }),
      ),
    );
    list.append(startersEl);
  }
  const dropStarters = () => {
    startersEl?.remove();
    startersEl = null;
  };

  // ---- Continue, under an answer the message's cost cap cut short ----
  let continueEl: HTMLElement | null = null;
  function showContinue() {
    dropContinue();
    const b = h("button", { class: "starter continue", type: "button", text: "Continue" });
    b.addEventListener("click", () => void sendTurn("Continue", []));
    continueEl = h("div", { class: "starters" }, b);
    list.append(continueEl);
    scroll();
  }
  const dropContinue = () => {
    continueEl?.remove();
    continueEl = null;
  };

  // ---- a change waiting for the user's OK ----
  function renderConfirm(c: Confirm) {
    const el = h("div", { class: "confirm", role: "group", "aria-label": c.title });
    el.append(h("p", { class: "title", text: c.title }));
    if (c.lines?.length) el.append(h("ul", {}, ...c.lines.map((l) => h("li", { text: l }))));
    const err = h("div", { class: "small", role: "alert" });
    const showState = (state: string, result?: string | null) => {
      row.remove();
      el.querySelector(".state")?.remove();
      const said = (CHANGE_STATES[state] ?? (() => ""))((result ?? "").slice(0, RESULT_SHOWN));
      el.insertBefore(h("p", { class: `state ${state}`, text: said }), err);
    };
    const row = h("div", { class: "row" });
    const yes = h("button", { class: "btn", type: "button", text: "Confirm", "aria-label": `Confirm: ${c.title}` });
    const no = h("button", { class: "btn ghost", type: "button", text: "Cancel", "aria-label": `Cancel: ${c.title}` });
    row.append(yes, no);
    el.append(row, err);
    let pending = false;
    const sync = () => {
      yes.disabled = no.disabled = pending || busy || signedOut;
    };
    syncers.add(sync);
    const tap = async (kind: "confirm" | "cancel") => {
      pending = true;
      sync();
      err.textContent = "";
      try {
        const r = await api.act(threadId!, { action_id: `confirm:${c.id}`, kind, pending_id: c.id });
        if (r.state === "waiting" || r.state === "running") throw new Error("pending");
        showState(r.state, r.result);
        syncers.delete(sync);
      } catch (e) {
        pending = false;
        sync();
        if (e instanceof AuthLost) stop(SIGN_IN);
        else if (e instanceof ApiError && e.status === 410) showState("expired");
        else err.textContent = e instanceof Error && e.message === "pending" ? "Still working on it. Try again in a moment." : errorText(e);
      }
    };
    yes.addEventListener("click", () => void tap("confirm"));
    no.addEventListener("click", () => void tap("cancel"));
    if (c.state && c.state !== "waiting" && c.state !== "running") showState(c.state, c.result);
    sync();
    list.append(el);
    scroll();
  }

  // ---- drafts and saved lines ----
  const syncers = new Set<() => void>(); // each draft's buttons follow the panel: off while a turn runs
  function renderDraft(d: Draft) {
    const el = h("div", { class: "draft" });
    const img = d.image_file_id ? imageUrls.get(d.image_file_id) : undefined;
    if (img) el.append(h("img", { src: img, alt: "" }));
    const posts = d.posts ?? [];
    if (posts.length > 1) el.append(h("ol", { class: "posts" }, ...posts.map((p) => h("li", { class: "post", text: p }))));
    else el.append(h("p", { class: "post", text: posts[0] ?? "" }));
    const actionId = `add:${d.id}`; // the same for every render of this draft: a repeat tap, or after a reload
    const row = h("div", { class: "row" });
    const add = h("button", { class: "btn", type: "button" });
    // The spoken name starts with the visible text (WCAG 2.5.3) and follows it: Save draft, Saving, Saved. Plain words
    // any app fits (the draft goes wherever the app keeps drafts).
    const setAdd = (text: string, label: string) => {
      add.textContent = text;
      add.setAttribute("aria-label", label);
    };
    setAdd("Save draft", "Save draft: this draft");
    const shorter = h("button", { class: "btn ghost", type: "button", text: "Shorter", "aria-label": "Make this draft shorter" });
    const angle = h("button", { class: "btn ghost", type: "button", text: "Another angle", "aria-label": "Try another angle for this draft" });
    const err = h("div", { class: "small", role: "alert" });
    const note = h("div", { class: "note" });

    // The app's free times, when it can schedule (soonest first; ones already past are left out): the bot's pick is
    // chosen, Schedule saves the draft and asks the app to publish it then (whatever publishing means in that app).
    const labelOf = (at: string) => d.post_times?.find((t) => t.at === at)?.label ?? new Date(at).toLocaleString();
    const times = (d.post_times ?? []).filter((t) => Date.parse(t.at) > Date.now());
    let when: HTMLSelectElement | null = null;
    let schedule: HTMLButtonElement | null = null;
    if (times.length) {
      when = h("select", {}, ...times.map((t) => h("option", { value: t.at, text: t.label })));
      when.value = times.some((t) => t.at === d.post_at) ? d.post_at! : times[0].at;
      schedule = h("button", { class: "btn", type: "button", text: "Schedule" });
      add.classList.add("ghost");
      const named = () => schedule!.setAttribute("aria-label", `Schedule: this draft for ${labelOf(when!.value)}`);
      named();
      when.addEventListener("change", named);
    }

    const buttons = [schedule, add, shorter, angle].filter((b): b is HTMLButtonElement => b !== null);
    let pending = false;
    let finished = false;
    const sync = () => {
      for (const b of buttons) b.disabled = busy || signedOut;
      if (pending || finished) {
        add.disabled = true;
        if (schedule) schedule.disabled = true;
      }
      if (when) when.disabled = busy || signedOut || pending || finished;
    };
    syncers.add(sync);
    const editLink = (editUrl: string | null | undefined) => {
      const href = editUrl ? safeHref(editUrl) : null;
      if (href) row.append(h("a", { class: "btn ghost", href, "aria-label": "Edit this draft in the app", text: "Edit" }));
    };
    const markAdded = (editUrl: string | null | undefined) => {
      finished = true;
      schedule?.remove();
      setAdd("Saved", "Saved: this draft");
      add.classList.add("done");
      add.disabled = true;
      if (when) when.disabled = true;
      editLink(editUrl);
    };
    const markScheduled = (at: string, editUrl: string | null | undefined) => {
      finished = true;
      add.remove();
      const text = `Scheduled for ${labelOf(at)}`;
      if (!schedule) {
        schedule = h("button", { class: "btn", type: "button" });
        row.prepend(schedule);
      }
      schedule.textContent = text;
      schedule.setAttribute("aria-label", `${text}: this draft`);
      schedule.classList.add("done");
      schedule.disabled = true;
      if (when) when.disabled = true;
      editLink(editUrl);
    };
    const save = async (scheduleAt: string | null) => {
      pending = true;
      sync();
      const busyButton = scheduleAt ? schedule! : add;
      const before = busyButton.textContent!;
      busyButton.textContent = scheduleAt ? "Scheduling…" : "Saving…";
      busyButton.setAttribute("aria-label", `${busyButton.textContent}: this draft`);
      err.textContent = "";
      try {
        const body: ActionBody = { action_id: actionId, kind: "add_draft", draft_id: d.id };
        if (scheduleAt) body.schedule_at = scheduleAt;
        const r = await api.act(threadId!, body);
        pending = false;
        if (r.scheduled_at) markScheduled(r.scheduled_at, r.edit_url);
        else {
          markAdded(r.edit_url);
          if (scheduleAt) note.textContent = `Saved as a draft: ${r.schedule_note || "the app couldn't schedule it."}`;
        }
        window.dispatchEvent(new CustomEvent("assistant:saved", { detail: { draft_id: r.draft_id } }));
      } catch (e) {
        pending = false;
        if (scheduleAt) {
          busyButton.textContent = before;
          schedule!.setAttribute("aria-label", `Schedule: this draft for ${labelOf(when!.value)}`);
        } else setAdd("Save draft", "Save draft: this draft");
        sync();
        if (e instanceof AuthLost) stop(SIGN_IN);
        else err.textContent = errorText(e);
      }
    };
    add.addEventListener("click", () => void save(null));
    schedule?.addEventListener("click", () => void save(when!.value));
    shorter.addEventListener("click", () => void sendTurn(SHORTER, [], d.id));
    angle.addEventListener("click", () => void sendTurn(ANOTHER_ANGLE, [], d.id));
    row.prepend(...buttons);
    if (when) el.append(h("label", { class: "when" }, `${opts.scheduleLabel || "Publish at"} `, when));
    el.append(row, note, err);
    if (d.added && d.scheduled_at) markScheduled(d.scheduled_at, d.edit_url);
    else if (d.added) markAdded(d.edit_url);
    sync();
    list.append(el);
    scroll();
  }

  /** The quiet line under a turn: what the bot remembered. Not a message, nothing to press. */
  function renderSaved(items: string[]) {
    // Each item without its own end punctuation, so the joins read "a; b", never "a.; b".
    const text = items
      .map((i) => i.trim().replace(/[\s.;,]+$/, ""))
      .filter(Boolean)
      .join("; ");
    const el = h("div", { class: "saved" });
    el.insertAdjacentHTML("afterbegin", CHECK_ICON);
    el.append(h("span", { text }));
    list.append(el);
    scroll();
  }

  // ---- files ----
  function addFile(file: File) {
    const bar = h("progress", { max: 1, value: 0, "aria-label": `Uploading ${file.name}` });
    const note = h("span", { class: "msg-bad" });
    const x = iconButton("x", "close", `Remove ${file.name}`, 14);
    const el = h("span", { class: "chip" }, h("span", { class: "name", text: file.name }), bar, note, x);
    const chip: Chip = { file, state: "uploading", el, bar, note };
    chips.push(chip);
    chipsEl.append(el);
    x.addEventListener("click", () => {
      chips.splice(chips.indexOf(chip), 1);
      el.remove();
      refresh();
    });
    refresh();
    // One at a time, so two files picked together join one thread instead of each opening its own.
    const mine = uploads.then(() => api.upload(threadId, file, (f) => (bar.value = f)));
    uploads = mine.catch(() => undefined);
    mine
      .then((r) => {
        chip.state = "ready";
        chip.fileId = r.file_id;
        if (!threadId && r.thread_id) setThread(r.thread_id);
        if (file.type.startsWith("image/")) imageUrls.set(r.file_id, URL.createObjectURL(file));
        bar.remove();
      })
      .catch((e) => {
        chip.state = "error";
        chip.error = errorText(e);
        el.classList.add("bad");
        bar.remove();
        note.textContent = chip.error;
        if (e instanceof AuthLost) stop(SIGN_IN);
      })
      .finally(refresh);
  }
  attach.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    for (const f of Array.from(fileInput.files ?? [])) addFile(f);
    fileInput.value = "";
  });

  // ---- voice ----
  async function startVoice() {
    if (recording || busy || signedOut) return;
    holding = true;
    try {
      const r = await startRecording();
      if (!holding) {
        r.cancel(); // let go before the mic was ready
        return;
      }
      recording = r;
      mic.classList.add("rec");
      status.textContent = "Recording… let go to finish.";
      recordTimer = setTimeout(() => void endVoice(), MAX_RECORD_MS);
    } catch {
      holding = false;
      status.textContent = "I can't use the microphone. Check the browser's permission.";
    }
  }
  async function endVoice() {
    holding = false;
    clearTimeout(recordTimer);
    const r = recording;
    if (!r) return;
    recording = null;
    mic.classList.remove("rec");
    status.textContent = "";
    addFile(await r.stop());
  }
  mic.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    void startVoice();
  });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) mic.addEventListener(ev, () => void endVoice());
  mic.addEventListener("keydown", (e) => {
    const k = (e as KeyboardEvent).key;
    if ((k === " " || k === "Enter") && !(e as KeyboardEvent).repeat) {
      e.preventDefault();
      void startVoice();
    }
  });
  mic.addEventListener("keyup", (e) => {
    const k = (e as KeyboardEvent).key;
    if (k === " " || k === "Enter") void endVoice();
  });
  mic.addEventListener("contextmenu", (e) => e.preventDefault());

  // ---- sending ----
  /** Sends the typed text and ready files. */
  function submit() {
    if (busy || signedOut || chips.some((c) => c.state === "uploading")) return;
    const text = input.value.trim();
    const sent = chips.filter((c) => c.state === "ready");
    if (!text && !sent.length) return;
    void sendTurn(text, sent);
  }

  /** One turn. `draftId` is set when it is a Shorter or Another angle about that draft (then nothing typed is taken). */
  async function sendTurn(text: string, sent: Chip[], draftId?: string) {
    if (busy || signedOut) return;
    showList();
    dropStarters();
    dropContinue();
    const fromComposer = draftId === undefined;
    busy = true;
    refresh();
    const mine = addUserMsg(
      text,
      sent.map((c) => ({ name: c.file.name, kind: kindOf(c.file), src: URL.createObjectURL(c.file) })),
    );
    if (fromComposer) {
      input.value = "";
      for (const c of chips.splice(0)) c.el.remove();
    }
    let accepted = false;
    const accept = () => {
      accepted = true;
    };
    // Refused before the server took it (offline, a limit, a bad file): give the text and files back to send again.
    const giveBack = () => {
      mine.remove();
      if (!fromComposer) return;
      if (!input.value) input.value = text;
      chips.unshift(...sent);
      chipsEl.prepend(...sent.map((c) => c.el));
    };
    status.textContent = "";
    workingLabel.textContent = STEP_LABELS.thinking;
    showWorking();
    let bubble: HTMLElement | null = null;
    const spoken: HTMLElement[] = []; // this turn's answers: Copy under each once the turn ends
    const onEvent = (e: BotEvent) => {
      accept();
      const d = e.data;
      switch (e.event) {
        case "thread":
          setThread(d.thread_id);
          break;
        case "transcript":
          addMsg("heard", `Heard: ${d.text}`);
          break;
        case "status":
          bubble = null; // whatever the bot says next goes below the indicator
          showWorking(d.step);
          break;
        case "text": {
          // A new bubble never starts with blank lines (the bot's paragraph break before a draft or a status).
          const delta = bubble ? String(d.delta ?? "") : String(d.delta ?? "").replace(/^\s+/, "");
          if (!delta) break;
          hideWorking();
          if (!bubble) {
            bubble = addBot("");
            spoken.push(bubble);
          }
          setBot(bubble, (said.get(bubble) ?? "") + delta);
          break;
        }
        case "draft":
          hideWorking();
          bubble = null;
          renderDraft(d);
          break;
        case "confirm":
          hideWorking();
          bubble = null;
          renderConfirm(d);
          break;
        case "saved":
          hideWorking();
          bubble = null;
          if (Array.isArray(d.items) && d.items.length) renderSaved(d.items);
          break;
        case "note":
          window.dispatchEvent(new CustomEvent("assistant:note", { detail: { text: d.text } }));
          break;
        case "failed":
          hideWorking();
          showError(d.message || "Something went wrong. Please try again.");
          break;
        case "done":
          if (d.capped) showContinue();
          break;
      }
    };
    try {
      await api.send(threadId, text, sent.map((c) => c.fileId!), onEvent, accept, draftId);
    } catch (e) {
      if (!accepted) giveBack();
      if (e instanceof AuthLost) stop(SIGN_IN);
      else showError(errorText(e));
    } finally {
      hideWorking();
      for (const m of spoken) if (m.isConnected) addActions(m);
      busy = false;
      refresh();
      if (!signedOut) input.focus();
    }
  }
  send.addEventListener("click", submit);
  const coarse = () => typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches;
  input.addEventListener("keydown", (e) => {
    const k = e as KeyboardEvent;
    if (k.key === "Enter" && !k.shiftKey && !k.isComposing && !coarse()) {
      e.preventDefault();
      submit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  });

  refresh();
  return { host, root, open, close };
}
