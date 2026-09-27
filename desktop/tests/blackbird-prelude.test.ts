import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BlackbirdPrelude } from "../src/renderer/BlackbirdPrelude";
import { BLACKBIRD_IDENT_TIMING, preloadSummary } from "../src/renderer/blackbird-preload";

describe("publisher ident loading indicator", () => {
  it.each(["letters", "veil", "light"] as const)("preserves the approved artwork in %s mode", introStyle => {
    const html = renderToStaticMarkup(createElement(BlackbirdPrelude, { reduced:false, exiting:false, preparation:preloadSummary([]), introStyle }));
    expect(html).toContain(`ident-${introStyle}`);
    expect(html).toContain('viewBox="0 0 2172 724"');
  });
  it("holds the signature and black pause before a fifteen-second lunar transition", () => {
    expect(BLACKBIRD_IDENT_TIMING.visibleMs).toBe(10_000);
    expect(BLACKBIRD_IDENT_TIMING.minimumMs).toBe(BLACKBIRD_IDENT_TIMING.visibleMs + BLACKBIRD_IDENT_TIMING.blackHoldMs);
    expect(BLACKBIRD_IDENT_TIMING.minimumMs + BLACKBIRD_IDENT_TIMING.dissolveMs).toBe(15_000);
  });
  it("reveals the original signature with staggered cells instead of replacing its lettering", () => {
    const preparation = preloadSummary([]);
    const html = renderToStaticMarkup(createElement(BlackbirdPrelude, { reduced:false, exiting:false, preparation }));
    expect(html).toContain('viewBox="0 0 2172 724"');
    expect(html.match(/class="bb-prelude-glyph"/g)).toHaveLength(23);
    expect(html).toContain("--glyph-delay:1.1s");
    expect(html).toContain("--glyph-delay:4.1s");
  });
  it("uses distinct SVG masks for simultaneous preview instances", () => {
    const ident = () => createElement(BlackbirdPrelude, { reduced:false, exiting:false, preparation:preloadSummary([]) });
    const html = renderToStaticMarkup(createElement("div", {}, ident(), ident()));
    const ids = [...html.matchAll(/<mask id="([^"]+)"/g)].map(match => match[1]);
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(2);
  });
  it("shows real completed-task progress with the pending task label", () => {
    const preparation = preloadSummary([{ label: "Шрифты", status: "ready" }, { label: "Луна", status: "pending" }]);
    const html = renderToStaticMarkup(createElement(BlackbirdPrelude, { reduced: false, exiting: false, preparation }));
    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-valuenow="50"');
    expect(html).toContain('aria-valuetext="Луна"');
    expect(html).toContain('scaleX(0.5)');
  });
  it("has no decorative loading bar once preparation has completed", () => {
    const preparation = preloadSummary([{ label: "Луна", status: "ready" }]);
    const html = renderToStaticMarkup(createElement(BlackbirdPrelude, { reduced: false, exiting: true, preparation }));
    expect(html).not.toContain('role="progressbar"');
    expect(html).toContain("exiting");
  });
});
