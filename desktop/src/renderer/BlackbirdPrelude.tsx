import signature from "./assets/blackbird/technologies-signature.png";
import { useId, type CSSProperties } from "react";
import "./blackbird-prelude.css";
import type { preloadSummary } from "./blackbird-preload";

// Reveal the approved artwork itself, not a replacement font. Adjacent mask
// cells overlap by one source pixel, so the settled signature has no seams.
const signatureCells = [
  ...Array.from({ length: 10 }, (_, i) => ({ x: 36 + i * 85.6, width: 86.6, delay: 1.1 + i * .13 })),
  ...Array.from({ length: 12 }, (_, i) => ({ x: 898 + i * 89.2, width: 90.2, delay: 2.35 + i * .12 })),
  { x: 1968, width: 204, delay: 4.1 },
];

/** Publisher ident, not a loading dialog. The real launch scene loads beneath it. */
export function BlackbirdPrelude({ reduced, exiting, blackPause = false, preparation }: { reduced: boolean; exiting: boolean; blackPause?: boolean; preparation: ReturnType<typeof preloadSummary> }) {
  const maskId = `bb-ident-${useId().replace(/:/g, "")}`;
  return <div className={`bb-prelude ${reduced ? "reduced" : ""} ${exiting ? "exiting" : ""} ${blackPause ? "black-pause" : ""}`} aria-label="Технологии Товарищества">
    <div className="bb-prelude-depth" aria-hidden="true"><i/><b/></div>
    <div className="bb-prelude-stars" aria-hidden="true"/>
    <div className="bb-prelude-signature" aria-hidden="true">
      <svg className="bb-prelude-artwork" viewBox="0 0 2172 724" focusable="false">
        <defs><mask id={maskId} maskUnits="userSpaceOnUse" x="0" y="0" width="2172" height="724" style={{ maskType: "luminance" }}>
          {signatureCells.map((cell, index) => <rect key={index} className="bb-prelude-glyph" x={cell.x} y="0" width={cell.width} height="724" fill="white" style={{ "--glyph-delay": `${cell.delay}s` } as CSSProperties}/>)}
        </mask></defs>
        <image href={signature} width="2172" height="724" mask={`url(#${maskId})`}/>
      </svg>
      <div className="bb-prelude-light" style={{ maskImage: `url(${signature})`, WebkitMaskImage: `url(${signature})` }}/>
    </div>
    <div className="bb-prelude-vignette" aria-hidden="true"/>
    {!preparation.ready && <div className="bb-prelude-loading">
      <div className="bb-prelude-progress" role="progressbar" aria-label={preparation.label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(preparation.progress * 100)} aria-valuetext={preparation.label}>
        <i style={{ transform: `scaleX(${preparation.progress})` }}/>
      </div>
      <span role="status">{preparation.label}</span>
    </div>}
  </div>;
}
