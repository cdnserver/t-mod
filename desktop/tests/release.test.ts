import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const root = resolve(import.meta.dirname, "..");

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
    expect(manifest.version).toBe("0.2.0");
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
});
