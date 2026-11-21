/**
 * `@app-assistant/host/react`: the chat window. Loads the bot's `widget.js` once per page with the data attributes
 * it reads; the widget draws its own floating open button and panel.
 */
import { type ReactNode, useEffect, useRef } from "react";
import { mountAppView, type AppViewData, type AppViewOptions } from "../view/index";

export type { AppViewData, AppViewOptions } from "../view/index";

export interface AssistantChatProps {
  /** Your app id. */
  app: string;
  /** The bot's address, e.g. `https://bot.example.com`. */
  botUrl: string;
  /** Your route that answers `{token}` for the signed-in user. Default `/api/assistant/session`. */
  sessionUrl?: string;
  /** The open button's words. The widget's default is "Write with AI". */
  label?: string;
  position?: "left" | "right";
  /** The draft's time picker, in your app's words ("Post at", "Send at"). Default "Publish at". */
  scheduleLabel?: string;
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
function loadWidget({ app, botUrl, sessionUrl, label, position, scheduleLabel }: AssistantChatProps): void {
  if (document.querySelector(`script[${MARK}]`) || document.getElementById("assistant-widget")) return;
  const script = document.createElement("script");
  script.src = `${botUrl.replace(/\/+$/, "")}/widget.js`;
  script.async = true;
  script.setAttribute(MARK, "");
  script.dataset.app = app;
  script.dataset.sessionUrl = sessionUrl ?? "/api/assistant/session";
  if (label) script.dataset.label = label;
  if (position) script.dataset.position = position;
  if (scheduleLabel) script.dataset.scheduleLabel = scheduleLabel;
  document.body.appendChild(script);
}

export function AssistantChat(props: AssistantChatProps) {
  const { app, botUrl, sessionUrl, label, position, scheduleLabel, children, className } = props;
  useEffect(
    () => loadWidget({ app, botUrl, sessionUrl, label, position, scheduleLabel }),
    [app, botUrl, sessionUrl, label, position, scheduleLabel],
  );
  if (children === undefined || children === null) return null;
  return (
    <button type="button" className={className} onClick={openAssistant}>
      {children}
    </button>
  );
}

export interface AppViewProps extends AppViewOptions {
  /** A card from the bot (a `view` event, or one of a thread read's `views`). */
  view: AppViewData;
  className?: string;
}

/**
 * An app's card in your own chat (MCP Apps): its page in a sandboxed frame, given the tool's input and result. Draw
 * it where the bot sent it, before the words that follow. `onMessage` sends what the card asks as the user's message.
 */
export function AppView({ view, className, onMessage, onOpenLink, maxHeight, locale, theme }: AppViewProps) {
  const box = useRef<HTMLDivElement>(null);
  const handlers = useRef({ onMessage, onOpenLink });
  handlers.current = { onMessage, onOpenLink };
  useEffect(() => {
    if (!box.current) return;
    const mounted = mountAppView(box.current, view, {
      onMessage: (text) => handlers.current.onMessage?.(text),
      onOpenLink: handlers.current.onOpenLink ? (url) => handlers.current.onOpenLink?.(url) : undefined,
      maxHeight,
      locale,
      theme,
    });
    return () => mounted.destroy();
    // A card's page never changes once shown: draw it again only for another card.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view.id, view.html, maxHeight, locale, theme]);
  return <div ref={box} className={className} data-app-view={view.tool} />;
}
