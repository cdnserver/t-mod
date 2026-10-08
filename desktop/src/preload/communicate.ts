import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("blackbirdCommunicate", {
  context: () => ipcRenderer.invoke("blackbird:communicate-context"),
  request: (action: "communicate" | "communicate-update", data?: Record<string, string>) =>
    ipcRenderer.invoke("blackbird:communicate-request", action, data),
  upload: (input: { partnerId: string; filename: string; caption: string; bytes: Uint8Array; clientNonce?: string }) =>
    ipcRenderer.invoke("blackbird:communicate-upload", input),
  attachment: (id: string) => ipcRenderer.invoke("blackbird:communicate-attachment", id),
  linkPreview: (url: string) => ipcRenderer.invoke("blackbird:communicate-link-preview", url),
  close: () => ipcRenderer.invoke("blackbird:communicate-close"),
  minimize: () => ipcRenderer.invoke("blackbird:communicate-minimize"),
  openLink: (url: string) => ipcRenderer.invoke("blackbird:communicate-open-link", url),
  copyLink: (url: string) => ipcRenderer.invoke("blackbird:communicate-copy-link", url),
  onShare: (listener: (url: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, url: string) => listener(url);
    ipcRenderer.on("blackbird:share-link", handler);
    return () => ipcRenderer.removeListener("blackbird:share-link", handler);
  },
});
