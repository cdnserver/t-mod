import { describe, expect, it } from "vitest";
import { ConsensusInvitationGate, consensusAttendanceConfirmed, consensusBallotLoaded, consensusHallMayEnter, consensusHallPhase, consensusViewerMatches } from "../src/shared/consensus-flow";
import type { ConsensusLiveSnapshot } from "../src/shared/contracts";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ConsensusHall } from "../src/renderer/ConsensusHall";

const snapshot: ConsensusLiveSnapshot = {
  sessionKey: "session-16", plenaryNumber: 16, stage: "registration", stageLabel: "Регистрация",
  confirmed: true, ballotAvailable: true, confirmedCount: 8, invitedCount: 12,
  quorumReady: true, currentBillTitle: null,
};

describe("Blackbird Consensus hall", () => {
  it("requires the exact native-loaded ballot, not just a service without a spinner", () => {
    const state = { activeService: "consensus" as const, loading: false, canGoBack: false, canGoForward: false, serviceReady: true, url: "https://consensus.tvr.lat/?view=ballot" };
    expect(consensusBallotLoaded(state)).toBe(true);
    expect(consensusBallotLoaded({ ...state, url: "https://consensus.tvr.lat/" })).toBe(false);
    expect(consensusBallotLoaded({ ...state, serviceReady: undefined })).toBe(false);
    expect(consensusBallotLoaded({ ...state, loading: true })).toBe(false);
    expect(consensusBallotLoaded({ ...state, locked: true })).toBe(false);
    expect(consensusBallotLoaded({ ...state, error: "service_http_504" })).toBe(false);
    expect(consensusBallotLoaded({ ...state, url: "https://consensus.tvr.lat/login?view=ballot" })).toBe(false);
  });
  it("keeps confirmed senators in the waiting hall throughout registration", () => {
    expect(consensusHallPhase(snapshot, "session-16")).toBe("waiting");
  });
  it("opens the web ballot only when the user's ballot is available and the session progresses", () => {
    expect(consensusHallPhase({ ...snapshot, stage: "presentation", ballotAvailable: false }, "session-16")).toBe("waiting");
    expect(consensusHallPhase({ ...snapshot, stage: "presentation" }, "session-16")).toBe("ready");
    expect(consensusHallPhase({ ...snapshot, stage: "voting" }, "session-16")).toBe("ready");
    for (const stage of ["finalizing", "discussion_type", "discussion", "paused", "after_result"]) {
      expect(consensusHallPhase({ ...snapshot, stage }, "session-16")).toBe("ready");
    }
    for (const stage of ["idle", "unknown", "", "voting-next"]) {
      expect(consensusHallPhase({ ...snapshot, stage }, "session-16")).toBe("waiting");
    }
  });
  it("does not enter another session or a cancelled one", () => {
    expect(consensusHallPhase({ ...snapshot, sessionKey: "next" }, "session-16")).toBe("changed");
    expect(consensusHallPhase({ ...snapshot, stage: "cancelled" }, "session-16")).toBe("finished");
    expect(consensusHallPhase(null, "session-16")).toBe("syncing");
  });
  it("requires a connected, loaded ballot for the same live session", () => {
    const ready = { ...snapshot, stage: "voting" };
    expect(consensusHallMayEnter(ready, "session-16", true, true)).toBe(true);
    expect(consensusHallMayEnter(ready, "session-16", false, true)).toBe(false);
    expect(consensusHallMayEnter(ready, "session-16", true, false)).toBe(false);
    expect(consensusHallMayEnter({ ...ready, sessionKey: "other" }, "session-16", true, true)).toBe(false);
    expect(consensusHallMayEnter({ ...ready, stage: "cancelled" }, "session-16", true, true)).toBe(false);
  });
  it("does not present a stale admission as ready while disconnected", () => {
    expect(consensusHallPhase({ ...snapshot, stage: "voting" }, "session-16", false)).toBe("syncing");
    expect(consensusHallPhase({ ...snapshot, stage: "voting" }, "session-16", true)).toBe("ready");
  });
  it("uses an exact Discord ID when the consensus server provides one", () => {
    const account = { id: 902235631952998400, id_exact: "902235631952998410" };
    expect(consensusViewerMatches({ id: 902235631952998400, id_exact: "902235631952998410" }, account)).toBe(true);
    expect(consensusViewerMatches({ id: 902235631952998400, id_exact: "902235631952998411" }, account)).toBe(false);
    expect(consensusViewerMatches({ id: 902235631952998400 }, account)).toBe(false);
    expect(consensusViewerMatches({ id: 42 }, { id: 42, id_exact: "42" })).toBe(true);
    expect(consensusViewerMatches({ id: 42 }, { id: 42, id_exact: "43" })).toBe(false);
    expect(consensusViewerMatches(undefined, account)).toBe(false);
  });
  it("offers reconnection instead of declaring admission before the first server reply", () => {
    const html = renderToStaticMarkup(createElement(ConsensusHall, {
      sessionKey: "session-16", plenaryNumber: 16, serviceReady: false,
      onEnter: () => {}, onLeave: () => {}, onRetry: () => {},
    }));
    expect(html).toContain("ПРОВЕРЯЕМ СОЕДИНЕНИЕ");
    expect(html).toContain("Повторить соединение");
    expect(html).not.toContain("ЛИЧНЫЙ ДОПУСК ПОДТВЕРЖДЁН");
    expect(html).not.toContain('disabled=""');
  });
});

