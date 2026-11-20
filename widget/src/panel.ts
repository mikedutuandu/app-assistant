/** The button and the chat panel, in a shadow root. Server and user text only ever goes in through textContent. */
import { ApiError, AuthLost, type BotApi, type BotEvent, type Draft } from "./api";
import { startRecording, type Recording } from "./recorder";
import { CSS } from "./styles";

export type PanelApi = Pick<BotApi, "send" | "upload" | "act" | "thread">;
export interface PanelOptions {
  api: PanelApi;
  app: string;
  label: string;
  position: "left" | "right";
  parent?: HTMLElement;
}

const SIGN_IN = "Please sign in again.";
/** What the bot says it is doing, by the server's `status` step. A step not listed here changes nothing. */
const STEP_LABELS: Record<string, string> = {
  reading: "Reading your files…",
  listening: "Listening to your voice note…",
  thinking: "Thinking…",
  writing: "Writing your posts…",
  checking: "Checking every number…",
};
const SHORTER = "Make it shorter";
const ANOTHER_ANGLE = "Try another angle";
const CHECK_ICON =
  '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
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
  const launcher = h("button", { class: "launcher", type: "button", "aria-haspopup": "dialog", "aria-expanded": "false", text: opts.label });
  const list = h("div", { class: "list", role: "log", "aria-live": "polite", "aria-label": "Conversation" });
  const status = h("div", { class: "status", "aria-live": "polite" });
  const chipsEl = h("div", { class: "chips" });
  const input = h("textarea", { rows: 1, placeholder: "Tell me what you did…", "aria-label": "Your message", maxlength: 4000 });
  const fileInput = h("input", { type: "file", accept: "image/*,application/pdf,audio/*", hidden: true, tabindex: -1 });
  fileInput.multiple = true;
  const attach = h("button", { class: "icon", type: "button", "aria-label": "Attach a file", text: "📎" });
  const mic = h("button", { class: "icon mic", type: "button", "aria-label": "Hold to record a voice note", text: "🎤" });
  const send = h("button", { class: "icon send", type: "button", "aria-label": "Send", text: "➤" });
  const closeBtn = h("button", { class: "icon", type: "button", "aria-label": "Close", text: "✕" });
  const panel = h(
    "div",
    { class: "panel", role: "dialog", "aria-label": opts.label, hidden: true },
    h("header", {}, h("h2", { text: opts.label }), closeBtn),
    list,
    status,
    chipsEl,
    h("div", { class: "composer" }, attach, mic, input, send, fileInput),
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
          addMsg("msg user", m.text);
          continue;
        }
        if (m.text) addMsg("msg bot", m.text);
        for (const d of m.drafts ?? []) renderDraft(d);
        if (m.saved?.length) renderSaved(m.saved);
      }
    } catch (e) {
      if (e instanceof AuthLost) stop(SIGN_IN);
      else if (e instanceof ApiError && e.status === 404) store.set(null); // the thread is gone: start fresh
      else {
        showError(errorText(e));
        loaded = false; // a passing failure: try again next time the panel opens
      }
    }
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
    // The spoken name starts with the visible text (WCAG 2.5.3) and follows it: Add, Adding, Added.
    const setAdd = (text: string, label: string) => {
      add.textContent = text;
      add.setAttribute("aria-label", label);
    };
    setAdd("Add to Schedule", "Add to Schedule: this draft");
    const shorter = h("button", { class: "btn ghost", type: "button", text: "Shorter", "aria-label": "Make this draft shorter" });
    const angle = h("button", { class: "btn ghost", type: "button", text: "Another angle", "aria-label": "Try another angle for this draft" });
    const err = h("div", { class: "small", role: "alert" });
    const buttons = [add, shorter, angle];
    let pending = false;
    const sync = () => {
      for (const b of buttons) b.disabled = busy || signedOut;
      if (pending || add.classList.contains("done")) add.disabled = true;
    };
    syncers.add(sync);
    const markAdded = (editUrl: string | null | undefined) => {
      setAdd("Added to your Schedule", "Added to your Schedule: this draft");
      add.classList.add("done");
      add.disabled = true;
      const href = editUrl ? safeHref(editUrl) : null;
      if (href) row.append(h("a", { class: "btn ghost", href, "aria-label": "Edit this draft in your Schedule", text: "Edit" }));
    };
    add.addEventListener("click", async () => {
      pending = true;
      add.disabled = true;
      setAdd("Adding…", "Adding to Schedule: this draft");
      err.textContent = "";
      try {
        const r = await api.act(threadId!, { action_id: actionId, kind: "add_draft", draft_id: d.id });
        pending = false;
        markAdded(r.edit_url);
        window.dispatchEvent(new CustomEvent("assistant:saved", { detail: { draft_id: r.draft_id } }));
      } catch (e) {
        pending = false;
        setAdd("Add to Schedule", "Add to Schedule: this draft");
        sync();
        if (e instanceof AuthLost) stop(SIGN_IN);
        else err.textContent = errorText(e);
      }
    });
    shorter.addEventListener("click", () => void sendTurn(SHORTER, [], d.id));
    angle.addEventListener("click", () => void sendTurn(ANOTHER_ANGLE, [], d.id));
    row.prepend(add, shorter, angle);
    el.append(row, err);
    if (d.added) markAdded(d.edit_url);
    sync();
    list.append(el);
    scroll();
  }

  /** The quiet line under a turn: what the bot remembered. Not a message, nothing to press. */
  function renderSaved(items: string[]) {
    const text = items.join("; ");
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
    const x = h("button", { class: "icon x", type: "button", "aria-label": `Remove ${file.name}`, text: "✕" });
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
    const fromComposer = draftId === undefined;
    busy = true;
    refresh();
    const mine = addMsg("msg user", [text, ...sent.map((c) => `📎 ${c.file.name}`)].filter(Boolean).join("\n"));
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
          if (!bubble) bubble = addMsg("msg bot", "");
          bubble.textContent += delta;
          scroll();
          break;
        }
        case "draft":
          hideWorking();
          bubble = null;
          renderDraft(d);
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
