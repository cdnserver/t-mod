import { useEffect, useRef, useState } from "react";
import moon8k from "./assets/blackbird/moon-nasa-8k.jpg";
import { createLunarRenderer } from "./lunar-gpu";

/* A real textured sphere with fixed lighting; software fallback stays static. */
export function LunarTexture() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const gpuRef = useRef<HTMLCanvasElement>(null);
  const [ready, setReady] = useState(false);
  const [gpuReady, setGpuReady] = useState(false);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const worker = new Worker(new URL("./lunar-worker.ts", import.meta.url), { type: "module" });
    const image = new Image();
    let disposed = false;
    let initialized = false;
    let resizeTimer: ReturnType<typeof setTimeout>;
    let latestSize = 0;
    let renderer: ReturnType<typeof createLunarRenderer> = null;
    let frame = 0;
    let lastFrame = 0;
    let turn = 0;
    let previousTime = 0;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const animate = (now: number) => {
      if (disposed || !renderer) return;
      // Ignore hidden time: resume without a sudden surface jump.
      if (!document.hidden && now - lastFrame >= 1000 / 30) {
        if (!reducedMotion.matches && previousTime) turn += Math.min(now - previousTime, 100) / 720_000;
        renderer.render(turn); lastFrame = now; previousTime = now;
      }
      if (document.hidden) previousTime = 0;
      frame = requestAnimationFrame(animate);
    };
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
    const renderFallback = async () => {
      try {
        const bitmap = await createImageBitmap(image);
        if (disposed) { bitmap.close(); return; }
        latestSize = size(); initialized = true;
        worker.postMessage({ bitmap, size: latestSize }, [bitmap]);
        image.onload = null; image.src = "";
      } catch { /* Keep the vector fallback if the device cannot decode the map. */ }
    };
    const gpuCanvas = gpuRef.current;
    const contextLost = (event: Event) => {
      event.preventDefault(); cancelAnimationFrame(frame);
      renderer?.dispose(); renderer = null; setGpuReady(false);
      image.onload = () => { void renderFallback(); }; image.src = moon8k;
    };
    gpuCanvas?.addEventListener("webglcontextlost", contextLost);
    image.onload = async () => {
      try {
        if (disposed) return;
        if (gpuRef.current) renderer = createLunarRenderer(gpuRef.current, image);
        if (renderer) {
          renderer.render(0); setGpuReady(true);
          frame = requestAnimationFrame(animate);
          image.onload = null; image.src = "";
          return;
        }
        await renderFallback();
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
      cancelAnimationFrame(frame); renderer?.dispose();
      gpuCanvas?.removeEventListener("webglcontextlost", contextLost);
    };
  }, []);
  return <>
    <canvas className={`bbc-lunar-texture ${gpuReady ? "ready" : ""}`} ref={gpuRef} aria-hidden="true"/>
    <canvas className={`bbc-lunar-texture ${ready && !gpuReady ? "ready" : ""}`} ref={canvasRef} aria-hidden="true"/>
  </>;
}
