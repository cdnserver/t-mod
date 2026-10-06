import { useEffect, useRef, useState, type FormEvent } from "react";
import { parseConsoleCommand, type ConsoleCommand } from "../shared/command-console";
import "./command-console.css";

export function CommandConsole({ onClose, onCommand }: { onClose: () => void; onCommand: (command: ConsoleCommand) => void }) {
  const [value, setValue] = useState("");
  const [message, setMessage] = useState("/help — все доступные команды");
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const command = parseConsoleCommand(value);
    if (command.kind === "invalid") { setMessage(command.message); return; }
    if (command.kind === "help") { setMessage("/home · /atlas · /senate · /reactor · /settings · /lock · /notifications · /bar top|right · /zoom 90|100|110 · /window min|max"); return; }
    onCommand(command);
    onClose();
  };
  return <div className="bb-console-backdrop" onPointerDown={onClose}><section className="bb-console" role="dialog" aria-modal="true" aria-label="Консоль Blackbird" onPointerDown={event => event.stopPropagation()}>
    <header><span>BLACKBIRD / КОНСОЛЬ</span><button onClick={onClose} aria-label="Закрыть">×</button></header>
    <p>Быстрое управление пространством</p>
    <form onSubmit={submit}><span>›</span><input ref={input} autoComplete="off" spellCheck={false} value={value} placeholder="Введите команду…" onChange={event => setValue(event.target.value)}/><kbd>ENTER</kbd></form>
    <footer aria-live="polite">{message}</footer>
  </section></div>;
}
