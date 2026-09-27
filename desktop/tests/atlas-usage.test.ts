import { describe, expect, it } from "vitest";
import { atlasUsageMeter } from "../src/shared/atlas-usage";

describe("Atlas active ledger meter", () => {
  it("shows included balance before prepaid credits", () => {
    expect(atlasUsageMeter({monthly_balance_tokens:250,monthly_capacity_tokens:1000,payg_balance_tokens:900})).toEqual({remaining:250,capacity:1000,percent:25,prepaid:false});
  });
  it("switches to prepaid capacity once the monthly grant is spent", () => {
    expect(atlasUsageMeter({monthly_balance_tokens:0,payg_balance_tokens:400,payg_capacity_tokens:2000})).toEqual({remaining:400,capacity:2000,percent:20,prepaid:true});
  });
  it("handles empty and old-server responses without dividing by zero", () => {
    expect(atlasUsageMeter({}).percent).toBe(0);
    expect(atlasUsageMeter({monthly_balance_tokens:100,plan:{monthly_tokens:500}}).percent).toBe(20);
    expect(atlasUsageMeter({payg_balance_tokens:-100}).remaining).toBe(0);
  });
});
