import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/manrope/400.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@fontsource/manrope/700.css";
import "@fontsource/unbounded/500.css";
import "@fontsource/unbounded/600.css";
import { App } from "./App";
import { desktopProduct } from "../shared/product";
import "./styles.css";
import "./cinematics.css";
import "./lumen.css";

document.title = desktopProduct.fullName;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
