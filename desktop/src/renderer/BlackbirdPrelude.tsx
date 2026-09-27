import signature from "./assets/blackbird/technologies-signature.png";
import "./blackbird-prelude.css";
import type { preloadSummary } from "./blackbird-preload";

/** Publisher ident, not a loading dialog. The real launch scene loads beneath it. */
export function BlackbirdPrelude({ reduced, exiting, blackPause = false, preparation }: { reduced: boolean; exiting: boolean; blackPause?: boolean; preparation: ReturnType<typeof preloadSummary> }) {
  return <div className={`bb-prelude ${reduced ? "reduced" : ""} ${exiting ? "exiting" : ""} ${blackPause ? "black-pause" : ""}`} aria-label="Технологии Товарищества">
    <div className="bb-prelude-depth" aria-hidden="true"><i/><b/></div>
    <div className="bb-prelude-stars" aria-hidden="true"/>
    <div className="bb-prelude-signature" aria-hidden="true">
      <img src={signature} alt=""/>
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
