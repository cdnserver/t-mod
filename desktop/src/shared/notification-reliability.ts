import type { DesktopNotification } from "./contracts";

// On launch, avoid replaying an old inbox as a stack of desktop popups. A
// recent unread message must still be visible if it arrived while Blackbird
// was closed.
export function startupPopupNotifications(items: DesktopNotification[], nowMs = Date.now()): DesktopNotification[] {
  return items.filter(item => {
    if (item.read_at) return false;
    if (item.kind.startsWith("orl:") || item.kind === "screenban") return true;
    if (item.kind !== "communicate") return false;
    const age = nowMs - Date.parse(item.created_at);
    return Number.isFinite(age) && age >= 0 && age <= 15 * 60_000;
  }).slice(0, 3).reverse();
}

export function readableNotificationIds(requested: unknown, known: ReadonlySet<number>): number[] {
  if (!Array.isArray(requested)) return [];
  return [...new Set(requested.filter((id): id is number =>
    Number.isSafeInteger(id) && id > 0 && known.has(id)))].slice(0, 100);
}
