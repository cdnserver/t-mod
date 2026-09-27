import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = (name: string) => readFileSync(new URL(`../src/renderer/${name}`, import.meta.url), "utf8");
describe("Blackbird explicit motion policy", () => {
  it("isolates the system CSS override from Blackbird only", () => {
    expect(source("main.tsx")).toContain('if (desktopProduct.privateEdition) document.documentElement.dataset.motionPolicy = "app"');
    expect(source("styles.css")).toContain(':root:not([data-motion-policy="app"]) *');
    expect(source("styles.css")).toContain('.desktop.reduce-motion *');
  });
  it("keeps Blackbird scenes controlled by the client preference", () => {
    for (const name of ["blackbird-setup.css", "blackbird-workspace.css", "blackbird-hub-recut.css", "blackbird-prelude.css", "blackbird-idle.css"]) {
      expect(source(name)).not.toContain("prefers-reduced-motion");
      expect(source(name)).toContain(".reduce-motion");
    }
    expect(source("LunarTexture.tsx")).toContain('!reduced && (document.documentElement.dataset.motionPolicy === "app" || !reducedMotion.matches)');
  });
});
