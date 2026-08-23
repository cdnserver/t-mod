import { contextBridge, ipcRenderer } from "electron";
import type {
  AtlasOverlayApi,
  AtlasOverlayAudioInput,
  AtlasOverlayCatalog,
  AtlasOverlayConfig,
  AtlasOverlayEvent,
  AtlasOverlayPttPhase,
  AtlasOverlaySpeechResult,
  AtlasOverlaySubmitResult,
  AtlasOverlayVoiceCatalog,
} from "../shared/atlas-overlay";

const MAX_CAPTURE_BYTES = 12 * 1024 * 1024;
const AUDIO_MIME = /^(?:audio\/(?:webm|ogg|mp4|mpeg|wav|x-wav)|video\/webm)(?:;.*)?$/i;

function safeAudioInput(value: AtlasOverlayAudioInput): AtlasOverlayAudioInput {
  if (!(value?.audio instanceof ArrayBuffer)) throw new Error("atlas_overlay_audio_invalid");
  if (value.audio.byteLength < 64 || value.audio.byteLength > MAX_CAPTURE_BYTES) {
    throw new Error("atlas_overlay_audio_size_invalid");
  }
  const mimeType = String(value.mimeType || "").trim().slice(0, 96);
  if (!AUDIO_MIME.test(mimeType)) throw new Error("atlas_overlay_audio_type_invalid");
  const durationMs = Math.max(100, Math.min(60_000, Number(value.durationMs) || 0));
  return { audio: value.audio, mimeType, durationMs };
}

const api: AtlasOverlayApi = {
  getConfig: () =>
    ipcRenderer.invoke("atlas-overlay:get-config") as Promise<AtlasOverlayConfig>,
  getCatalog: () =>
    ipcRenderer.invoke("atlas-overlay:get-catalog") as Promise<AtlasOverlayCatalog>,
  saveConfig: (patch) =>
    ipcRenderer.invoke("atlas-overlay:save-config", patch) as Promise<AtlasOverlayConfig>,
  moveBy: (deltaX, deltaY) =>
    ipcRenderer.invoke(
      "atlas-overlay:move-by",
      Number.isFinite(deltaX) ? Math.max(-360, Math.min(360, deltaX)) : 0,
      Number.isFinite(deltaY) ? Math.max(-260, Math.min(260, deltaY)) : 0,
    ) as Promise<AtlasOverlayConfig>,
  getVoices: () => ipcRenderer.invoke("atlas-overlay:get-voices") as Promise<AtlasOverlayVoiceCatalog>,
  previewVoice: (voice) =>
    ipcRenderer.invoke("atlas-overlay:preview-voice", String(voice || "").slice(0, 80)) as Promise<AtlasOverlaySpeechResult>,
  submitAudio: (input) =>
    ipcRenderer.invoke(
      "atlas-overlay:submit-audio",
      safeAudioInput(input),
    ) as Promise<AtlasOverlaySubmitResult>,
  submitText: (question) =>
    ipcRenderer.invoke(
      "atlas-overlay:submit-text",
      String(question || "").trim().slice(0, 4_000),
    ) as Promise<AtlasOverlaySubmitResult>,
  cancel: () => ipcRenderer.invoke("atlas-overlay:cancel"),
  hide: () => ipcRenderer.invoke("atlas-overlay:hide"),
  openAtlas: () => ipcRenderer.invoke("atlas-overlay:open-atlas"),
  onEvent: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: AtlasOverlayEvent) =>
      listener(payload);
    ipcRenderer.on("atlas-overlay:event", handler);
    return () => ipcRenderer.removeListener("atlas-overlay:event", handler);
  },
  onPtt: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, phase: AtlasOverlayPttPhase) =>
      listener(phase);
    ipcRenderer.on("atlas-overlay:ptt", handler);
    return () => ipcRenderer.removeListener("atlas-overlay:ptt", handler);
  },
};

contextBridge.exposeInMainWorld("tmodAtlasOverlay", api);
