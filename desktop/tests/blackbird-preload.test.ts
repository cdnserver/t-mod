import { describe, expect, it } from "vitest";
import { preloadSummary } from "../src/renderer/blackbird-preload";

describe("Blackbird startup preparation", () => {
  it("does not report completion while an actual task is still pending", () => {
    const result = preloadSummary([{ label: "Fonts", status: "ready" }, { label: "Moon", status: "pending" }]);
    expect(result.ready).toBe(false);
    expect(result.progress).toBe(.5);
    expect(result.label).toBe("Moon");
  });
  it("allows a settled fallback without claiming full preparation succeeded", () => {
    const result = preloadSummary([{ label: "Moon", status: "fallback" }, { label: "Connection", status: "ready" }]);
    expect(result.ready).toBe(true);
    expect(result.degraded).toBe(true);
    expect(result.label).toContain("резервный");
  });
  it("finishes only when every task has settled", () => {
    expect(preloadSummary([{ label: "Fonts", status: "ready" }, { label: "Moon", status: "ready" }])).toMatchObject({ ready: true, progress: 1, degraded: false });
    expect(preloadSummary([]).ready).toBe(true);
  });
});
