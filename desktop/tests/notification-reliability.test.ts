import { describe, expect, it } from "vitest";
import type { DesktopNotification } from "../src/shared/contracts";
import { readableNotificationIds, startupPopupNotifications } from "../src/shared/notification-reliability";

const now = Date.parse("2026-10-01T12:00:00Z");
const item = (id: number, kind: string, created_at = "2026-10-01T11:59:00Z", read_at: string | null = null): DesktopNotification =>
  ({ id, kind, created_at, read_at, title: "Сообщение", body: "Текст", route: null, severity: "info" });

describe("Blackbird notification delivery", () => {
  it("shows recent unread chat messages after startup without replaying an old inbox", () => {
    expect(startupPopupNotifications([
      item(4, "communicate"), item(3, "communicate", "2026-10-01T11:00:00Z"),
      item(2, "communicate", undefined, "2026-10-01T11:59:30Z"), item(1, "general"),
    ], now).map(value => value.id)).toEqual([4]);
  });
  it("keeps popup order chronological and caps startup replay", () => {
    expect(startupPopupNotifications([5, 4, 3, 2, 1].map(id => item(id, "communicate")), now)
      .map(value => value.id)).toEqual([3, 4, 5]);
  });
  it("accepts freshly polled IDs while rejecting unknown and invalid IDs", () => {
    expect(readableNotificationIds([9, 9, 8, 0, "9", 1.5], new Set([9]))).toEqual([9]);
  });
});
