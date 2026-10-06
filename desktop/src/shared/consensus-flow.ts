import type { ConsensusLiveSnapshot } from "./contracts";

export type ConsensusHallPhase = "syncing" | "waiting" | "ready" | "finished" | "changed";

export function consensusHallPhase(snapshot: ConsensusLiveSnapshot | null, sessionKey: string): ConsensusHallPhase {
  if (!snapshot) return "syncing";
  if (!snapshot.sessionKey || snapshot.stage === "finished" || snapshot.stage === "cancelled") return "finished";
  if (snapshot.sessionKey !== sessionKey) return "changed";
  if (snapshot.confirmed && snapshot.ballotAvailable && snapshot.stage !== "registration") return "ready";
  return "waiting";
}
