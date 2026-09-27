import { contextBridge, ipcRenderer } from "electron";
contextBridge.exposeInMainWorld("blackbirdNotification", {
  onItem: (listener: (value: unknown) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, value: unknown) => listener(value);
    ipcRenderer.on("blackbird:notification", handler);
    ipcRenderer.send("blackbird:notification-ready");
    return () => ipcRenderer.removeListener("blackbird:notification", handler);
  },
  action: (action: "open" | "dismiss") => ipcRenderer.send("blackbird:notification-action", action),
});
