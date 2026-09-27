import { useEffect, useState } from "react";
import { LunarTexture } from "./LunarTexture";
import { ConstellationClock } from "./ConstellationClock";
import technologies from "./assets/blackbird/technologies-signature.png";
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
    <div className="bbi-deepfield" aria-hidden="true">{Array.from({ length: 56 }, (_, i) => <i key={i} style={{ left:`${(i * 43 + 7) % 100}%`, top:`${(i * 29 + 13) % 100}%`, animationDelay:`${-(i % 17)}s`, animationDuration:`${5 + i % 8}s` }}/>)}</div>
    <div className="bbi-meteors" aria-hidden="true">{[0,1,2,3].map(i => <i key={i} style={{ top:`${12+i*18}%`, left:`${28+i*14}%`, animationDelay:`${i*6+2}s`, animationDuration:`${23+i*7}s` }}/>)}</div>
    <div className="bbi-moon" aria-hidden="true"><LunarTexture reduced={reduced}/></div>
    <header><span>BLACKBIRD <i/> OBSERVATORY</span><button onClick={onMinimize} aria-label="Свернуть приложение">−</button></header>
    <main><div className="bbi-date">{now.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })}</div><time dateTime={now.toISOString()}><ConstellationClock time={now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}/></time><div className="bbi-divider"/><h1>{greeting}{name ? `, ${name}` : ""}.</h1><p>Пусть следующая мысль будет ясной.</p></main>
    <div className="bbi-signature" aria-hidden="true"><img src={technologies} alt=""/></div>
    <footer><span>Ваше пространство ждёт.</span><small>Нажмите клавишу, чтобы продолжить <b>↵</b></small></footer>
  </section>;
}
