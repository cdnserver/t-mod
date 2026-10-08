import { useEffect, useState } from "react";
import { BlackbirdPrelude } from "./BlackbirdPrelude";
import blackbirdMaster from "./assets/blackbird/master.png";
import "./blackbird-ban.css";

export function BlackbirdBanSequence(props: Parameters<typeof BlackbirdBan>[0] & { introStyle?: "letters" | "veil" | "light" }) {
  const [phase, setPhase] = useState<"ident" | "dissolve" | "decision">("ident");
  useEffect(() => {
    const dissolve = window.setTimeout(() => setPhase("dissolve"), 8_800);
    const decision = window.setTimeout(() => setPhase("decision"), 10_450);
    return () => { window.clearTimeout(dissolve); window.clearTimeout(decision); };
  }, []);
  return <div className="bb-ban-sequence">
    <BlackbirdBan {...props}/>
    {phase !== "decision" && (
      <BlackbirdPrelude banTone reduced={false} exiting={phase === "dissolve"}
        preparation={{ ready: true, progress: 1, label: "Всё готово", degraded: false }} introStyle={props.introStyle}/>
    )}
  </div>;
}

export function BlackbirdBan({
  reason,
  reference,
  onMinimize,
  onClose,
}: {
  reason: string;
  reference: string;
  onMinimize: () => void;
  onClose: () => void;
}) {
  return <section className="blackbird-ban" role="alert" aria-labelledby="blackbird-ban-title">
    <div className="bb-ban-sky" aria-hidden="true"><i/><i/><i/><i/><i/></div>
    <div className="bb-ban-eclipse" aria-hidden="true"><span/><i/><i/></div>
    <header className="bb-ban-top"><div className="bb-ban-identity"><img src={blackbirdMaster} alt=""/><span>BLACKBIRD <small>ТЕХНОЛОГИИ ТОВАРИЩЕСТВА</small></span></div><div className="bb-ban-window"><button onClick={onMinimize} aria-label="Свернуть окно">−</button><button onClick={onClose} aria-label="Закрыть окно">×</button></div></header>
    <main className="bb-ban-content">
      <div className="bb-ban-eyebrow"><span/> РЕШЕНИЕ О ДОСТУПЕ <em>·</em> {reference || "GB-—"}</div>
      <h1 id="blackbird-ban-title">Ваш аккаунт<br/><span>заблокирован.</span></h1>
      <p>Доступ к сервисам Технологий Товарищества прекращён. Пока глобальная блокировка действует, вход и работа в закрытых контурах недоступны.</p>
      <div className="bb-ban-case"><small>ОСНОВАНИЕ РЕШЕНИЯ</small><strong>{reason || "Решение администратора."}</strong><span>Снять блокировку может только уполномоченный администратор.</span></div>
    </main>
    <footer className="bb-ban-footer"><span><i/> СТАТУС · ДОСТУП ПРЕКРАЩЁН</span><small>BLACKBIRD / IDENTITY SECURITY</small></footer>
  </section>;
}
