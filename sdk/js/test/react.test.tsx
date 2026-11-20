// @vitest-environment jsdom
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AssistantChat, openAssistant } from "../src/react/index";

afterEach(() => {
  cleanup();
  document.querySelectorAll("script").forEach((s) => s.remove());
});

const scripts = () => [...document.querySelectorAll<HTMLScriptElement>("script[data-app]")];

describe("AssistantChat", () => {
  it("loads the bot's widget.js once, with the attributes the widget reads", () => {
    const { rerender } = render(
      <AssistantChat
        app="shipnote"
        botUrl="https://bot.example.com/"
        sessionUrl="/api/assistant/session"
        label="Write"
        position="left"
      />,
    );
    rerender(
      <AssistantChat
        app="shipnote"
        botUrl="https://bot.example.com/"
        sessionUrl="/api/assistant/session"
        label="Write"
        position="left"
      />,
    );
    render(<AssistantChat app="shipnote" botUrl="https://bot.example.com" sessionUrl="/api/assistant/session" />);
    expect(scripts()).toHaveLength(1);
    const s = scripts()[0];
    expect(s.src).toBe("https://bot.example.com/widget.js");
    expect(s.async).toBe(true);
    expect(s.dataset).toMatchObject({
      app: "shipnote",
      sessionUrl: "/api/assistant/session",
      label: "Write",
      position: "left",
    });
  });

  it("leaves label and position out when not given", () => {
    render(<AssistantChat app="shipnote" botUrl="https://bot.example.com" sessionUrl="/s" />);
    const s = scripts()[0];
    expect(s.dataset.label).toBeUndefined();
    expect(s.dataset.position).toBeUndefined();
  });

  it("renders nothing of its own by default: the widget draws its open button", () => {
    const { container } = render(<AssistantChat app="shipnote" botUrl="https://bot.example.com" sessionUrl="/s" />);
    expect(container.innerHTML).toBe("");
  });

  it("renders an open button from children that opens the chat", () => {
    const opened = vi.fn();
    window.addEventListener("assistant:open", opened);
    const { getByRole } = render(
      <AssistantChat app="shipnote" botUrl="https://bot.example.com" sessionUrl="/s" className="btn">
        Write with me
      </AssistantChat>,
    );
    const button = getByRole("button", { name: "Write with me" });
    expect(button.getAttribute("type")).toBe("button");
    expect(button.className).toBe("btn");
    fireEvent.click(button);
    openAssistant();
    expect(opened).toHaveBeenCalledTimes(2);
    window.removeEventListener("assistant:open", opened);
  });
});
