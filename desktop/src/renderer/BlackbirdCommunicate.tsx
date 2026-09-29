import { useEffect, useRef, useState } from "react";
import type { AtlasOverlayServer } from "../shared/atlas-overlay";
import { sharedLinkInMessage } from "../shared/communicate-links";
import type { SharedLinkCard } from "../shared/communicate-links";
import "./blackbird-communicate.css";

interface SearchResult { user_id: number; nickname: string; static_id: string; server_code: string }
interface Conversation { partner_id: number; partner_name: string; last_message: string; created_at: string; from_me: boolean }
interface ChatMessage { id: number; sender_id: number; recipient_id: number; body: string; created_at: string }

const request = async (action: "communicate" | "communicate-update", data?: Record<string,string>) => {
  if (import.meta.env.DEV && new URLSearchParams(location.search).get("preview") === "1" && !window.blackbirdCommunicate && !window.tmodDesktop) {
    if (action === "communicate") return { preferences: { discoverable: true }, conversations: [{ partner_id: 2, partner_name: "Роберт", last_message: "Посмотри дело в Реакторе", created_at: new Date().toISOString(), from_me: false }] };
    if (data?.action === "thread") return { result: [
      { id: 1, sender_id: 2, recipient_id: 1, body: "Привет! Посмотри дело в Реакторе — https://reactor.tvr.lat/reactor/case/42", created_at: new Date().toISOString() },
      { id: 2, sender_id: 1, recipient_id: 2, body: "Открою. Вечером обсудим детали.", created_at: new Date().toISOString() },
    ] };
    if (data?.action === "search") return { result: { user_id: 2, nickname: "Роберт", static_id: data.static_id, server_code: data.server_code } };
    if (data?.action === "discoverability") return { result: { discoverable: data.discoverable === "true" } };
    return { result: { id: Date.now(), sender_id: 1, recipient_id: 2, body: data?.message || "", created_at: new Date().toISOString() } };
  }
  const result = window.blackbirdCommunicate ? await window.blackbirdCommunicate.request(action, data) : await window.tmodDesktop?.accountRequest?.(action, data);
  if (!result) throw new Error("communicate_unavailable");
  return result;
};
function SharedLinkCard({ card }: { card: SharedLinkCard }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    const ok = window.blackbirdCommunicate ? await window.blackbirdCommunicate.copyLink(card.url) : await navigator.clipboard.writeText(card.url).then(() => true).catch(() => false);
    if (ok) { setCopied(true); window.setTimeout(() => setCopied(false), 1800); }
  };
  const open = () => window.blackbirdCommunicate ? void window.blackbirdCommunicate.openLink(card.url) : window.open(card.url, "_blank", "noopener,noreferrer");
  return <div className={`bbc-link-card ${card.tone}`}><div className="bbc-link-emblem" aria-hidden="true">{card.tone === "senate" ? "✦" : card.tone === "atlas" ? "◌" : "↗"}</div><div className="bbc-link-body"><small>{card.contour}</small><strong>{card.title}</strong><span>{card.detail}</span></div><div className="bbc-link-actions"><button type="button" onClick={open}>Открыть ↗</button><button type="button" onClick={() => void copy()}>{copied ? "Скопировано" : "Копировать"}</button></div></div>;
}
function MessageContent({ body }: { body: string }) {
  const { card, text } = sharedLinkInMessage(body);
  return <>{text && <p>{text}</p>}{card && <SharedLinkCard card={card}/>}</>;
}

