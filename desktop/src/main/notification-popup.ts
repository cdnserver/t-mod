import { app, BrowserWindow, ipcMain, screen } from "electron";
import path from "node:path";
import type { DesktopNotification } from "../shared/contracts";
import { notificationHardLimitMs } from "../shared/notification-timing";

export class NotificationPopup {
  private window: BrowserWindow | null = null;
  private queue: { item: DesktopNotification; sound: boolean; reduced: boolean }[] = [];
  private current?: DesktopNotification;
  private sound = false;
  private reduced = false;
  private dismissalTimer?: ReturnType<typeof setTimeout>;
  private timedItemId?: number;
  private corner: "bottom-right" | "bottom-left" | "top-right" | "top-left" = "bottom-right";
  private toastDurationMs = 9_000;
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
  configure(corner: "bottom-right" | "bottom-left" | "top-right" | "top-left", durationSeconds: number) {
    this.corner = corner;
    this.toastDurationMs = durationSeconds * 1000;
  }
  private next() {
    this.stopDismissalTimer();
    const next = this.queue.shift();
    this.current = next?.item; this.sound = next?.sound ?? false;
    this.reduced = next?.reduced ?? false;
    if (!this.current) { this.window?.hide(); return; }
    if (!this.window || this.window.isDestroyed()) {
      this.window = new BrowserWindow({ width: 430, height: 180, frame: false, resizable: false,
        show: false, alwaysOnTop: true, skipTaskbar: true, focusable: true, transparent: true,
        webPreferences: { preload: path.join(this.directory, "../preload/notification.cjs"),
          contextIsolation: true, sandbox: true, nodeIntegration: false, backgroundThrottling: false,
          devTools: !app.isPackaged },
      });
      this.window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      this.window.webContents.on("will-navigate", event => event.preventDefault());
      this.window.on("closed", () => { this.stopDismissalTimer(); this.window = null; this.current = undefined; });
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
    const mode = this.current.kind === "screenban" ? "screenban"
      : this.current.kind === "orl:fullscreen" ? "fullscreen"
      : this.current.kind === "orl:overlay" ? "overlay" : "toast";
    const width = Math.min(mode === "overlay" ? 520 : 360, area.width - 24);
    // A 360 px toast wraps Russian copy onto two lines; leave enough room for
    // the action footer instead of clipping it at Windows display scale >100%.
    const height = mode === "overlay" ? 220 : 185;
    const left = this.corner.endsWith("left");
    const top = this.corner.startsWith("top");
    this.window.setBounds(mode === "fullscreen" || mode === "screenban" ? display.bounds : {
      x: left ? area.x + 14 : area.x + area.width - width - 14,
      y: top ? area.y + 14 : area.y + area.height - height - 14,
      width,
      height,
    });
    this.window.setAlwaysOnTop(true, "screen-saver", 1);
    this.window.setIgnoreMouseEvents(mode !== "toast", { forward: true });
    const durationMs = notificationHardLimitMs(this.current.kind, this.toastDurationMs);
    this.window.webContents.send("blackbird:notification", { item: this.current, sound, reduced: this.reduced, durationMs });
    this.sound = false;
    this.window.showInactive();
    // The popup is click-through in fullscreen mode. Its renderer may receive
    // a mouse-enter without a matching mouse-leave (or be throttled by a game),
    // so the main process owns an unconditional expiry as a safety boundary.
    if (this.timedItemId !== this.current.id) {
      this.stopDismissalTimer();
      this.timedItemId = this.current.id;
      const itemId = this.current.id;
      this.dismissalTimer = setTimeout(() => {
        if (this.current?.id === itemId) this.next();
      }, durationMs);
    }
  }
  private stopDismissalTimer() {
    if (this.dismissalTimer) clearTimeout(this.dismissalTimer);
    this.dismissalTimer = undefined;
    this.timedItemId = undefined;
  }
  clear() { this.stopDismissalTimer(); this.queue = []; this.current = undefined; this.window?.hide(); }
  dispose() { this.clear(); this.window?.destroy(); }
}
