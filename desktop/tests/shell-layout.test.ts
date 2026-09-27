import { describe, expect, it } from "vitest";
import { normalizeIntroStyle, serviceBounds } from "../src/shared/shell-layout";

describe("Blackbird control bar and native service alignment", () => {
  it("reserves the compact header and full service navigation", () => {
    expect(serviceBounds(1200,800,true,false,false)).toEqual({x:286,y:44,width:914,height:756});
  });
  it("reserves the right rail without leaving an unused header", () => {
    expect(serviceBounds(1200,800,true,true,false)).toEqual({x:286,y:0,width:866,height:800});
    expect(serviceBounds(1200,800,true,true,true)).toEqual({x:78,y:0,width:1074,height:800});
  });
  it("leaves the public T-Mod layout unchanged", () => {
    expect(serviceBounds(1200,800,false,true,false)).toEqual({x:286,y:70,width:914,height:730});
  });
  it("never submits negative native bounds", () => {
    expect(serviceBounds(10,10,true,false,false).width).toBe(1);
    expect(serviceBounds(10,10,true,false,false).height).toBe(1);
  });
  it("migrates missing and invalid intro preferences safely", () => {
    expect(normalizeIntroStyle(undefined)).toBe("letters");
    expect(normalizeIntroStyle("bad")).toBe("letters");
    expect(normalizeIntroStyle("veil")).toBe("veil");
    expect(normalizeIntroStyle("light")).toBe("light");
  });
});
