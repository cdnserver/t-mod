import { BrowserWindow, ipcMain, screen } from "electron";
import path from "node:path";
import type { DesktopNotification } from "../shared/contracts";

export class NotificationPopup {
  private window: BrowserWindow | null = null;
  private queue: { item: DesktopNotification; sound: boolean; reduced: boolean }[] = [];
  private current?: DesktopNotification;
  private sound = false;
  private reduced = false;
  constructor(private directory: string, private onOpen: (item: DesktopNotification) => void, private bounds: () => Electron.Rectangle | undefined) {
    ipcMain.on("blackbird:notification-action", (event, action: unknown) => {
      if (event.sender.id !== this.window?.webContents.id) return;
      if (action === "open" && this.current) this.onOpen(this.current);
      if (action === "open" || action === "dismiss") this.next();
    });
    ipcMain.on("blackbird:notification-ready", event => {
      if (event.sender.id === this.window?.webContents.id) this.present(this.sound);
    });
  }
  show(item: DesktopNotification, sound: boolean, reduced = false) {
    if (this.current?.id === item.id || this.queue.some(entry => entry.item.id === item.id)) return;
    this.queue.push({ item, sound, reduced });
    this.queue = this.queue.slice(-8);
    if (!this.current) this.next();
  }
  private next() {
    const next = this.queue.shift();
    this.current = next?.item; this.sound = next?.sound ?? false;
    this.reduced = next?.reduced ?? false;
    if (!this.current) { this.window?.hide(); return; }
    if (!this.window || this.window.isDestroyed()) {
      this.window = new BrowserWindow({ width: 430, height: 180, frame: false, resizable: false,
        show: false, alwaysOnTop: true, skipTaskbar: true, focusable: false, transparent: true,
        webPreferences: { preload: path.join(this.directory, "../preload/notification.cjs"),
          contextIsolation: true, sandbox: true, nodeIntegration: false, backgroundThrottling: false },
      });
      this.window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      this.window.webContents.on("will-navigate", event => event.preventDefault());
      this.window.on("closed", () => { this.window = null; this.current = undefined; });
      const dev = process.env.ELECTRON_RENDERER_URL;
      const loading = dev ? this.window.loadURL(new URL("notification.html", dev).href)
        : this.window.loadFile(path.join(this.directory, "../renderer/notification.html"));
      void loading.catch(() => this.clear());
    } else this.present(this.sound);
  }
  private present(sound: boolean) {
    if (!this.window || !this.current) return;
    const bounds = this.bounds();
    const display = bounds ? screen.getDisplayMatching(bounds) : screen.getPrimaryDisplay();
    const area = display.workArea;
    const width = Math.min(430, area.width - 24);
    this.window.setBounds({ x: area.x + area.width - width - 12, y: area.y + area.height - 192, width, height: 180 });
    this.window.webContents.send("blackbird:notification", { item: this.current, sound, reduced: this.reduced });
    this.sound = false;
    this.window.showInactive();
  }
  clear() { this.queue = []; this.current = undefined; this.window?.hide(); }
  dispose() { this.clear(); this.window?.destroy(); }
}
