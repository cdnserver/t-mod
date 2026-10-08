import { describe, expect, it } from "vitest";
import { normalizeHardwareUuid } from "../src/shared/device-identity";

describe("hardware UUID binding", () => {
  it("accepts an ordinary UUID without changing its identity", () => {
    expect(normalizeHardwareUuid("  4C4C4544-0038-5510-8050-B7C04F463233  "))
      .toBe("4c4c4544-0038-5510-8050-b7c04f463233");
  });
  it("rejects generic and missing motherboard identifiers", () => {
    for (const value of ["", "To be filled by O.E.M.", "00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff", "00000000-0000-0000-0000-000000000001"]) {
      expect(normalizeHardwareUuid(value)).toBeNull();
    }
  });
});
