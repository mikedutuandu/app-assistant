/**
 * `@app-assistant/host/react`: the chat window. Loads the bot's `widget.js` once per page with the data attributes
 * it reads; the widget draws its own floating open button and panel.
 */
import { type ReactNode, useEffect } from "react";

export interface AssistantChatProps {
  /** Your app id. */
  app: string;
  /** The bot's address, e.g. `https://bot.example.com`. */
  botUrl: string;
  /** Your route that answers `{token}` for the signed-in user. Default `/api/assistant/session`. */
  sessionUrl?: string;
  /** The open button's words. The widget's default is "Write with me". */
  label?: string;
  position?: "left" | "right";
  /** An open button of your own, in your page's flow, as well as the widget's floating one. */
  children?: ReactNode;
  /** The class of your open button. */
  className?: string;
}

const MARK = "data-app-assistant-host";

/** Opens the chat window (the widget listens for `assistant:open`). */
export function openAssistant(): void {
  window.dispatchEvent(new Event("assistant:open"));
}

/** Adds the widget's script once per page. Later changes to the props don't reload it. */
function loadWidget({ app, botUrl, sessionUrl, label, position }: AssistantChatProps): void {
  if (document.querySelector(`script[${MARK}]`) || document.getElementById("assistant-widget")) return;
  const script = document.createElement("script");
  script.src = `${botUrl.replace(/\/+$/, "")}/widget.js`;
  script.async = true;
  script.setAttribute(MARK, "");
  script.dataset.app = app;
  script.dataset.sessionUrl = sessionUrl ?? "/api/assistant/session";
  if (label) script.dataset.label = label;
  if (position) script.dataset.position = position;
  document.body.appendChild(script);
}

export function AssistantChat(props: AssistantChatProps) {
  const { app, botUrl, sessionUrl, label, position, children, className } = props;
  useEffect(() => loadWidget({ app, botUrl, sessionUrl, label, position }), [app, botUrl, sessionUrl, label, position]);
  if (children === undefined || children === null) return null;
  return (
    <button type="button" className={className} onClick={openAssistant}>
      {children}
    </button>
  );
}
