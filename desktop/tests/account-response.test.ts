import { describe, expect, it } from "vitest";
import { accountSnapshotMatches, csrfTokenFromAccountSnapshot } from "../src/shared/account-response";

describe("Blackbird account updates", () => {
  it("uses the Reactor viewer CSRF token for character and chat mutations", () => {
    expect(csrfTokenFromAccountSnapshot({ viewer: { id: 1, csrf_token: "valid" } })).toBe("valid");
  });
  it("also accepts the legacy top-level account token, never an empty value", () => {
    expect(csrfTokenFromAccountSnapshot({ csrf_token: "legacy" })).toBe("legacy");
    expect(csrfTokenFromAccountSnapshot({ viewer: { csrf_token: "" } })).toBeUndefined();
  });
  it("ties chat, media, security and billing snapshots to their actual owner", () => {
    const expected = { id: 902235631952998410, id_exact: "902235631952998410" };
    expect(accountSnapshotMatches({ viewer: { id: expected.id_exact } }, expected)).toBe(true);
    expect(accountSnapshotMatches({ viewer: { id: expected.id, id_exact: expected.id_exact } }, expected)).toBe(true);
    expect(accountSnapshotMatches({ links: { discord: expected.id_exact } }, expected)).toBe(true);
    expect(accountSnapshotMatches({ account: { user_id: expected.id } }, expected)).toBe(true);
    expect(accountSnapshotMatches({ viewer: { id: "902235631952998411" } }, expected)).toBe(false);
    expect(accountSnapshotMatches({ viewer: { id: expected.id, id_exact: "902235631952998411" } }, expected)).toBe(false);
    expect(accountSnapshotMatches({ links: { discord: "other-account" } }, expected)).toBe(false);
    expect(accountSnapshotMatches({ account: { user_id: 99 } }, expected)).toBe(false);
    expect(accountSnapshotMatches({ csrf_token: "present-but-no-owner" }, expected)).toBe(false);
  });
});
