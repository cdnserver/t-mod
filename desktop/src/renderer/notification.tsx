import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import type { DesktopNotification } from "../shared/contracts";
import mark from "./assets/blackbird/master.png";
import "./notification.css";

declare global { interface Window { blackbirdNotification?: {
  onItem(listener: (value: { item: DesktopNotification; sound: boolean; reduced?: boolean }) => void): () => void;
  action(action: "open" | "dismiss"): void;
} } }

function Popup() {
  const preview = import.meta.env.DEV && new URLSearchParams(location.search).get("preview") === "1";
  const [item, setItem] = useState<DesktopNotification | undefined>(preview ? { id: 1, severity: "info", kind: "preview", title: "Ваше пространство готово",
    body: "Новые материалы появились в Сенате. Откройте Blackbird, чтобы продолжить.", route: null, read_at: null, created_at: new Date().toISOString() } : undefined);
  const [hover, setHover] = useState(false);
  const [reduced, setReduced] = useState(false);
  useEffect(() => window.blackbirdNotification?.onItem(value => {
    setItem(value.item); setHover(false); setReduced(value.reduced === true);
    if (value.sound) {
      const audio = new AudioContext();
      const oscillator = audio.createOscillator(), volume = audio.createGain();
      oscillator.frequency.value = 440; oscillator.type = "sine";
      volume.gain.setValueAtTime(.045, audio.currentTime);
      volume.gain.exponentialRampToValueAtTime(.001, audio.currentTime + .4);
      oscillator.connect(volume).connect(audio.destination); oscillator.start(); oscillator.stop(audio.currentTime + .4);
      oscillator.onended = () => { void audio.close(); };
    }
  }), []);
  useEffect(() => {
    if (!item || hover || preview || item.severity === "critical") return;
    const timer = setTimeout(() => window.blackbirdNotification?.action("dismiss"), 8500);
    return () => clearTimeout(timer);
  }, [item, hover, preview]);
  if (!item) return null;
  return <article key={item.id} className={`bb-popup ${item.severity} ${reduced ? "reduced" : ""}`} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
    <header><img src={mark} alt=""/><span>BLACKBIRD <i/> {item.severity === "critical" ? "ВАЖНО" : "СОБЫТИЕ"}</span><button aria-label="Скрыть" onClick={() => window.blackbirdNotification?.action("dismiss")}>×</button></header>
    <button className="bb-popup-body" onClick={() => window.blackbirdNotification?.action("open")}><strong>{item.title}</strong><p>{item.body}</p><small>Открыть в Blackbird <b>↗</b></small></button>
  </article>;
}
createRoot(document.getElementById("root")!).render(<Popup/>);
