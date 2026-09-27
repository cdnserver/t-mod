import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { BlackbirdIdle } from "../src/renderer/BlackbirdIdle";

describe("Blackbird observatory", () => {
  it("has its own scene, stable time and minimize action without old vault copy", () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-09-27T22:30:00"));
    const html = renderToStaticMarkup(createElement(BlackbirdIdle, { name:"Роберт", reduced:false, unlocking:false, onMinimize:()=>{} }));
    expect(html).toContain("Добрый вечер"); expect(html).toContain("Роберт"); expect(html).toContain("Свернуть приложение");
    expect(html).toContain("22:30"); expect(html).not.toContain("Вручную"); expect(html).not.toContain("vault-screen");
    vi.useRealTimers();
  });
  it("native timer locks independently of focus and shell rendering remains unthrottled", () => {
    const main = readFileSync(new URL("../src/main/index.ts", import.meta.url), "utf8");
    const monitor = main.slice(main.indexOf("function startIdleLockMonitor"), main.indexOf("function clearServiceRetry"));
    expect(monitor).toContain("powerMonitor.getSystemIdleTime()"); expect(monitor).not.toContain("isFocused");
    expect(main).toContain("backgroundThrottling: false"); expect(main).toContain('mainWindow.on("restore", emitState)');
    expect(main).toContain("locked: desktopLocked");
  });
});
