import { describe, expect, it } from "vitest";
import { parseConsoleCommand } from "../src/shared/command-console";

describe("Blackbird command console", () => {
  it("maps only supported local commands", () => {
    expect(parseConsoleCommand("/home")).toEqual({kind:"service",id:"home"});
    expect(parseConsoleCommand("/senate")).toEqual({kind:"workspace",id:"senate"});
    expect(parseConsoleCommand("/bar right")).toEqual({kind:"bar",value:"vertical"});
    expect(parseConsoleCommand("/zoom 110")).toEqual({kind:"zoom",value:1.1});
  });
  it("never accepts shell commands or arbitrary destinations", () => {
    expect(parseConsoleCommand("rm -rf /").kind).toBe("invalid");
    expect(parseConsoleCommand("open https://example.com").kind).toBe("invalid");
    expect(parseConsoleCommand("/zoom 500").kind).toBe("invalid");
  });
});
