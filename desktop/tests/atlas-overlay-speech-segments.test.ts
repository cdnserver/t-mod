import { describe, expect, it } from "vitest";
import { splitAtlasOverlaySpeech } from "../src/shared/atlas-overlay-speech-segments";

describe("Atlas Overlay streamed speech segmentation", () => {
  it("releases a completed sentence while preserving the partial next one", () => {
    expect(splitAtlasOverlaySpeech("Сначала зафиксируйте основание. Затем сообщите права")).toEqual({
      phrases: ["Сначала зафиксируйте основание."],
      remainder: " Затем сообщите права",
    });
  });

  it("does not split a short abbreviation and flushes the final tail on done", () => {
    const partial = splitAtlasOverlaySpeech("См. ст. 12 кодекса. Это важно");
    expect(partial.phrases).toEqual(["См. ст. 12 кодекса."]);
    expect(splitAtlasOverlaySpeech(partial.remainder, { flush: true })).toEqual({
      phrases: ["Это важно"],
      remainder: "",
    });
  });

  it("bounds a very long sentence at a safe soft boundary", () => {
    const text = `${"проверяйте обстоятельства ".repeat(24)}и фиксируйте вывод`;
    const result = splitAtlasOverlaySpeech(text, { maximumChars: 180, maximumPhrases: 1 });
    expect(result.phrases).toHaveLength(1);
    expect(result.phrases[0].length).toBeLessThanOrEqual(180);
    expect(result.remainder.length).toBeGreaterThan(0);
  });
});
