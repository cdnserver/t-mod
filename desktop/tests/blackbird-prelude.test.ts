import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BlackbirdPrelude } from "../src/renderer/BlackbirdPrelude";
import { preloadSummary } from "../src/renderer/blackbird-preload";

describe("publisher ident loading indicator", () => {
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
