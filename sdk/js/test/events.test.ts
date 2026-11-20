import { describe, expect, it } from "vitest";
import { createAssistantHost, readEvents } from "../src/server/index";
import { SECRET } from "./helpers";

function stream(...chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) {
      for (const chunk of chunks) c.enqueue(enc.encode(chunk));
      c.close();
    },
  });
}

async function all(body: ReadableStream<Uint8Array>) {
  const out: { event: string; data: unknown }[] = [];
  for await (const e of readEvents(body)) out.push(e);
  return out;
}

describe("readEvents", () => {
  it("reads events with JSON data", async () => {
    expect(
      await all(stream('event: status\ndata: {"step":"thinking"}\n\nevent: done\ndata: {"cost_usd":0.01}\n\n')),
    ).toEqual([
      { event: "status", data: { step: "thinking" } },
      { event: "done", data: { cost_usd: 0.01 } },
    ]);
  });

  it("handles CRLF and CR line endings, even split across chunks", async () => {
    expect(
      await all(stream('event: text\r\ndata: {"delta":"a"}\r', '\n\r\nevent: text\rdata: {"delta":"b"}\r\r')),
    ).toEqual([
      { event: "text", data: { delta: "a" } },
      { event: "text", data: { delta: "b" } },
    ]);
  });

  it("joins multi-line data with newlines", async () => {
    expect(
      await all(
        stream('event: draft\ndata: {"id": "d1",\ndata:  "posts": ["x"]}\n\n', "data: line one\ndata:line two\n\n"),
      ),
    ).toEqual([
      { event: "draft", data: { id: "d1", posts: ["x"] } },
      { event: "message", data: "line one\nline two" },
    ]);
  });

  it("skips comments, unknown fields and events without data", async () => {
    expect(
      await all(stream(': ping\n\nid: 7\nretry: 100\n\n:keep-alive\nevent: note\nfoo: bar\ndata: {"text":"hi"}\n\n')),
    ).toEqual([{ event: "note", data: { text: "hi" } }]);
  });

  it("splits events across arbitrary chunks, including inside a character", async () => {
    const bytes = new TextEncoder().encode('event: text\ndata: {"delta":"héllo ✓"}\n\n');
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        for (const b of bytes) c.enqueue(new Uint8Array([b]));
        c.close();
      },
    });
    expect(await all(body)).toEqual([{ event: "text", data: { delta: "héllo ✓" } }]);
  });

  it("drops an event the stream ended in the middle of", async () => {
    expect(await all(stream('event: done\ndata: {"a":1}\n\nevent: text\ndata: {"delta":"cut'))).toEqual([
      { event: "done", data: { a: 1 } },
    ]);
  });

  it("stops reading when the caller stops", async () => {
    let cancelled = false;
    const enc = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      pull(c) {
        c.enqueue(enc.encode('event: text\ndata: {"delta":"x"}\n\n'));
      },
      cancel() {
        cancelled = true;
      },
    });
    for await (const e of readEvents(body)) {
      expect(e.event).toBe("text");
      break;
    }
    expect(cancelled).toBe(true);
  });

  it("is on the host too", () => {
    const host = createAssistantHost({
      appId: "shipnote",
      secret: SECRET,
      context: async () => ({ language: "en" }),
      saveDraft: async () => ({ draft_id: "d", edit_url: null }),
      saveMaterial: async () => {},
      charge: async () => ({ ok: true }),
    });
    expect(host.readEvents).toBe(readEvents);
  });
});
