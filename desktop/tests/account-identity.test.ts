import { describe, expect, it } from "vitest";
import { accountIdentityKey, sameAccountIdentity } from "../src/shared/account-identity";

describe("exact account identity", () => {
  it("distinguishes Discord IDs that round to the same JavaScript number", () => {
    const a = { id: 902235631952998400, id_exact: "902235631952998410" };
    const b = { id: 902235631952998400, id_exact: "902235631952998411" };
    expect(sameAccountIdentity(a, b)).toBe(false);
    expect(accountIdentityKey(a)).toBe("902235631952998410");
  });
  it("retains compatibility with a numeric-only v1 server", () => {
    expect(sameAccountIdentity({ id: 42 }, { id: 42 })).toBe(true);
    expect(sameAccountIdentity({ id: 42 }, { id: 43 })).toBe(false);
  });
});
