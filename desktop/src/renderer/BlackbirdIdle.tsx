import { useEffect, useState } from "react";
import { LunarTexture } from "./LunarTexture";
import "./blackbird-idle.css";

/** Blackbird's own calm observatory. No inherited T-Mod vault or spinning cube. */
export function BlackbirdIdle({ name, reduced, unlocking, onMinimize }: { name: string; reduced: boolean; unlocking: boolean; onMinimize: () => void }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const update = () => setNow(new Date());
    const timer = window.setInterval(update, 1000);
    document.addEventListener("visibilitychange", update);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", update); };
  }, []);
  const hour = now.getHours();
  const greeting = hour < 6 ? "Доброй ночи" : hour < 12 ? "Доброе утро" : hour < 18 ? "Добрый день" : "Добрый вечер";
  return <section className={`bb-idle ${reduced ? "still" : ""} ${unlocking ? "leaving" : ""}`} aria-label="Blackbird · экран ожидания">
    <div className="bbi-stars" aria-hidden="true"/><div className="bbi-horizon" aria-hidden="true"/>
    <div className="bbi-moon" aria-hidden="true"><LunarTexture reduced={reduced}/></div>
    <header><span>BLACKBIRD <i/> OBSERVATORY</span><button onClick={onMinimize} aria-label="Свернуть приложение">−</button></header>
    <main><div className="bbi-date">{now.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })}</div><time dateTime={now.toISOString()}>{now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}</time><div className="bbi-divider"/><h1>{greeting}{name ? `, ${name}` : ""}.</h1><p>Пусть следующая мысль будет ясной.</p></main>
    <footer><span>Ваше пространство ждёт.</span><small>Нажмите клавишу, чтобы продолжить <b>↵</b></small></footer>
  </section>;
}
