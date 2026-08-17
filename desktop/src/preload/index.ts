import { contextBridge, ipcRenderer } from "electron";
import type {
  BootstrapResult,
  DesktopLoginCredentials,
  DesktopLoginResult,
  DesktopState,
  DesktopShellPreferences,
  DesktopUpdateState,
  ServiceId,
  TModDesktopApi,
} from "../shared/contracts";

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
  onUpdate: (listener) => {
    const handler = (_event: Electron.IpcRendererEvent, state: DesktopUpdateState) =>
      listener(state);
    ipcRenderer.on("desktop:update", handler);
    return () => ipcRenderer.removeListener("desktop:update", handler);
  },
};

contextBridge.exposeInMainWorld("tmodDesktop", api);
