import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "..");
const workspaceRoot = resolve(root, "..");

describe("desktop release contract", () => {
  it("never ships demonstration identities", () => {
    const renderer = readFileSync(resolve(root, "src/renderer/App.tsx"), "utf8");
    expect(renderer).not.toContain("mockBootstrap");
    expect(renderer).not.toContain("S. Goodman | 263345 | Иван");
    expect(renderer).not.toContain("721577061143019555");
  });

  it("uses the production Electron bridge and native account flow", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const preload = readFileSync(resolve(root, "src/preload/index.ts"), "utf8");
    expect(main).toContain("new BrowserWindow");
    expect(main).not.toContain("new BaseWindow");
    expect(main).toContain("/auth/login?client=desktop");
    expect(preload).toContain('contextBridge.exposeInMainWorld("tmodDesktop"');
    expect(preload).toContain('ipcRenderer.invoke("desktop:login"');
  });

  it("publishes installers and updater metadata from the public release channel", () => {
    const manifest = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
    expect(manifest.version).toBe("0.3.2");
    expect(manifest.build.publish).toEqual([
      expect.objectContaining({
        provider: "github",
        owner: "cdnserver",
        repo: "t-mod-releases",
      }),
    ]);
    expect(manifest.build.win.target).toBe("nsis");
    expect(manifest.build.mac.target).toContain("zip");
  });

  it("keeps shell controls above every remote service contour", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const preload = readFileSync(resolve(root, "src/preload/index.ts"), "utf8");
    expect(main).toContain('ipcMain.handle("desktop:shell-overlay"');
    expect(main).toContain('mainWindow?.webContents.send("desktop:command-palette")');
    expect(main).toContain("syncServiceVisibility");
    expect(preload).toContain('ipcRenderer.invoke("desktop:shell-overlay"');
    expect(preload).toContain('ipcRenderer.on("desktop:command-palette"');
  });

  it("keeps beta sessions resilient and exposes shell preferences", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const preload = readFileSync(resolve(root, "src/preload/index.ts"), "utf8");
    expect(main).toContain("SERVICE_RETRY_DELAYS");
    expect(main).toContain("TModDesktop/${app.getVersion()}");
    expect(main).toContain('ipcMain.handle("desktop:preferences"');
    expect(preload).toContain('ipcRenderer.invoke("desktop:preferences"');
    expect(preload).toContain('ipcRenderer.invoke("desktop:copy-current-link"');
  });

  it("shows the branded launch sequence and allows its local audio signature", () => {
    const main = readFileSync(resolve(root, "src/main/index.ts"), "utf8");
    const renderer = readFileSync(resolve(root, "src/renderer/App.tsx"), "utf8");
    const styles = readFileSync(resolve(root, "src/renderer/styles.css"), "utf8");
    expect(main).toContain('appendSwitch("autoplay-policy", "no-user-gesture-required")');
    expect(renderer).toContain("function LaunchSequence");
    expect(renderer).toContain("function playLaunchSound");
    expect(renderer).toContain("launchVisible && <LaunchSequence");
    expect(styles).toContain(".launch-sequence");
    expect(styles).toContain("@keyframes launch-sequence-out");
    expect(renderer).not.toContain("launch-orbit");
    expect(styles).not.toContain(".launch-orbit");
  });

  it("ships one complete vector identity for every desktop contour", () => {
    const services = [
      "home",
      "reactor",
      "consensus",
      "atlas",
      "sgl",
      "ovr",
      "games",
      "tasks",
      "admin",
    ];
    const sizes = [24, 32, 64, 128, 256];

    for (const service of services) {
      expect(existsSync(resolve(workspaceRoot, `brand/services/${service}.svg`))).toBe(true);
      expect(existsSync(resolve(workspaceRoot, `brand/services/mono/${service}.svg`))).toBe(true);
      for (const size of sizes) {
        expect(existsSync(resolve(workspaceRoot, `brand/exports/color/${size}/${service}.png`))).toBe(true);
        expect(existsSync(resolve(workspaceRoot, `brand/exports/mono/${size}/${service}.png`))).toBe(true);
      }
    }

    for (const variant of ["color", "mono-light", "mono-dark"]) {
      for (const size of sizes) {
        expect(existsSync(resolve(workspaceRoot, `brand/exports/tmod-${variant}/${size}/tmod.png`))).toBe(true);
      }
    }

    const masterMark = readFileSync(resolve(workspaceRoot, "brand/tmod/mark.svg"), "utf8");
    const desktopMark = readFileSync(resolve(root, "resources/icon.svg"), "utf8");
    expect(desktopMark).toBe(masterMark);
  });
});
