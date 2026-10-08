export interface ProtectedAccessState {
  revision: number;
  accountId: string | null;
  locked: boolean;
  banned: boolean;
}

export interface ProtectedAccessLease {
  readonly signal: AbortSignal;
  readonly accountId: string;
  assertCurrent(): void;
}

export interface UnlockContext { lockRevision: number; sessionRevision: number; accountId: string | null }
export function mayCompleteUnlock(expected: UnlockContext, current: UnlockContext, banned: boolean): boolean {
  return !banned && Boolean(expected.accountId) && expected.accountId === current.accountId &&
    expected.lockRevision === current.lockRevision && expected.sessionRevision === current.sessionRevision;
}

/** A local pause is not a logout, but still permanently revokes earlier work.
 * A request cannot become valid again merely because the same account unlocks.
 * This complements, rather than replaces, server-side authorization.
 */
export class ProtectedAccessGate {
  private generation = 0;
  private cancellation = new AbortController();
  constructor(private readonly state: () => ProtectedAccessState) {}

  revoke(): void {
    this.generation++;
    this.cancellation.abort(new Error("access_revoked"));
    this.cancellation = new AbortController();
  }

  capture(): ProtectedAccessLease {
    const initial = { ...this.state() };
    if (!initial.accountId || initial.locked || initial.banned) throw new Error("access_locked");
    const generation = this.generation;
    return {
      signal: this.cancellation.signal,
      accountId: initial.accountId,
      assertCurrent: () => {
        const current = this.state();
        if (generation !== this.generation || current.revision !== initial.revision ||
            current.accountId !== initial.accountId || current.locked || current.banned)
          throw new Error("session_changed");
      },
    };
  }
}
