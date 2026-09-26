import wordmark from "./assets/blackbird/wordmark.svg";

/** Vector artwork redrawn from the approved Blackbird lettering reference. */
export function BlackbirdWordmark() {
  return <img className="bb-wordmark" src={wordmark} alt="Blackbird" draggable={false} />;
}
