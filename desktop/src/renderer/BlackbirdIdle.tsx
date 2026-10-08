import { useEffect, useState } from "react";
import { LunarTexture } from "./LunarTexture";
import technologies from "./assets/blackbird/technologies-signature.png";
import "./blackbird-idle.css";

const stars = Array.from({ length: 42 }, (_, index) => ({
  left: `${(index * 137.53 + 11) % 100}%`, top: `${(index * 73.19 + 17) % 94}%`,
  size: index % 13 === 0 ? "2px" : "1px",
  delay: `${(index * 1.73) % 11}s`,
}));

/** A cinematic pause, distinct from the old T-Mod vault and star-digit clock. */
export function BlackbirdIdle({ name, reduced, unlocking, checking = false, error = false, onMinimize }: { name: string; reduced: boolean; unlocking: boolean; checking?: boolean; error?: boolean; onMinimize: () => void }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const update = () => setNow(new Date());
    const timer = window.setInterval(update, 1000);
    document.addEventListener("visibilitychange", update);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", update); };
  }, []);
  const hour = now.getHours();
  const greeting = hour < 6 ? "Доброй ночи" : hour < 12 ? "Доброе утро" : hour < 18 ? "Добрый день" : "Добрый вечер";
  const time = now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return <section className={`bb-idle ${reduced ? "still" : ""} ${unlocking ? "leaving" : ""}`} aria-label="Blackbird · экран ожидания">
    <div className="bbi-sky" aria-hidden="true">{stars.map((star, index) => <i key={index} style={{ left: star.left, top: star.top, width: star.size, height: star.size, animationDelay: star.delay }}/>)}</div>
    <div className="bbi-halo" aria-hidden="true"/>
    <svg className="bbi-orbits" viewBox="0 0 1440 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true"><path d="M-270 716 C174 82 752 -144 1555 217"/><path className="bbi-far-orbit" d="M-260 782 C344 205 964 157 1604 488"/><circle cx="1151" cy="116" r="2.2"/><circle cx="326" cy="475" r="1.5"/></svg>
    <div className="bbi-traveler" aria-hidden="true"><i/><i/></div>
    <div className="bbi-moon" aria-hidden="true"><LunarTexture reduced={reduced}/></div>
    <div className="bbi-moon-shadow" aria-hidden="true"/>
    <header><span>BLACKBIRD <i/> ПАУЗА</span><button onClick={onMinimize} aria-label="Свернуть приложение">−</button></header>
    <main><div className="bbi-scene-index">01 / МОМЕНТ ТИШИНЫ</div><div className="bbi-date">{now.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })}</div><time key={time} dateTime={now.toISOString()}>{time}</time><div className="bbi-divider"/><h1>{greeting}{name ? `, ${name}` : ""}.</h1><p>Вы на паузе. Ваше пространство остаётся рядом.</p></main>
    <div className="bbi-signature" aria-hidden="true"><img src={technologies} alt=""/></div>
    <footer><span><i/> СЕАНС ЗАЩИЩЁН</span><small className={error ? "bbi-unlock-error" : ""}>{checking ? "Проверяем доступ…" : error ? "Нет связи или доступ закрыт. Нажмите клавишу для повтора." : "Нажмите любую клавишу"} {!checking && <b>↵</b>}</small></footer>
  </section>;
}
