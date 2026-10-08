import { describe, expect, it } from "vitest";
import { mayCompleteUnlock, ProtectedAccessGate, type ProtectedAccessState } from "../src/shared/protected-access";

function context() {
  const state: ProtectedAccessState = { revision: 1, accountId: "902235631952998410", locked: false, banned: false };
  return { state, gate: new ProtectedAccessGate(() => state) };
}

describe("protected client request lifetime", () => {
  it("rejects capture while paused, banned or unauthenticated", () => {
    for (const update of [{ locked: true }, { banned: true }, { accountId: null }]) {
      const { state, gate } = context();
      Object.assign(state, update);
      expect(() => gate.capture()).toThrow("access_locked");
    }
  });
  it("aborts in-flight requests and never revives them on unlock", () => {
    const { state, gate } = context();
    const previous = gate.capture();
    state.locked = true; gate.revoke();
    expect(previous.signal.aborted).toBe(true);
    state.locked = false;
    expect(() => previous.assertCurrent()).toThrow("session_changed");
    const next = gate.capture();
    expect(next.signal.aborted).toBe(false);
    expect(() => next.assertCurrent()).not.toThrow();
  });
  it("rejects late results on a new session even for the same account", () => {
    const { state, gate } = context();
    const lease = gate.capture();
    state.revision++;
    expect(() => lease.assertCurrent()).toThrow("session_changed");
  });
  it("distinguishes exact Discord snowflakes when the numeric IDs would round together", () => {
    const { state, gate } = context();
    const lease = gate.capture();
    state.accountId = "902235631952998411";
    expect(() => lease.assertCurrent()).toThrow("session_changed");
  });
  it("does not send a mutation when the context request completes after locking", async () => {
    const { state, gate } = context();
    const lease = gate.capture();
    let resolve!: () => void;
    const pending = new Promise<void>(done => { resolve = done; });
    let sent = false;
    const action = (async () => { await pending; lease.assertCurrent(); sent = true; })();
    state.locked = true; gate.revoke(); resolve();
    await expect(action).rejects.toThrow("session_changed");
    expect(sent).toBe(false);
  });
  it("permits a same-account refresh without cancelling an unrelated operation", () => {
    const { gate } = context();
    const lease = gate.capture();
    expect(() => lease.assertCurrent()).not.toThrow();
    expect(lease.signal.aborted).toBe(false);
  });
  it("does not let an older unlock finish after a repeated lock or account change", () => {
    const previous = { lockRevision: 1, sessionRevision: 3, accountId: "100" };
    expect(mayCompleteUnlock(previous, { ...previous }, false)).toBe(true);
    expect(mayCompleteUnlock(previous, { ...previous, lockRevision: 2 }, false)).toBe(false);
    expect(mayCompleteUnlock(previous, { ...previous, sessionRevision: 4 }, false)).toBe(false);
    expect(mayCompleteUnlock(previous, { ...previous, accountId: "200" }, false)).toBe(false);
    expect(mayCompleteUnlock(previous, { ...previous, accountId: null }, false)).toBe(false);
    expect(mayCompleteUnlock(previous, { ...previous }, true)).toBe(false);
    expect(mayCompleteUnlock({ ...previous, accountId: null }, { ...previous, accountId: null }, false)).toBe(false);
  });
});
