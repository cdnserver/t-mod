import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  define: { __TMOD_DESKTOP_EDITION__: JSON.stringify("blackbird") },
  server: { host: "127.0.0.1", port: 5174, strictPort: true },
});
