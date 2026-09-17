import { defineConfig } from "electron-vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

const edition = ["blackbird", "lumen"].includes(process.env.TMOD_DESKTOP_EDITION || "")
  ? "blackbird"
  : "tmod";
const productDefines = {
  __TMOD_DESKTOP_EDITION__: JSON.stringify(edition),
};

export default defineConfig({
  main: { define: productDefines },
  preload: {
    define: productDefines,
    build: {
      rollupOptions: {
        input: {
          index: resolve("src/preload/index.ts"),
          overlay: resolve("src/preload/overlay.ts"),
        },
        external: ["electron"],
        output: {
          format: "cjs",
          entryFileNames: "[name].cjs",
        },
      },
    },
  },
  renderer: {
    define: productDefines,
    plugins: [react()],
    build: {
      rollupOptions: {
        input: {
          index: resolve("src/renderer/index.html"),
          overlay: resolve("src/renderer/overlay.html"),
        },
      },
    },
  },
});
