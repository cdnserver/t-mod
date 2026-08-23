export interface VoiceCaptureResult {
  audio: ArrayBuffer;
  mimeType: string;
  durationMs: number;
}

const RECORDER_MIME_TYPES = [
  "audio/webm;codecs=opus",
  "audio/ogg;codecs=opus",
  "audio/webm",
] as const;

function supportedMimeType(): string | undefined {
  return RECORDER_MIME_TYPES.find((value) => MediaRecorder.isTypeSupported(value));
}

export class OverlayVoiceCapture {
  private generation = 0;
  private recorder: MediaRecorder | undefined;
  private stream: MediaStream | undefined;
  private chunks: Blob[] = [];
  private startedAt = 0;
  private startPromise: Promise<void> | undefined;
  private finishPromise: Promise<VoiceCaptureResult | null> | undefined;
  private finish: ((result: VoiceCaptureResult | null) => void) | undefined;
  private stopRequested = false;

  async start(deviceId = ""): Promise<void> {
    this.cancel();
    const generation = this.generation;
    this.stopRequested = false;
    this.startPromise = this.prepare(generation, deviceId);
    await this.startPromise;
  }

  async stop(): Promise<VoiceCaptureResult | null> {
    this.stopRequested = true;
    await this.startPromise?.catch(() => undefined);
    const recorder = this.recorder;
    const finished = this.finishPromise;
    if (!recorder || !finished) return null;
    if (recorder.state !== "inactive") {
      try {
        recorder.requestData();
      } catch {
        // Some Chromium builds reject requestData immediately before stop. The
        // final dataavailable event still contains the complete Opus payload.
      }
      recorder.stop();
    }
    // Releasing PTT while getUserMedia is still resolving makes prepare()
    // request the stop itself. The recorder is already inactive at this point,
    // but onstop still owns the complete first clip, so wait for it.
    return await finished;
  }

  cancel(): void {
    this.generation += 1;
    this.stopRequested = true;
    if (this.recorder && this.recorder.state !== "inactive") {
      this.recorder.ondataavailable = null;
      this.recorder.onstop = null;
      this.recorder.onerror = null;
      try {
        this.recorder.stop();
      } catch {
        // The recorder may already be transitioning to inactive.
      }
    }
    this.releaseStream();
    this.finish?.(null);
    this.resetRecorder();
  }

  private async prepare(generation: number, deviceId: string): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      throw new Error("Микрофон недоступен в этой версии системы.");
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      },
      video: false,
    });
    if (generation !== this.generation) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    this.stream = stream;
    this.chunks = [];
    this.startedAt = performance.now();
    const mimeType = supportedMimeType();
    const recorder = mimeType
      ? new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 48_000 })
      : new MediaRecorder(stream, { audioBitsPerSecond: 48_000 });
    this.recorder = recorder;
    this.finishPromise = new Promise((resolve) => {
      this.finish = resolve;
    });
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) this.chunks.push(event.data);
    };
    recorder.onerror = () => this.complete(null);
    recorder.onstop = () => void this.completeRecording(recorder.mimeType || mimeType || "audio/webm");
    recorder.start(180);
    if (this.stopRequested && recorder.state !== "inactive") recorder.stop();
  }

  private async completeRecording(mimeType: string): Promise<void> {
    const durationMs = Math.max(0, Math.round(performance.now() - this.startedAt));
    const blob = new Blob(this.chunks, { type: mimeType });
    const audio = await blob.arrayBuffer();
    this.complete(audio.byteLength >= 64 ? { audio, mimeType, durationMs } : null);
  }

  private complete(result: VoiceCaptureResult | null): void {
    this.releaseStream();
    this.finish?.(result);
    this.resetRecorder();
  }

  private releaseStream(): void {
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = undefined;
  }

  private resetRecorder(): void {
    this.recorder = undefined;
    this.startPromise = undefined;
    this.finishPromise = undefined;
    this.finish = undefined;
    this.chunks = [];
    this.startedAt = 0;
  }
}
