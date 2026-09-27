import { useEffect, useState } from "react";
import master from "./assets/blackbird/master-hd.png";
import signature from "./assets/blackbird/technologies-signature.png";
import wordmark from "./assets/blackbird/wordmark.svg";
import { preloadSummary, type PreloadStatus } from "./blackbird-preload";

export function useBlackbirdPreload(enabled: boolean, connectionReady: boolean, moon: PreloadStatus) {
  const [fonts, setFonts] = useState<PreloadStatus>("pending");
  const [images, setImages] = useState<PreloadStatus>("pending");
  const [connection, setConnection] = useState<PreloadStatus>(connectionReady ? "ready" : "pending");
  useEffect(() => {
    if (connectionReady) setConnection("ready");
  }, [connectionReady]);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const finish = (setter: typeof setFonts, status: PreloadStatus) => {
      if (!cancelled) setter(current => current === "pending" ? status : current);
    };
    void document.fonts.ready.then(() => finish(setFonts, "ready"), () => finish(setFonts, "fallback"));
    const assets = [master, signature, wordmark].map(src => {
      const image = new Image(); image.src = src;
      return image;
    });
    void Promise.all(assets.map(image => image.decode())).then(
      () => finish(setImages, "ready"), () => finish(setImages, "fallback"),
    );
    // Never strand the client on an unavailable network or a broken graphics driver.
    const deadline = window.setTimeout(() => {
      finish(setFonts, "fallback"); finish(setImages, "fallback"); finish(setConnection, "fallback");
    }, 15000);
    return () => { cancelled = true; window.clearTimeout(deadline); };
  }, [enabled]);
  return preloadSummary([
    { label: "Подготавливаем шрифты", status: fonts },
    { label: "Подготавливаем оформление", status: images },
    { label: "Подготавливаем лунную сцену", status: moon },
    { label: "Проверяем вход в аккаунт", status: connection },
  ]);
}
