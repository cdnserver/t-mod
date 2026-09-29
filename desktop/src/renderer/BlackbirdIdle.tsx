import { useEffect, useRef, useState } from "react";
import { LunarTexture } from "./LunarTexture";
import { ConstellationClock } from "./ConstellationClock";
import technologies from "./assets/blackbird/technologies-signature.png";
import observatory from "./assets/blackbird/observatory-constellations-v1.png";
import "./blackbird-idle.css";

/** Blackbird's own calm observatory. No inherited T-Mod vault or spinning cube. */
export function BlackbirdIdle({ name, reduced, unlocking, onMinimize }: { name: string; reduced: boolean; unlocking: boolean; onMinimize: () => void }) {
  const [now, setNow] = useState(() => new Date());
  const skyImage = useRef<HTMLImageElement>(null);
  const starflight = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const update = () => setNow(new Date());
    const timer = window.setInterval(update, 1000);
    document.addEventListener("visibilitychange", update);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", update); };
  }, []);
  const hour = now.getHours();
  const greeting = hour < 6 ? "Доброй ночи" : hour < 12 ? "Доброе утро" : hour < 18 ? "Добрый день" : "Добрый вечер";
  return <section className={`bb-idle ${reduced ? "still" : ""} ${unlocking ? "leaving" : ""}`} aria-label="Blackbird · экран ожидания">
    <div className="bbi-sky" aria-hidden="true"><img ref={skyImage} src={observatory} alt=""/></div>
    <div className="bbi-horizon" aria-hidden="true"/>
    <canvas ref={starflight} className="bbi-starflight" aria-hidden="true"/>
    <svg className="bbi-meteors" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
      <path className="bbi-meteor-trail one" d="M-90 105 C330 172 720 278 1260 508"/>
      <path className="bbi-meteor-trail two" d="M170 -110 C475 55 855 250 1310 542"/>
      <path className="bbi-meteor-trail three" d="M-75 368 C340 352 790 418 1240 615"/>
    </svg>
    <div className="bbi-moon" aria-hidden="true"><LunarTexture reduced={reduced}/></div>
    <header><span>BLACKBIRD <i/> OBSERVATORY</span><button onClick={onMinimize} aria-label="Свернуть приложение">−</button></header>
    <main><div className="bbi-date">{now.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })}</div><time dateTime={now.toISOString()}><ConstellationClock time={now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })} reduced={reduced} skyImage={skyImage} starflight={starflight}/></time><div className="bbi-divider"/><h1>{greeting}{name ? `, ${name}` : ""}.</h1><p>Пусть следующая мысль будет ясной.</p></main>
    <div className="bbi-signature" aria-hidden="true"><img src={technologies} alt=""/></div>
    <footer><span>Ваше пространство ждёт.</span><small>Нажмите клавишу, чтобы продолжить <b>↵</b></small></footer>
  </section>;
}
