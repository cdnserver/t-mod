import type { DesktopShellPreferences } from "./contracts";

export function notificationChannel(delivery: DesktopShellPreferences["notificationDelivery"], focused: boolean) {
  if (delivery === "off") return "none";
  if (delivery === "in-app") return "custom";
  if (delivery === "system") return "system";
  // Both means adaptive delivery, not two banners and two sounds for one event.
  return focused ? "custom" : "system";
}
