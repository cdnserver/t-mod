import { describe, expect, it } from "vitest";
import { NavigationIntent, navigationWasAborted, ServiceNavigation } from "../src/shared/service-navigation";

const ballot = "https://consensus.tvr.lat/?view=ballot";

describe("service navigation ownership", () => {
  it("invalidates a pending load when another destination or the hub is selected", () => {
    const navigation = new ServiceNavigation();
    const first = navigation.begin(ballot);
    const second = navigation.begin("https://reactor.tvr.lat/reactor");
    expect(navigation.current(first)).toBe(false);
    expect(navigation.current(second)).toBe(true);
    navigation.begin();
    expect(navigation.current(second)).toBe(false);
    expect(navigation.ready).toBe(false);
    expect(navigation.destination).toBe("");
  });
  it("retries the precise document and ignores rejection from an earlier attempt", () => {
    const navigation = new ServiceNavigation();
    const ticket = navigation.begin(ballot);
    const oldAttempt = navigation.startAttempt();
    const retry = navigation.startAttempt();
    expect(navigation.destination).toBe(ballot);
    expect(navigation.currentAttempt(ticket, oldAttempt)).toBe(false);
    expect(navigation.currentAttempt(ticket, retry)).toBe(true);
  });
  it("does not mistake a landing page, another query or origin for a loaded ballot", () => {
    const navigation = new ServiceNavigation();
    navigation.begin(ballot);
    expect(navigation.finish("https://consensus.tvr.lat/", true)).toBe(false);
    expect(navigation.finish("https://consensus.tvr.lat/?view=archive", true)).toBe(false);
    expect(navigation.finish("https://consensus.tvr.lat.evil.test/?view=ballot", true)).toBe(false);
    expect(navigation.finish(`${ballot}#article`, true)).toBe(true);
  });
  it.each([401, 403, 423, 500, 504])("never marks HTTP %i as ready", status => {
    const navigation = new ServiceNavigation();
    navigation.begin(ballot);
    navigation.status = status;
    expect(navigation.finish(ballot, true)).toBe(false);
    expect(navigation.ready).toBe(false);
  });
  it("follows a real redirect but does not expose an authentication document", () => {
    const navigation = new ServiceNavigation();
    const ticket = navigation.begin(ballot);
    const login = "https://tvr.lat/login?next=/consensus";
    navigation.redirect(login);
    expect(navigation.current(ticket)).toBe(true);
    expect(navigation.finish(ballot, true)).toBe(false);
    expect(navigation.finish(login, false)).toBe(false);
  });
  it("recognizes aborted loads without classifying unrelated errors as cancellation", () => {
    expect(navigationWasAborted({ code: "ERR_ABORTED" })).toBe(true);
    expect(navigationWasAborted({ errno: -3 })).toBe(true);
    expect(navigationWasAborted(new Error("ERR_ABORTED (-3) loading URL"))).toBe(true);
    expect(navigationWasAborted(new Error("ERR_CONNECTION_REFUSED"))).toBe(false);
    expect(navigationWasAborted(null)).toBe(false);
  });
  it("ignores a delayed renderer reply after another click or lock invalidation", async () => {
    const intent = new NavigationIntent();
    let resolve!: () => void;
    const delayed = new Promise<void>(done => { resolve = done; });
    const first = intent.begin();
    let screen = "hub";
    const reply = delayed.then(() => { if (intent.current(first)) screen = "senate"; });
    intent.begin();
    resolve();
    await reply;
    expect(screen).toBe("hub");
  });
});
