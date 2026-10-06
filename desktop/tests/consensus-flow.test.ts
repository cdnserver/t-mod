import { describe, expect, it } from "vitest";
import { consensusHallPhase } from "../src/shared/consensus-flow";
import type { ConsensusLiveSnapshot } from "../src/shared/contracts";

const snapshot: ConsensusLiveSnapshot = {
  sessionKey: "session-16", plenaryNumber: 16, stage: "registration", stageLabel: "Регистрация",
  confirmed: true, ballotAvailable: true, confirmedCount: 8, invitedCount: 12,
  quorumReady: true, currentBillTitle: null,
};

describe("Blackbird Consensus hall", () => {
  it("keeps confirmed senators in the waiting hall throughout registration", () => {
    expect(consensusHallPhase(snapshot, "session-16")).toBe("waiting");
  });
  it("opens the web ballot only when the user's ballot is available and the session progresses", () => {
    expect(consensusHallPhase({ ...snapshot, stage: "presentation", ballotAvailable: false }, "session-16")).toBe("waiting");
    expect(consensusHallPhase({ ...snapshot, stage: "presentation" }, "session-16")).toBe("ready");
    expect(consensusHallPhase({ ...snapshot, stage: "voting" }, "session-16")).toBe("ready");
  });
  it("does not enter another session or a cancelled one", () => {
    expect(consensusHallPhase({ ...snapshot, sessionKey: "next" }, "session-16")).toBe("changed");
    expect(consensusHallPhase({ ...snapshot, stage: "cancelled" }, "session-16")).toBe("finished");
    expect(consensusHallPhase(null, "session-16")).toBe("syncing");
  });
});
