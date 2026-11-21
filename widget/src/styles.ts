/**
 * The widget's CSS, inside its shadow root. Themed by the host's --assistant-accent, --assistant-radius and
 * --assistant-font; --assistant-bottom lifts the open button above the host's own bottom bar.
 *
 * The look (Mike, 21 Nov 2026: "refer Claude chat"): a calm window, the assistant's words with no bubble at full width,
 * the user's in a soft grey bubble, one rounded box to write in with thin line icons and a round send button, quiet
 * actions under an answer. The app's accent is the one colour: the send button, the main buttons, the open button.
 */
export const CSS = `
:host { all: initial; --a: var(--assistant-accent, #18181b); --r: var(--assistant-radius, 14px);
  --f: var(--assistant-font, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif);
  --ink: #1c1c1e; --muted: #6e6e73; --faint: #8e8e93; --line: #e6e6e9; --bg: #fff; --soft: #f3f3f5; --hover: #ececef;
  --bad: #b42318; --ok: #1a7f37; }
* { box-sizing: border-box; font-family: var(--f); }
button, input, textarea, select { font: inherit; color: inherit; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: .5; }
:focus-visible { outline: 2px solid var(--a); outline-offset: 2px; }
[hidden] { display: none !important; }
svg { display: block; flex: none; }

/* The open button */
.launcher { position: fixed; bottom: calc(20px + var(--assistant-bottom, 0px)); transition: bottom .2s; z-index: 2147483000;
  display: inline-flex; align-items: center; gap: 8px; background: var(--a); color: #fff; border: 0; border-radius: 999px;
  padding: 11px 18px 11px 14px; font-size: 15px; font-weight: 600; letter-spacing: -.01em;
  box-shadow: 0 1px 2px rgba(0,0,0,.12), 0 8px 24px rgba(0,0,0,.18); }
.launcher:hover { filter: brightness(1.08); }
.right .launcher, .right .panel { right: 20px; }
.left .launcher, .left .panel { left: 20px; }

/* The window */
.panel { position: fixed; bottom: 20px; z-index: 2147483001; width: 420px; max-width: calc(100vw - 40px); height: 680px;
  max-height: calc(100vh - 40px); display: flex; flex-direction: column; background: var(--bg); color: var(--ink);
  border: 1px solid var(--line); border-radius: calc(var(--r) + 4px); overflow: hidden;
  box-shadow: 0 2px 6px rgba(0,0,0,.06), 0 24px 64px rgba(0,0,0,.18); font-size: 15px; line-height: 1.55; }
@media (max-width: 639px) {
  .panel { inset: 0 !important; width: 100%; max-width: none; height: 100vh; height: 100dvh; max-height: none; border: 0;
    border-radius: 0; }
}
header { display: flex; align-items: center; gap: 8px; padding: 10px 10px 10px 18px; border-bottom: 1px solid var(--line); }
header h2 { margin: 0; flex: 1; min-width: 0; font-size: 15px; font-weight: 600; letter-spacing: -.01em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.head-tools { display: flex; gap: 2px; }
.icon { background: none; border: 0; border-radius: 10px; width: 36px; height: 36px; display: inline-grid; place-items: center;
  color: var(--muted); }
.icon:hover:not(:disabled) { background: var(--soft); color: var(--ink); }
.icon[aria-expanded="true"] { background: var(--soft); color: var(--ink); }

/* The conversation */
.list { flex: 1; overflow-y: auto; padding: 20px 18px 8px; display: flex; flex-direction: column; gap: 14px;
  overscroll-behavior: contain; }
.msg { overflow-wrap: anywhere; }
.user { align-self: flex-end; max-width: 85%; background: var(--soft); color: var(--ink); border-radius: 18px;
  padding: 9px 14px; white-space: pre-wrap; }
.bot { align-self: stretch; color: var(--ink); }
.bot p { margin: 0 0 10px; }
.bot p:last-child, .bot ul:last-child, .bot ol:last-child { margin-bottom: 0; }
.bot ul, .bot ol { margin: 0 0 10px; padding-left: 22px; }
.bot li { margin: 3px 0; }
.bot li > ul, .bot li > ol { margin: 3px 0 0; }
.bot strong { font-weight: 600; }
.bot code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .88em; background: var(--soft);
  border-radius: 5px; padding: 1px 5px; }
.bot a { color: inherit; text-decoration: underline; text-underline-offset: 2px; text-decoration-color: var(--faint); }
.acts { display: flex; gap: 2px; margin: -8px 0 0 -8px; }
.acts .icon { width: 30px; height: 30px; border-radius: 8px; color: var(--faint); }
.heard { align-self: flex-end; color: var(--muted); font-size: 13px; font-style: italic; padding: 0 4px; max-width: 85%;
  overflow-wrap: anywhere; }
.working { align-self: flex-start; display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 14px; }
.dots { display: inline-flex; gap: 4px; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--a); opacity: .3; animation: assistant-dot 1.2s ease-in-out infinite; }
.dot:nth-child(2) { animation-delay: .2s; }
.dot:nth-child(3) { animation-delay: .4s; }
@keyframes assistant-dot { 0%, 80%, 100% { opacity: .3; } 40% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .dot { animation: none; opacity: .7; } }
.err { align-self: stretch; color: var(--bad); font-size: 14px; background: #fef3f2; border-radius: 10px; padding: 8px 12px; }
.saved { align-self: flex-start; display: flex; align-items: flex-start; gap: 6px; color: var(--muted); font-size: 13px;
  overflow-wrap: anywhere; }
.saved svg { margin-top: 3px; color: var(--ok); }

/* What the user sent: pictures as rounded thumbnails (a tap shows them whole), a voice note as a player, a file as a chip */
.user .sent { margin-bottom: 6px; }
.user .sent:last-child { margin-bottom: 0; }
.thumb { display: block; padding: 0; border: 0; background: none; border-radius: 12px; overflow: hidden; }
.thumb img { display: block; max-width: 220px; max-height: 180px; object-fit: cover; }
.user audio { display: block; max-width: 240px; height: 36px; }
.file { display: inline-flex; align-items: center; gap: 6px; color: var(--ink); text-decoration: none; font-size: 14px;
  background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: 6px 10px; }
.viewer { position: absolute; inset: 0; z-index: 5; background: rgba(15,15,18,.88); display: grid; place-items: center; padding: 24px; }
.viewer img { max-width: 100%; max-height: 100%; border-radius: 10px; }
.viewer .icon { position: absolute; top: 10px; right: 10px; color: #fff; }
.viewer .icon:hover { background: rgba(255,255,255,.14); color: #fff; }

/* An app's card (MCP Apps): its own page, in a frame as wide as the answer */
.view { align-self: stretch; }
.view.framed { border: 1px solid var(--line); border-radius: var(--r); overflow: hidden; }
/* Cards: a draft, a change waiting for the user's OK */
.draft, .confirm { align-self: stretch; border: 1px solid var(--line); border-radius: var(--r); padding: 14px;
  background: var(--bg); box-shadow: 0 1px 2px rgba(0,0,0,.04); }
.draft .post { margin: 0 0 10px; white-space: pre-wrap; overflow-wrap: anywhere; }
.draft ol.posts { margin: 0 0 10px; padding-left: 22px; }
.draft ol.posts .post { margin: 0 0 8px; padding-left: 2px; }
.draft img { display: block; max-width: 100%; max-height: 220px; border-radius: 10px; margin-bottom: 10px; }
.draft .when { display: flex; align-items: center; gap: 8px; margin: 0 0 10px; font-size: 14px; color: var(--muted); }
.draft .when select { min-height: 34px; border: 1px solid var(--line); border-radius: 8px; padding: 4px 8px; background: var(--bg); }
.draft .note { font-size: 13px; color: var(--muted); margin-top: 8px; }
.draft .note:empty { display: none; }
.confirm .title { margin: 0 0 6px; font-weight: 600; }
.confirm ul { margin: 0 0 12px; padding-left: 18px; color: var(--muted); font-size: 14px; }
.confirm .state { margin: 0; font-size: 14px; color: var(--muted); overflow-wrap: anywhere; }
.confirm .state.done { color: var(--ok); }
.confirm .state.failed { color: var(--bad); }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.btn { min-height: 34px; display: inline-flex; align-items: center; border: 1px solid var(--a); background: var(--a); color: #fff;
  border-radius: 999px; padding: 6px 14px; font-size: 14px; font-weight: 500; text-decoration: none; }
.btn.ghost { background: var(--bg); color: var(--ink); border-color: var(--line); }
.btn.ghost:hover:not(:disabled) { background: var(--soft); }
.btn.done { background: var(--bg); color: var(--ok); border-color: var(--line); }
.small { font-size: 13px; color: var(--bad); }
.small:empty { display: none; }

/* One-tap starts: the app's starters, and Continue under an answer the cap cut short */
.empty { margin: auto 0 0; display: flex; flex-direction: column; gap: 14px; }
.hello { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -.02em; color: var(--ink); }
.starters { display: flex; flex-wrap: wrap; gap: 8px; align-self: stretch; }
.starter { border: 1px solid var(--line); background: var(--bg); color: var(--ink); border-radius: 999px; padding: 7px 14px;
  font-size: 14px; min-height: 36px; text-align: left; }
.starter:hover:not(:disabled) { background: var(--soft); }

/* Past chats */
.history { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 2px; }
.past { display: flex; align-items: baseline; gap: 12px; width: 100%; border: 0; background: none; text-align: left;
  border-radius: 10px; padding: 10px 12px; }
.past:hover, .past.current { background: var(--soft); }
.past-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.past-when { color: var(--faint); font-size: 13px; flex: none; }
.quiet { margin: 16px 12px; color: var(--muted); font-size: 14px; }

/* Writing: one rounded box, the words on top and the tools under them */
.foot { padding: 6px 14px 14px; }
.status { padding: 0 6px 6px; font-size: 13px; color: var(--muted); }
.status:empty { display: none; }
.composer { border: 1px solid var(--line); border-radius: 22px; background: var(--bg); padding: 10px 10px 8px 14px;
  box-shadow: 0 1px 3px rgba(0,0,0,.05); transition: border-color .15s, box-shadow .15s; }
.composer:focus-within { border-color: #c9c9ce; box-shadow: 0 2px 10px rgba(0,0,0,.08); }
.composer textarea { display: block; width: 100%; resize: none; max-height: 140px; min-height: 24px; border: 0; outline: 0;
  padding: 2px 0 6px; font-size: 16px; line-height: 1.45; background: transparent; }
.composer textarea::placeholder { color: var(--faint); }
.tools { display: flex; align-items: center; gap: 2px; margin-left: -6px; }
.grow { flex: 1; }
.mic.rec { background: var(--bad); color: #fff; }
.send { width: 34px; height: 34px; border-radius: 999px; background: var(--a); color: #fff; }
.send:hover:not(:disabled) { background: var(--a); color: #fff; filter: brightness(1.1); }
.send:disabled { opacity: .3; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; }
.chips:empty { display: none; }
.chip { display: flex; align-items: center; gap: 6px; border: 1px solid var(--line); background: var(--soft); border-radius: 10px;
  padding: 4px 4px 4px 10px; font-size: 13px; max-width: 100%; }
.chip.bad { border-color: var(--bad); }
.chip .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 160px; }
.chip progress { width: 48px; height: 6px; accent-color: var(--a); }
.chip .msg-bad { color: var(--bad); }
.chip .x { width: 24px; height: 24px; font-size: 16px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
`;
