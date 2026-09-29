import { describe, expect, it } from "vitest";
import { csrfTokenFromAccountSnapshot } from "../src/shared/account-response";

describe("Blackbird account updates", () => {
  it("uses the Reactor viewer CSRF token for character and chat mutations", () => {
    expect(csrfTokenFromAccountSnapshot({ viewer: { id: 1, csrf_token: "valid" } })).toBe("valid");
  });
  it("also accepts the legacy top-level account token, never an empty value", () => {
    expect(csrfTokenFromAccountSnapshot({ csrf_token: "legacy" })).toBe("legacy");
    expect(csrfTokenFromAccountSnapshot({ viewer: { csrf_token: "" } })).toBeUndefined();
  });
});