describe("Consensus invitation ownership", () => {
  it("does not reopen the hall after a locally confirmed invitation", () => {
    let now = 0;
    const gate = new ConsensusInvitationGate(() => now);
    expect(gate.accept("100", "meeting", false, gate.observe())).toBe(true);
    const oldPoll = gate.observe();
    gate.confirmed("100", "meeting");
    expect(gate.accept("100", "meeting", false, oldPoll)).toBe(false);
    expect(gate.accept("100", "meeting", true, oldPoll)).toBe(false);
    now = 10 * 60_000;
    expect(gate.accept("100", "meeting", true, gate.observe())).toBe(false);
  });
  it("resumes an already confirmed session once and reminds unconfirmed invitees boundedly", () => {
    let now = 0;
    const gate = new ConsensusInvitationGate(() => now);
    expect(gate.accept("100", "meeting", true, gate.observe())).toBe(true);
    expect(gate.accept("100", "meeting", true, gate.observe())).toBe(false);
    // A fresh server observation may revoke confirmation; a late one cannot.
    expect(gate.accept("100", "meeting", false, gate.observe())).toBe(true);
    now = 299_999;
    expect(gate.accept("100", "meeting", false, gate.observe())).toBe(false);
    now = 300_000;
    expect(gate.accept("100", "meeting", false, gate.observe())).toBe(true);
  });
  it("isolates accounts and sessions, and invalidates old polls after logout", () => {
    const gate = new ConsensusInvitationGate();
    gate.confirmed("100", "meeting");
    expect(gate.accept("200", "meeting", true, gate.observe())).toBe(true);
    expect(gate.accept("200", "next", true, gate.observe())).toBe(true);
    const oldPoll = gate.observe();
    gate.reset();
    expect(gate.accept("200", "next", true, oldPoll)).toBe(false);
    expect(gate.accept("200", "next", true, gate.observe())).toBe(true);
  });
  it("requires an explicit acknowledgement of the exact session", () => {
    const reply = { ok: true, confirmed: true, session_key: "meeting" };
    expect(consensusAttendanceConfirmed(reply, "meeting")).toBe(true);
    for (const value of [null, {}, { confirmed: true }, { ...reply, ok: false }, { ...reply, confirmed: "true" }, { ...reply, session_key: "another" }]) {
      expect(consensusAttendanceConfirmed(value, "meeting")).toBe(false);
    }
  });
});
