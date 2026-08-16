import { contextBridge, ipcRenderer } from "electron";
import type {
  BootstrapResult,
  DesktopState,
  ServiceId,
  TModDesktopApi,
} from "../shared/contracts";

const api: TModDesktopApi = {
  bootstrap: () => ipcRenderer.invoke("desktop:bootstrap") as Promise<BootstrapResult>,
  navigate: (serviceId: ServiceId) =>
    ipcRenderer.invoke("desktop:navigate", serviceId) as Promise<DesktopState>,
  reload: () => ipcRenderer.invoke("desktop:reload"),
  goBack: () => ipcRenderer.invoke("desktop:back"),
  goForward: () => ipcRenderer.invoke("desktop:forward"),
  openLogin: () => ipcRenderer.invoke("desktop:open-login") as Promise<DesktopState>,
  minimize: () => ipcRenderer.invoke("desktop:minimize"),
  toggleMaximize: () => ipcRenderer.invoke("desktop:maximize"),
  close: () => ipcRenderer.invoke("desktop:close"),
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
};

contextBridge.exposeInMainWorld("tmodDesktop", api);
