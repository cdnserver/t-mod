import { accountIdentityKey, sameAccountIdentity, type AccountIdentity } from "./account-identity";

/** Verify the owner of a CSRF snapshot, not only the local session revision.
 * A service can replace the shared cookie while an earlier screen is open.
 */
export function accountSnapshotMatches(snapshot: Record<string, unknown>, expected: AccountIdentity): boolean {
  const viewer = snapshot.viewer;
  if (viewer && typeof viewer === "object" && !Array.isArray(viewer)) {
    const value = viewer as Record<string, unknown>;
    if (typeof value.id === "string") return /^\d+$/.test(value.id) && value.id === accountIdentityKey(expected);
    if (typeof value.id === "number") return sameAccountIdentity(expected, { id: value.id, id_exact: typeof value.id_exact === "string" ? value.id_exact : undefined });
    return false;
  }
  const links = snapshot.links;
  if (links && typeof links === "object" && !Array.isArray(links)) {
    const id = (links as Record<string, unknown>).discord;
    return typeof id === "string" && /^\d+$/.test(id) && id === accountIdentityKey(expected);
  }
  const account = snapshot.account;
  if (account && typeof account === "object" && !Array.isArray(account)) {
    const id = (account as Record<string, unknown>).user_id;
    if (typeof id === "string") return /^\d+$/.test(id) && id === accountIdentityKey(expected);
    if (typeof id === "number") return Number.isInteger(id) && id === expected.id;
  }
  return false;
}

export function csrfTokenFromAccountSnapshot(snapshot: Record<string, unknown>): string | undefined {
  if (typeof snapshot.csrf_token === "string" && snapshot.csrf_token) return snapshot.csrf_token;
  const viewer = snapshot.viewer;
  if (viewer && typeof viewer === "object" && !Array.isArray(viewer)) {
    const token = (viewer as Record<string, unknown>).csrf_token;
    if (typeof token === "string" && token) return token;
  }
  return undefined;
}
