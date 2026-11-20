/** The widget's CSS, inside its shadow root. Themed by the host's --assistant-accent, --assistant-radius and --assistant-font. */
export const CSS = `
:host { all: initial; --a: var(--assistant-accent, #111); --r: var(--assistant-radius, 12px);
  --f: var(--assistant-font, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif);
  --ink: #111827; --muted: #6b7280; --line: #e5e7eb; --bg: #fff; --soft: #f4f4f5; --bad: #b91c1c; --ok: #15803d; }
* { box-sizing: border-box; font-family: var(--f); }
button, input, textarea { font: inherit; color: inherit; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: .6; }
:focus-visible { outline: 2px solid var(--a); outline-offset: 2px; }
.launcher { position: fixed; bottom: 20px; z-index: 2147483000; display: inline-flex; align-items: center; gap: 8px;
  background: var(--a); color: #fff; border: 0; border-radius: 999px; padding: 12px 18px; font-size: 15px; font-weight: 600;
  box-shadow: 0 6px 24px rgba(0,0,0,.25); }
.right .launcher, .right .panel { right: 20px; }
.left .launcher, .left .panel { left: 20px; }
.panel { position: fixed; bottom: 20px; z-index: 2147483001; width: 400px; max-width: calc(100vw - 40px); height: 640px;
  max-height: calc(100vh - 40px); display: flex; flex-direction: column; background: var(--bg); color: var(--ink);
  border: 1px solid var(--line); border-radius: var(--r); box-shadow: 0 12px 48px rgba(0,0,0,.28); overflow: hidden;
  font-size: 15px; line-height: 1.45; }
.panel[hidden], .launcher[hidden] { display: none; }
@media (max-width: 639px) {
  .panel { inset: 0 !important; width: 100%; max-width: none; height: 100vh; height: 100dvh; max-height: none; border: 0; border-radius: 0; }
}
header { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px; border-bottom: 1px solid var(--line); }
header h2 { margin: 0; font-size: 16px; }
.icon { background: none; border: 0; border-radius: 8px; width: 36px; height: 36px; display: inline-grid; place-items: center;
  font-size: 20px; color: var(--muted); }
.icon:hover:not(:disabled) { background: var(--soft); }
.list { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
.msg { max-width: 88%; padding: 9px 12px; border-radius: var(--r); white-space: pre-wrap; overflow-wrap: anywhere; }
.user { align-self: flex-end; background: var(--a); color: #fff; }
.bot { align-self: flex-start; background: var(--soft); }
.heard { align-self: flex-end; color: var(--muted); font-size: 13px; font-style: italic; padding: 0 4px; max-width: 88%; overflow-wrap: anywhere; }
.err { align-self: stretch; color: var(--bad); font-size: 14px; }
.card { align-self: stretch; display: flex; flex-direction: column; gap: 10px; }
.piece { border: 1px solid var(--line); border-radius: var(--r); padding: 12px; background: var(--bg); }
.piece p { margin: 0 0 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
.piece img { display: block; max-width: 100%; max-height: 200px; border-radius: 8px; margin-bottom: 8px; }
.piece.dropped { background: var(--soft); color: var(--muted); }
.piece .why { font-size: 13px; }
.row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.btn { border: 1px solid var(--a); background: var(--a); color: #fff; border-radius: 8px; padding: 7px 12px; font-size: 14px; font-weight: 600; text-decoration: none; }
.btn.ghost { background: transparent; color: var(--a); }
.btn.done { background: transparent; color: var(--ok); border-color: var(--ok); }
.facts { border: 1px dashed var(--line); border-radius: var(--r); padding: 12px; }
.facts h3 { margin: 0 0 2px; font-size: 14px; }
.facts .hint { margin: 0 0 8px; color: var(--muted); font-size: 13px; }
.fact { width: 100%; margin-bottom: 6px; border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; font-size: 14px; background: var(--bg); }
.small { font-size: 13px; color: var(--bad); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 14px; }
.chips:empty { display: none; }
.chip { display: flex; align-items: center; gap: 6px; border: 1px solid var(--line); background: var(--soft); border-radius: 999px; padding: 3px 4px 3px 10px; font-size: 13px; max-width: 100%; }
.chip.bad { border-color: var(--bad); }
.chip .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 140px; }
.chip progress { width: 48px; height: 6px; accent-color: var(--a); }
.chip .msg-bad { color: var(--bad); }
.chip .x { width: 24px; height: 24px; font-size: 16px; }
.composer { display: flex; align-items: flex-end; gap: 6px; padding: 10px 14px 14px; }
.composer textarea { flex: 1; resize: none; max-height: 120px; min-height: 40px; border: 1px solid var(--line); border-radius: var(--r); padding: 9px 12px; font-size: 16px; background: var(--bg); }
.mic.rec { background: var(--bad); color: #fff; }
.send { background: var(--a); color: #fff; }
.send:hover:not(:disabled) { background: var(--a); opacity: .9; }
.status { padding: 0 14px 4px; font-size: 13px; color: var(--muted); min-height: 0; }
.status:empty { display: none; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
`;
