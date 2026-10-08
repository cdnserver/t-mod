// SMBIOS UUIDs are stable across OS reinstalls, but many virtual machines and
// OEMs expose a shared placeholder. Never bind a ban to those values.
export function normalizeHardwareUuid(value: string): string | null {
  const uuid = String(value || "").trim().toLowerCase();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(uuid)) return null;
  const hex = uuid.replaceAll("-", "");
  if (new Set(hex).size < 8) return null;
  return uuid;
}
