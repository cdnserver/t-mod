import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/manrope/400.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@fontsource/manrope/700.css";
import "@fontsource/unbounded/500.css";
import "@fontsource/unbounded/600.css";
import "@fontsource/ibm-plex-sans/200.css";
import "@fontsource/ibm-plex-sans/300.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import { App } from "./App";
import { desktopProduct } from "../shared/product";
import "./styles.css";
import "./cinematics.css";
import "./lumen.css";
import "./blackbird.css";
import "./blackbird-hub.css";
import "./blackbird-login.css";
import "./blackbird-launch.css";
import "./blackbird-refine.css";
import "./blackbird-recut.css";
import "./blackbird-hub-recut.css";
import "./blackbird-control-bar.css";
import "./blackbird-workspace.css";

document.title = desktopProduct.fullName;
// Blackbird has its own explicit accessibility preference. Windows' global
// animation switch must not silently override the client's chosen appearance.
if (desktopProduct.privateEdition) document.documentElement.dataset.motionPolicy = "app";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
