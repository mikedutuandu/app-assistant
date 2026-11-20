/** Voice notes with MediaRecorder: the first container the browser supports (iPhone Safari records mp4). */

const CANDIDATES: [mime: string, ext: string][] = [
  ["audio/webm;codecs=opus", "webm"],
  ["audio/mp4", "m4a"],
  ["audio/ogg", "ogg"],
];

export function pickMime(): string | undefined {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") return undefined;
  return CANDIDATES.find(([m]) => MediaRecorder.isTypeSupported(m))?.[0];
}

export interface Recording {
  stop(): Promise<File>;
  cancel(): void;
}

export async function startRecording(): Promise<Recording> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const mime = pickMime();
  const release = () => stream.getTracks().forEach((t) => t.stop());
  let recorder: MediaRecorder;
  try {
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
  } catch (e) {
    release();
    throw e;
  }
  const chunks: Blob[] = [];
  recorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) chunks.push(e.data);
  };
  recorder.start();
  return {
    stop: () =>
      new Promise<File>((resolve) => {
        recorder.onstop = () => {
          release();
          const type = (recorder.mimeType || mime || "audio/webm").split(";")[0];
          const ext = CANDIDATES.find(([m]) => m.split(";")[0] === type)?.[1] ?? "webm";
          resolve(new File(chunks, `voice.${ext}`, { type: mime ?? type }));
        };
        if (recorder.state === "inactive") recorder.onstop?.(new Event("stop"));
        else recorder.stop();
      }),
    cancel() {
      recorder.onstop = null;
      if (recorder.state !== "inactive") recorder.stop();
      release();
    },
  };
}
