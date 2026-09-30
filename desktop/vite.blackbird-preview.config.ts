import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// Browser-only preview of the real Blackbird renderer; no Electron or login.
export default defineConfig({
  root: resolve("src/renderer"),
  define: { __TMOD_DESKTOP_EDITION__: JSON.stringify("blackbird") },
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5174, strictPort: true },
});
