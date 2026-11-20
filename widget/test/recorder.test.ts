import { afterEach, describe, expect, it, vi } from "vitest";
import { pickMime, startRecording } from "../src/recorder";

afterEach(() => vi.unstubAllGlobals());

function stubMedia(supported: string[]) {
  const made: { mimeType?: string }[] = [];
  class FakeRecorder {
    static isTypeSupported = (t: string) => supported.includes(t);
    ondataavailable: ((e: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;
    state = "inactive";
    mimeType: string;
    constructor(_s: unknown, opts: { mimeType?: string }) {
      made.push(opts);
      this.mimeType = opts.mimeType ?? "";
    }
    start() {
      this.state = "recording";
    }
    stop() {
      this.state = "inactive";
      this.ondataavailable?.({ data: new Blob(["abc"], { type: this.mimeType }) });
      this.onstop?.();
    }
  }
  const track = { stop: vi.fn() };
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: async () => ({ getTracks: () => [track] }) } });
  return { made, track };
}

describe("recorder", () => {
  it("picks audio/mp4 when webm unsupported", async () => {
    const { made, track } = stubMedia(["audio/mp4", "audio/ogg"]);
    expect(pickMime()).toBe("audio/mp4");
    const rec = await startRecording();
    const file = await rec.stop();
    expect(made[0].mimeType).toBe("audio/mp4");
    expect(file.type).toBe("audio/mp4");
    expect(file.name).toMatch(/\.m4a$/);
    expect(track.stop).toHaveBeenCalled();
  });

  it("prefers webm opus when supported", () => {
    stubMedia(["audio/webm;codecs=opus", "audio/mp4"]);
    expect(pickMime()).toBe("audio/webm;codecs=opus");
  });
});
