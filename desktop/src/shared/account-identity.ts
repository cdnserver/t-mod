export interface AccountIdentity { id: number; id_exact?: string }

export function accountIdentityKey(viewer: AccountIdentity): string {
  return typeof viewer.id_exact === "string" && /^\d+$/.test(viewer.id_exact)
    ? viewer.id_exact : String(viewer.id);
}

export function sameAccountIdentity(expected: AccountIdentity, actual: AccountIdentity | undefined): boolean {
  if (!actual) return false;
  // Discord snowflakes are not exactly representable as JavaScript numbers.
  // Older servers expose only the numeric v1 field, so retain that fallback.
  if (expected.id_exact && actual.id_exact) return expected.id_exact === actual.id_exact;
  return expected.id === actual.id;
}
