import { contextBridge, ipcRenderer } from "electron";
import type {
  BootstrapResult,
  DesktopLoginCredentials,
  DesktopLoginResult,
  DesktopLockReason,
  DesktopState,
  DesktopShellPreferences,
  DesktopUpdateState,
  ServiceId,
  TModDesktopApi,
} from "../shared/contracts";
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

const api: TModDesktopApi = {
  bootstrap: () => ipcRenderer.invoke("desktop:bootstrap") as Promise<BootstrapResult>,
  login: (credentials: DesktopLoginCredentials) =>
    ipcRenderer.invoke("desktop:login", credentials) as Promise<DesktopLoginResult>,
  logout: () => ipcRenderer.invoke("desktop:logout") as Promise<boolean>,
  navigate: (serviceId: ServiceId) =>
    ipcRenderer.invoke("desktop:navigate", serviceId) as Promise<DesktopState>,
  reload: () => ipcRenderer.invoke("desktop:reload"),
  goBack: () => ipcRenderer.invoke("desktop:back"),
  goForward: () => ipcRenderer.invoke("desktop:forward"),
  setShellOverlayOpen: (open: boolean) =>
    ipcRenderer.invoke("desktop:shell-overlay", open),
  applyPreferences: (preferences: DesktopShellPreferences) =>
    ipcRenderer.invoke("desktop:preferences", preferences) as Promise<DesktopShellPreferences>,
  copyCurrentLink: () => ipcRenderer.invoke("desktop:copy-current-link") as Promise<boolean>,
  openCurrentLink: () => ipcRenderer.invoke("desktop:open-current-link") as Promise<boolean>,
  openLogin: () => ipcRenderer.invoke("desktop:open-login") as Promise<DesktopState>,
  lock: () => ipcRenderer.invoke("desktop:lock") as Promise<boolean>,
  unlock: () => ipcRenderer.invoke("desktop:unlock") as Promise<boolean>,
  minimize: () => ipcRenderer.invoke("desktop:minimize"),
  toggleMaximize: () => ipcRenderer.invoke("desktop:maximize"),
  close: () => ipcRenderer.invoke("desktop:close"),
  checkForUpdates: () =>
    ipcRenderer.invoke("desktop:update-check") as Promise<DesktopUpdateState>,
  installUpdate: () => ipcRenderer.invoke("desktop:update-install") as Promise<boolean>,
  openReleasePage: () =>
    ipcRenderer.invoke("desktop:update-open-release") as Promise<boolean>,
  onState: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, state: DesktopState) => listener(state);
    ipcRenderer.on("desktop:state", handler);
    return () => ipcRenderer.removeListener("desktop:state", handler);
  },
  onAuthChanged: (listener) => {
    const handler = () => listener();
    ipcRenderer.on("desktop:auth-changed", handler);
    return () => ipcRenderer.removeListener("desktop:auth-changed", handler);
  },
  onCommandPalette: (listener) => {
    const handler = () => listener();
    ipcRenderer.on("desktop:command-palette", handler);
    return () => ipcRenderer.removeListener("desktop:command-palette", handler);
  },
  onAtlasOverlaySettings: (listener) => {
    const handler = () => listener();
    ipcRenderer.on("desktop:open-atlas-overlay-settings", handler);
    return () => ipcRenderer.removeListener("desktop:open-atlas-overlay-settings", handler);
  },
  onLockRequested: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, reason: DesktopLockReason) =>
      listener(reason);
    ipcRenderer.on("desktop:lock-requested", handler);
    return () => ipcRenderer.removeListener("desktop:lock-requested", handler);
  },
  onUpdate: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, state: DesktopUpdateState) =>
      listener(state);
    ipcRenderer.on("desktop:update", handler);
    return () => ipcRenderer.removeListener("desktop:update", handler);
  },
};

contextBridge.exposeInMainWorld("tmodDesktop", api);

const overlayApi: AtlasOverlayApi = {
  getConfig: () => ipcRenderer.invoke("atlas-overlay:get-config") as Promise<AtlasOverlayConfig>,
  getCatalog: () => ipcRenderer.invoke("atlas-overlay:get-catalog") as Promise<AtlasOverlayCatalog>,
  saveConfig: (patch) =>
    ipcRenderer.invoke("atlas-overlay:save-config", patch) as Promise<AtlasOverlayConfig>,
  getVoices: () => ipcRenderer.invoke("atlas-overlay:get-voices") as Promise<AtlasOverlayVoiceCatalog>,
  previewVoice: (voice) =>
    ipcRenderer.invoke("atlas-overlay:preview-voice", voice) as Promise<AtlasOverlaySpeechResult>,
  submitAudio: (input: AtlasOverlayAudioInput) =>
    ipcRenderer.invoke("atlas-overlay:submit-audio", input) as Promise<AtlasOverlaySubmitResult>,
  submitText: (question) =>
    ipcRenderer.invoke("atlas-overlay:submit-text", String(question || "").slice(0, 4_000)) as Promise<AtlasOverlaySubmitResult>,
  cancel: () => ipcRenderer.invoke("atlas-overlay:cancel"),
  hide: () => ipcRenderer.invoke("atlas-overlay:hide"),
  openAtlas: () => ipcRenderer.invoke("atlas-overlay:open-atlas"),
  onEvent: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: AtlasOverlayEvent) => listener(payload);
    ipcRenderer.on("atlas-overlay:event", handler);
    return () => ipcRenderer.removeListener("atlas-overlay:event", handler);
  },
  onPtt: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, phase: AtlasOverlayPttPhase) => listener(phase);
    ipcRenderer.on("atlas-overlay:ptt", handler);
    return () => ipcRenderer.removeListener("atlas-overlay:ptt", handler);
  },
};

contextBridge.exposeInMainWorld("tmodAtlasOverlay", overlayApi);
