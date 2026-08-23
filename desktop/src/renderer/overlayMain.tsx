import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/manrope/400.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@fontsource/manrope/700.css";
import "@fontsource/unbounded/500.css";
import "@fontsource/unbounded/600.css";
import { AtlasOverlay } from "./overlay/AtlasOverlay";

createRoot(document.getElementById("overlay-root")!).render(
  <StrictMode>
    <AtlasOverlay />
  </StrictMode>,
);
