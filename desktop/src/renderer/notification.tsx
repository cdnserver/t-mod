import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import type { DesktopNotification } from "../shared/contracts";
import { notificationVisibilityMs } from "../shared/notification-timing";
import mark from "./assets/blackbird/master.png";
import "./notification.css";

declare global { interface Window { blackbirdNotification?: {
  onItem(listener: (value: { item: DesktopNotification; sound: boolean; reduced?: boolean }) => void): () => void;
  action(action: "open" | "dismiss"): void;
} } }

function Popup() {
  const previewMode = import.meta.env.DEV ? new URLSearchParams(location.search).get("preview") : null;
  const preview = Boolean(previewMode);
  useEffect(() => { document.documentElement.classList.toggle("notification-preview", preview); return () => document.documentElement.classList.remove("notification-preview"); }, [preview]);
  const [item, setItem] = useState<DesktopNotification | undefined>(preview ? { id: 1, severity: "info", kind: previewMode === "screenban" ? "screenban" : `orl:${previewMode === "fullscreen" ? "fullscreen" : previewMode === "overlay" ? "overlay" : "toast"}`, title: "Blackbird на связи",
    body: "Это пример уведомления. Новое сообщение мягко появится поверх игры, не перехватывая управление.", route: null, read_at: null, created_at: new Date().toISOString() } : undefined);
  const [hover, setHover] = useState(false);
  const [reduced, setReduced] = useState(false);
  useEffect(() => window.blackbirdNotification?.onItem(value => {
    setItem(value.item); setHover(false); setReduced(value.reduced === true);
    if (value.sound && typeof AudioContext !== "undefined") {
      const audio = new AudioContext();
      const start = audio.currentTime;
      for (const [index, frequency] of [523.25, 659.25, 783.99].entries()) {
        const oscillator = audio.createOscillator(), volume = audio.createGain();
        const at = start + index * .1;
        oscillator.frequency.setValueAtTime(frequency, at);
        oscillator.type = "sine";
        volume.gain.setValueAtTime(.0001, at);
        volume.gain.exponentialRampToValueAtTime(.032, at + .025);
        volume.gain.exponentialRampToValueAtTime(.0001, at + .38);
        oscillator.connect(volume).connect(audio.destination); oscillator.start(at); oscillator.stop(at + .4);
      }
      window.setTimeout(() => void audio.close(), 950);
    }
  }), []);
  useEffect(() => {
    if (!item || preview || (item.kind === "orl:toast" && hover)) return;
    const timer = setTimeout(() => window.blackbirdNotification?.action("dismiss"), notificationVisibilityMs(item.kind));
    return () => clearTimeout(timer);
  }, [item, hover, preview]);
  if (!item) return null;
  if (item.kind === "screenban") return <div className="bb-screenban" role="alert">
    <div className="bb-screenban-eclipse" aria-hidden="true"/><div className="bb-screenban-stars" aria-hidden="true"/>
    <div className="bb-screenban-content"><div className="bb-screenban-mark"><img src={mark} alt=""/> BLACKBIRD <span>·</span> ТЕХНОЛОГИИ ТОВАРИЩЕСТВА</div>
      <small>ДЕМОНСТРАЦИЯ ЭКРАНА БЛОКИРОВКИ</small><h1>Ваш аккаунт<br/><em>заблокирован.</em></h1>
      <p>Так выглядел бы экран при глобальной блокировке доступа к сервисам.</p>
      <strong>ЭТО ТОЛЬКО ВИЗУАЛЬНЫЙ ПОКАЗ · ВАШ АККАУНТ НЕ ЗАБЛОКИРОВАН</strong>
    </div><div className="bb-screenban-progress"/>
  </div>;
  const mode = item.kind === "orl:fullscreen" ? "fullscreen" : item.kind === "orl:overlay" ? "overlay" : "toast";
  return <div className={`bb-popup-shell ${mode}`}><article key={item.id} className={`bb-popup ${item.severity} ${reduced ? "reduced" : ""}`} onMouseEnter={() => { if (mode === "toast") setHover(true); }} onMouseLeave={() => setHover(false)}>
    <header><img src={mark} alt=""/><span>BLACKBIRD <i/> {mode === "toast" ? "УВЕДОМЛЕНИЕ" : "ПРЯМОЕ СООБЩЕНИЕ"}</span>{mode === "toast" && <button aria-label="Скрыть" onClick={() => window.blackbirdNotification?.action("dismiss")}>×</button>}</header>
    <button className="bb-popup-body" onClick={() => mode === "toast" && window.blackbirdNotification?.action("open")}><strong>{item.title}</strong><p>{item.body}</p>{mode === "toast" && <small>Открыть в Blackbird <b>↗</b></small>}</button>
  </article></div>;
}
createRoot(document.getElementById("root")!).render(<Popup/>);
