import { describe, expect, it } from "vitest";
import { notificationChannel } from "../src/shared/notification-policy";
describe("Blackbird notification delivery", () => {
  it("uses exactly one channel in adaptive mode", () => {
    expect(notificationChannel("both", true)).toBe("custom");
    expect(notificationChannel("both", false)).toBe("system");
  });
  it("respects explicit custom, system and silent preferences", () => {
    for (const focus of [true, false]) {
      expect(notificationChannel("in-app", focus)).toBe("custom");
      expect(notificationChannel("system", focus)).toBe("system");
      expect(notificationChannel("off", focus)).toBe("none");
    }
  });
});
