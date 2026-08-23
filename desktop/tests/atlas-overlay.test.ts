import { afterEach, describe, expect, it, vi } from "vitest";
import {
  initialAtlasOverlayState,
  isValidAtlasOverlayHotkey,
  normalizeAtlasOverlayConfig,
  reduceAtlasOverlayState,
} from "../src/shared/atlas-overlay";
import {
  parseServerSentEventJson,
  ServerSentEventDecoder,
} from "../src/shared/atlas-overlay-sse";
import { normalizeRussianKeyboardInput, sanitizeOverlaySpeech } from "../src/renderer/overlay/overlaySpeech";
import { OverlayVoiceCapture } from "../src/renderer/overlay/voiceCapture";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Atlas Overlay state contract", () => {
  it("starts a fresh capture and streams the answer without retaining old text", () => {
    const previous = {
      ...initialAtlasOverlayState,
      visible: true,
      stage: "answer" as const,
      answer: "Старый ответ",
    };
    const listening = reduceAtlasOverlayState(previous, { type: "ptt", phase: "down" });
    expect(listening).toMatchObject({ visible: true, stage: "listening", answer: "" });

    const transcribing = reduceAtlasOverlayState(listening, { type: "ptt", phase: "up" });
    const transcript = reduceAtlasOverlayState(transcribing, {
      type: "transcript",
      text: "Какие у меня права?",
    });
    const first = reduceAtlasOverlayState(transcript, { type: "delta", text: "Вы " });
    const second = reduceAtlasOverlayState(first, { type: "delta", text: "вправе..." });
    expect(second).toMatchObject({
      stage: "answer",
      transcript: "Какие у меня права?",
      answer: "Вы вправе...",
    });
  });

  it("keeps retry information and clears private answer data when hidden", () => {
    const failed = reduceAtlasOverlayState(
      { ...initialAtlasOverlayState, visible: true, answer: "частичный ответ" },
      { type: "error", message: "Нет связи", retryable: true },
    );
    expect(failed).toMatchObject({ stage: "error", error: "Нет связи", retryable: true });
    expect(reduceAtlasOverlayState(failed, { type: "hide" })).toEqual(initialAtlasOverlayState);
  });
});

describe("Atlas Overlay hotkey contract", () => {
  it("accepts bounded Electron accelerators and rejects commands or duplicate modifiers", () => {
    expect(isValidAtlasOverlayHotkey("Control+Shift+A")).toBe(true);
    expect(isValidAtlasOverlayHotkey("F10")).toBe(true);
    expect(isValidAtlasOverlayHotkey("CommandOrControl+Space")).toBe(true);
    expect(isValidAtlasOverlayHotkey("Control+Control+A")).toBe(false);
    expect(isValidAtlasOverlayHotkey("Control+rm -rf /")).toBe(false);
    expect(isValidAtlasOverlayHotkey("Control+Shift+A+unexpected+extra")).toBe(false);
  });

  it("clamps visual and speech values and keeps screen capture opt-in", () => {
    const config = normalizeAtlasOverlayConfig({
      enabled: true,
      hotkey: "not-a-hotkey",
      speechRate: 99,
      speechVolume: -2,
      opacity: 0.1,
      scale: 4,
      fontScale: 4,
      positionX: -3,
      positionY: 6,
      speechProvider: "system",
      captureInRecordings: false,
      calibrationMode: true,
      screenContextEnabled: false,
    });
    expect(config.hotkey).toBe("Control+Shift+A");
    expect(config.speechRate).toBe(1.45);
    expect(config.speechVolume).toBe(0);
    expect(config.opacity).toBe(0.68);
    expect(config.scale).toBe(1.18);
    expect(config.fontScale).toBe(1.28);
    expect(config.calibrationMode).toBe(true);
    expect(config.positionX).toBe(0);
    expect(config.positionY).toBe(1);
    expect(config.speechProvider).toBe("system");
    expect(config.captureInRecordings).toBe(false);
    expect(config.screenContextEnabled).toBe(false);
  });
});

describe("Atlas Overlay SSE decoder", () => {
  it("preserves Cyrillic characters split between byte chunks", () => {
    const encoded = new TextEncoder().encode(
      'data:{"type":"delta","text":"Право на защиту"}\n\n',
    );
    const decoder = new ServerSentEventDecoder();
    const split = encoded.findIndex((byte, index) => byte >= 0x80 && encoded[index + 1] >= 0x80) + 1;
    const events = [
      ...decoder.push(encoded.slice(0, split)),
      ...decoder.push(encoded.slice(split)),
      ...decoder.finish(),
    ];
    expect(events).toHaveLength(1);
    expect(parseServerSentEventJson(events[0])).toEqual({
      type: "delta",
      text: "Право на защиту",
    });
  });

  it("supports comments and multi-line data without executing it", () => {
    const decoder = new ServerSentEventDecoder();
    const events = decoder.push(new TextEncoder().encode(
      ": heartbeat\nevent: note\ndata: первая\ndata: вторая\n\n",
    ));
    expect(events).toEqual([{ event: "note", data: "первая\nвторая" }]);
  });
});

describe("Atlas Overlay speech delivery", () => {
  it("removes URLs, source markers, markdown and control characters before local TTS", () => {
    expect(sanitizeOverlaySpeech(
      "## Ответ\nСмотрите [правило](https://example.org/x) [Источник 2]. `Текст`\u0000",
    )).toBe("Ответ Смотрите правило . Текст");
  });

  it("recognizes a fast Russian request typed in the English keyboard layout", () => {
    expect(normalizeRussianKeyboardInput("ghbdtn rfr ltkf")).toBe("привет как дела");
    expect(normalizeRussianKeyboardInput("atlas help")).toBe("atlas help");
  });
});

describe("Atlas Overlay voice capture lifecycle", () => {
  it("keeps the first clip when PTT is released before getUserMedia resolves", async () => {
    let resolveStream!: (stream: MediaStream) => void;
    const stream = {
      getTracks: () => [{ stop: vi.fn() }],
    } as unknown as MediaStream;
    const mediaPromise = new Promise<MediaStream>((resolve) => {
      resolveStream = resolve;
    });

    class FakeMediaRecorder {
      static isTypeSupported = () => true;
      state: RecordingState = "inactive";
      mimeType = "audio/webm;codecs=opus";
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;

      constructor(_stream: MediaStream, _options?: MediaRecorderOptions) {}
      start(): void { this.state = "recording"; }
      requestData(): void {}
      stop(): void {
        if (this.state === "inactive") return;
        this.state = "inactive";
        queueMicrotask(() => {
          this.ondataavailable?.({
            data: new Blob([new Uint8Array(96)], { type: this.mimeType }),
          } as BlobEvent);
          this.onstop?.();
        });
      }
    }

    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
    vi.stubGlobal("navigator", {
      mediaDevices: { getUserMedia: vi.fn(() => mediaPromise) },
    });

    const capture = new OverlayVoiceCapture();
    const starting = capture.start();
    const stopping = capture.stop();
    resolveStream(stream);

    await starting;
    const result = await stopping;
    expect(result?.audio.byteLength).toBe(96);
    expect(result?.mimeType).toContain("audio/webm");
  });
});
