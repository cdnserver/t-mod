import { describe, expect, it } from "vitest";
import { notificationHardLimitMs, notificationVisibilityMs } from "../src/shared/notification-timing";

describe("operator notification expiry", () => {
  it("closes fullscreen messages after eleven seconds even without a renderer timer", () => {
    expect(notificationVisibilityMs("orl:fullscreen")).toBe(11_000);
    expect(notificationHardLimitMs("orl:fullscreen")).toBe(11_000);
  });
  it("bounds other non-interactive overlays and supports a configured toast duration", () => {
    expect(notificationHardLimitMs("orl:overlay")).toBe(9_000);
    expect(notificationHardLimitMs("screenban")).toBe(14_000);
    expect(notificationVisibilityMs("orl:toast")).toBe(9_000);
    expect(notificationHardLimitMs("orl:toast")).toBe(9_000);
    expect(notificationHardLimitMs("communicate", 20_000)).toBe(20_000);
  });
});