export function BlackbirdCommunicate({ servers, viewerId, onBack, sharedUrl = "", shareSequence = 0 }: { servers: AtlasOverlayServer[]; viewerId: number; onBack: () => void; sharedUrl?: string; shareSequence?: number }) {
  const [discoverable, setDiscoverable] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [server, setServer] = useState(servers[0]?.code || "phoenix-15");
  const [staticId, setStaticId] = useState("");
  const [found, setFound] = useState<SearchResult>();
  const [searchOpen, setSearchOpen] = useState(false);
  const [partner, setPartner] = useState<number>();
  const [partnerName, setPartnerName] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const refresh = async () => {
    const data = await request("communicate");
    setDiscoverable(Boolean((data.preferences as { discoverable?: boolean } | undefined)?.discoverable));
    setConversations((data.conversations || []) as Conversation[]);
    setReady(true);
  };
  useEffect(() => { let live = true; void refresh().catch(() => { if (live) { setReady(true); setError("Не удалось загрузить Communicate. Проверьте соединение."); } }); return () => { live = false; }; }, []);
  useEffect(() => { if (sharedUrl) setDraft(current => current ? `${current}\n${sharedUrl}` : sharedUrl); }, [sharedUrl, shareSequence]);
  useEffect(() => {
    if (!partner) { setMessages([]); return; }
    let live = true;
    const update = () => void request("communicate-update", { action: "thread", partner_id: String(partner) })
      .then(data => { if (live) setMessages((data.result || []) as ChatMessage[]); })
      .catch(() => { if (live) setError("Не удалось обновить беседу."); });
    update();
    const timer = window.setInterval(update, 8_000);
    return () => { live = false; window.clearInterval(timer); };
  }, [partner]);
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }); }, [messages.length, partner]);
  const run = async (task: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError("");
    try { await task(); } catch (cause) {
      const code = String(cause);
      setError(code.includes("communicate_recipient_unavailable") ? "Человек отключил сообщения или поиск." :
        code.includes("communicate_rate_limited") ? "Слишком много сообщений. Подождите минуту." :
        "Операция не завершена. Проверьте подключение и попробуйте ещё раз.");
    } finally { setBusy(false); }
  };
  const select = (id: number, name: string) => { setPartner(id); setPartnerName(name); setFound(undefined); setSearchOpen(false); };
  return <section className="bbc-page" aria-label="Blackbird Communicate"><div className="bbc-shell">
    <header className="bbc-windowbar"><div className="bbc-windowbrand"><span className="bbc-brand-glyph">✦</span><div><strong>Communicate</strong><small>BLACKBIRD · ПРЯМАЯ СВЯЗЬ</small></div></div><div className="bbc-window-controls"><button onClick={() => void window.blackbirdCommunicate?.minimize()} title="Свернуть">−</button><button onClick={onBack} title="Закрыть">×</button></div></header>
    <div className="bbc-layout"><aside className="bbc-directory"><div className="bbc-directory-head"><div><small>ВАШИ СООБЩЕНИЯ</small><strong>Диалоги</strong></div><button className={searchOpen ? "active" : ""} onClick={() => setSearchOpen(value => !value)} title="Найти человека">⌕</button></div>
      {searchOpen && <form className="bbc-search" onSubmit={e => { e.preventDefault(); void run(async () => { const data = await request("communicate-update",{action:"search",server_code:server,static_id:staticId}); setFound((data.result || undefined) as SearchResult | undefined); if (!data.result) setError("Персонаж не найден или человек не разрешил поиск."); }); }}><label>Сервер<input list="bbc-known-servers" value={server} maxLength={50} placeholder="phoenix-15" onChange={e => setServer(e.target.value)}/><datalist id="bbc-known-servers">{servers.map(item => <option value={item.code} key={item.code}>{item.name}</option>)}</datalist></label><label>Статик<input inputMode="numeric" maxLength={12} placeholder="263345" value={staticId} onChange={e => setStaticId(e.target.value)}/></label><button disabled={busy || !server.trim() || !/^\d{1,12}$/.test(staticId)}>Найти персонажа</button>{found && <button type="button" className="bbc-found" onClick={() => select(found.user_id, found.nickname)}><strong>{found.nickname}</strong><small>#{found.static_id} · {found.server_code}</small><span>Написать ↗</span></button>}</form>}
      <div className="bbc-conversations">{conversations.map(item => <button className={partner === item.partner_id ? "selected" : ""} key={item.partner_id} onClick={() => select(item.partner_id, item.partner_name || "Участник Blackbird")}><b>{(item.partner_name || "У").slice(0,1).toUpperCase()}</b><span><strong>{item.partner_name || "Участник Blackbird"}</strong><small>{item.from_me ? "Вы: " : ""}{item.last_message}</small></span><time>{new Date(item.created_at).toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit"})}</time></button>)}{ready && !conversations.length && <p className="bbc-list-empty">Диалогов пока нет.<br/>Поиск — вверху справа.</p>}</div>
      <div className="bbc-privacy"><label><input type="checkbox" checked={discoverable} disabled={busy} onChange={e => void run(async () => { const data = await request("communicate-update",{action:"discoverability",discoverable:String(e.target.checked)}); setDiscoverable(Boolean((data.result as {discoverable?:boolean})?.discoverable)); })}/><span>Меня можно найти по персонажу</span></label></div>
    </aside><div className="bbc-chat">{partner ? <><header><span className="bbc-avatar">{partnerName.slice(0,1).toUpperCase()}</span><div><strong>{partnerName}</strong><small>ЛИЧНЫЙ ДИАЛОГ</small></div><i/></header><div className="bbc-messages" aria-live="polite" ref={scrollRef}>{!messages.length && <div className="bbc-empty"><b>✦</b><strong>Начало разговора</strong><p>Сообщения и карточки контуров видны только вам и собеседнику.</p></div>}{messages.map(item => <article className={item.sender_id === viewerId ? "mine" : "theirs"} key={item.id}><MessageContent body={item.body}/><time>{new Date(item.created_at).toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit"})}</time></article>)}</div><form className="bbc-composer" onSubmit={e => { e.preventDefault(); const message = draft.trim(); if (!message) return; void run(async () => { const data = await request("communicate-update",{action:"send",partner_id:String(partner),message}); setMessages(current => [...current, data.result as unknown as ChatMessage]); setDraft(""); void refresh(); }); }}><textarea aria-label="Сообщение" value={draft} maxLength={1000} placeholder="Напишите сообщение или вставьте ссылку…" onChange={e => setDraft(e.target.value)} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}/><button disabled={busy || !draft.trim()} title="Отправить">➤</button></form><div className="bbc-composer-note">Enter — отправить · Shift+Enter — новая строка · ссылки на сервисы становятся карточками</div></> : <div className="bbc-empty"><b>✦</b><strong>Ваши люди — здесь.</strong><p>Выберите диалог или найдите персонажа по серверу и статику.</p>{sharedUrl && <small>Ссылка готова к отправке — выберите собеседника.</small>}</div>}</div></div>
    {error && <div className="bbc-error" role="alert">{error}<button onClick={() => setError("")}>×</button></div>}
  </div></section>;
}
