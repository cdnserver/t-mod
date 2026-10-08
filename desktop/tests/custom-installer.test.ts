import { describe, expect, it } from "vitest";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
describe("Custom Blackbird installer release gates", () => {
  it.skipIf(process.platform === "win32")("refuses packaging on non-Windows before invoking any build tool", () => {
    const result = spawnSync(process.execPath, ["scripts/build-blackbird.mjs", "custom"], { cwd: root, encoding: "utf8" });
    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain("native Windows only");
    expect(result.stdout).toBe("");
  });
  it("keeps the experimental native installer separate from the working feed", () => {
    const script = readFileSync(new URL("../scripts/build-blackbird.mjs", import.meta.url), "utf8");
    const project = readFileSync(new URL("../installer/Blackbird.Setup/Blackbird.Setup.csproj", import.meta.url), "utf8");
    const builder = readFileSync(new URL("../electron-builder.blackbird.yml", import.meta.url), "utf8");
    expect(script).toContain('"--publish", "never"');
    expect(script).toContain('"custom-latest.yml"');
    expect(script).toContain("installer/Blackbird.Setup.Pack");
    expect(project).toContain('Name="RequirePayloadForPublish"');
    expect(project).toContain('<UseWPF>true</UseWPF>');
    expect(builder).toContain("target: nsis");
  });
});
