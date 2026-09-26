import { useEffect, useRef, useState } from "react";
import moon8k from "./assets/blackbird/moon-nasa-8k.jpg";

/* Render the real LRO map once, off the UI thread. This also works on machines
   where Chromium's WebGL/GPU driver is blocked. Idle motion is CSS-only. */
export function LunarTexture() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const worker = new Worker(new URL("./lunar-worker.ts", import.meta.url), { type: "module" });
    const image = new Image();
    let disposed = false;
    let initialized = false;
    let resizeTimer: ReturnType<typeof setTimeout>;
    let latestSize = 0;
    const size = () => Math.max(1, Math.min(4096, Math.ceil(canvas.clientWidth * Math.min(2, window.devicePixelRatio || 1))));
    worker.onmessage = (event: MessageEvent<{ bitmap?: ImageBitmap }>) => {
      const bitmap = event.data.bitmap;
      if (!bitmap) return;
      if (!disposed) {
        canvas.width = bitmap.width; canvas.height = bitmap.height;
        canvas.getContext("2d", { alpha: true })?.drawImage(bitmap, 0, 0);
        setReady(true);
      }
      bitmap.close();
    };
    image.onload = async () => {
      try {
        const bitmap = await createImageBitmap(image);
        if (disposed) { bitmap.close(); return; }
        latestSize = size(); initialized = true;
        worker.postMessage({ bitmap, size: latestSize }, [bitmap]);
        image.onload = null; image.src = "";
      } catch { /* Keep the vector fallback if the device cannot decode the map. */ }
    };
    image.src = moon8k;
    const observer = new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        const next = size();
        if (initialized && Math.abs(next - latestSize) > 64) {
          latestSize = next; worker.postMessage({ size: next });
        }
      }, 180);
    });
    observer.observe(canvas);
    return () => {
      disposed = true; image.onload = null; image.src = "";
      observer.disconnect(); clearTimeout(resizeTimer); worker.terminate();
    };
  }, []);
  return <canvas className={`bbc-lunar-texture ${ready ? "ready" : ""}`} ref={canvasRef} aria-hidden="true"/>;
}
