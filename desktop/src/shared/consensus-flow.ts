import type { ConsensusLiveSnapshot, DesktopState } from "./contracts";
import type { AccountIdentity } from "./account-identity";

export type ConsensusHallPhase = "syncing" | "waiting" | "ready" | "finished" | "changed";
const ballotStages = new Set(["presentation", "voting", "finalizing", "discussion_type", "discussion", "paused", "after_result"]);

export function consensusHallPhase(snapshot: ConsensusLiveSnapshot | null, sessionKey: string, connected = true): ConsensusHallPhase {
  if (!snapshot || !connected) return "syncing";
  if (!snapshot.sessionKey || snapshot.stage === "finished" || snapshot.stage === "cancelled") return "finished";
  if (snapshot.sessionKey !== sessionKey) return "changed";
  if (snapshot.confirmed && snapshot.ballotAvailable && ballotStages.has(snapshot.stage)) return "ready";
  return "waiting";
}

export function consensusHallMayEnter(snapshot: ConsensusLiveSnapshot | null, sessionKey: string, connected: boolean, serviceReady: boolean): boolean {
  return connected && serviceReady && consensusHallPhase(snapshot, sessionKey) === "ready";
}

export function consensusBallotLoaded(state: DesktopState): boolean {
  if (state.activeService !== "consensus" || state.loading || state.error || state.locked || state.serviceReady !== true) return false;
  try {
    const url = new URL(state.url || "");
    return url.protocol === "https:" && url.host === "consensus.tvr.lat" && url.pathname === "/" && url.searchParams.get("view") === "ballot";
  } catch { return false; }
}

export function consensusViewerMatches(viewer: { id?: number; id_exact?: string } | undefined, account: AccountIdentity | undefined): boolean {
  if (!viewer || !account) return false;
  if (typeof viewer.id_exact === "string" && viewer.id_exact) {
    return viewer.id_exact === (account.id_exact || String(account.id));
  }
  // Only small, exactly representable IDs may use the legacy number field.
  // Rounded Discord snowflakes cannot prove ownership of a personal ballot.
  return Number.isSafeInteger(viewer.id) && Number.isSafeInteger(account.id)
    && viewer.id === account.id && (!account.id_exact || account.id_exact === String(viewer.id));
}

export function consensusAttendanceConfirmed(value: unknown, sessionKey: string): boolean {
  if (!value || typeof value !== "object") return false;
  const reply = value as Record<string, unknown>;
  return reply.ok === true && reply.confirmed === true && reply.session_key === sessionKey;
}

/** A successful local confirmation owns the invitation. A poll started before
 * that confirmation may not reopen registration or interrupt the ballot. */
export class ConsensusInvitationGate {
  private revision = 0;
  private lastKey = "";
  private lastAt = 0;
  constructor(private readonly now = () => Date.now()) {}
  observe(): number { return this.revision; }
  private key(account: string, session: string, confirmed: boolean): string {
    return JSON.stringify([account, session, confirmed]);
  }
  accept(account: string, session: string, confirmed: boolean, observation: number): boolean {
    if (observation !== this.revision) return false;
    const key = this.key(account, session, confirmed);
    if (this.lastKey === key && (confirmed || this.now() - this.lastAt < 5 * 60_000)) return false;
    this.lastKey = key; this.lastAt = this.now();
    return true;
  }
  confirmed(account: string, session: string): void {
    this.revision++;
    this.lastKey = this.key(account, session, true); this.lastAt = this.now();
  }
  reset(): void { this.revision++; this.lastKey = ""; this.lastAt = 0; }
}
