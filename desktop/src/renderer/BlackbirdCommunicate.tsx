import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AtlasOverlayServer } from "../shared/atlas-overlay";
import { conversationSnippet, sharedLinksInMessage } from "../shared/communicate-links";
import type { SharedLinkCard as LinkCard } from "../shared/communicate-links";
import { chatMessageLayout, isDeliveredChatMessage, isNearChatBottom, mergeDeliveredMessage, mergeThreadSnapshot, parseChatThread, unreadChatMessages } from "../shared/communicate-messages";
import { createDocumentPreviewLoader, readableDocumentPreview } from "../shared/communicate-preview";
import type { DocumentPreview } from "../shared/communicate-preview";
import { CommunicateComposers } from "../shared/communicate-composer";
import { attachmentSizeLabel, CHAT_FILE_LIMIT, CHAT_IMAGE_TYPES, createAttachmentQueue } from "../shared/communicate-attachments";
import "./blackbird-communicate.css";

interface SearchResult { user_id: string; nickname: string; static_id: string; server_code: string; login?: string }
interface Conversation { partner_id: string; partner_name: string; last_message: string; created_at: string; from_me: boolean; system?: boolean }
interface ChatAttachment { id: string; filename: string; mime_type: string; byte_size: number }
interface ChatMessage { id: number; sender_id: string; recipient_id: string; body: string; created_at: string; attachment?: ChatAttachment | null }
const loadAttachment = createAttachmentQueue(async (id: string) => {
  const result = await window.blackbirdCommunicate?.attachment(id);
  if (!result || !CHAT_IMAGE_TYPES.has(result.mimeType) || !result.bytes.byteLength || result.bytes.byteLength > CHAT_FILE_LIMIT) throw new Error("image_unavailable");
  return result;
});
const WELCOME = "Добро пожаловать в Blackbird! Здесь можно общаться с участниками Товарищества и делиться страницами сервисов. Найдите человека по персонажу и серверу или по точному логину T-Mod / Discord ID, чтобы начать личный диалог.";
const isBrowserPreview = () => import.meta.env.DEV && new URLSearchParams(location.search).get("preview") === "1" && !window.blackbirdCommunicate && !window.tmodDesktop;
const loadDocumentPreview = createDocumentPreviewLoader<DocumentPreview>(async url => {
  if (window.blackbirdCommunicate?.linkPreview) return window.blackbirdCommunicate.linkPreview(url);
  if (isBrowserPreview() && url === "https://consensus.tvr.lat/bills/42") return {
    title: "Транспортная доступность Phoenix", resource: "Инициатива №42", status: "На рассмотрении",
    detail: "Предлагается организовать регулярные поездки для новых участников и согласовать общий график. Демонстрационный материал для предпросмотра.",
  };
  return null;
});
const previewState = {
  discoverable: true,
  messages: [
    { id: 1, sender_id: "2", recipient_id: "1", body: "Привет! Посмотри дело в Реакторе — https://reactor.tvr.lat/reactor/case/42", created_at: new Date().toISOString() },
    { id: 2, sender_id: "1", recipient_id: "2", body: "Открою. Вечером обсудим детали.", created_at: new Date().toISOString() },
    ...(import.meta.env.DEV && new URLSearchParams(location.search).get("documents") === "1" ? [
      { id: 3, sender_id: "2", recipient_id: "1", body: "И ещё предложение к заседанию: https://consensus.tvr.lat/bills/42", created_at: new Date().toISOString() },
    ] : []),
    ...(import.meta.env.DEV && new URLSearchParams(location.search).get("history") === "1"
      ? Array.from({ length: 185 }, (_, index) => ({
        id: index + 4, sender_id: index % 2 ? "1" : "2", recipient_id: index % 2 ? "2" : "1",
        body: `Сообщение ${index + 1}. Проверяем длинную беседу и сохранение позиции при чтении истории.`,
        created_at: new Date(Date.now() - (185 - index) * 60_000).toISOString(),
      })) : []),
  ] as ChatMessage[],
};

async function request(action: "communicate" | "communicate-update", data?: Record<string, string>): Promise<Record<string, unknown>> {
  if (import.meta.env.DEV && new URLSearchParams(location.search).get("preview") === "1" && !window.blackbirdCommunicate && !window.tmodDesktop) {
    if (action === "communicate") return { viewer: { id: "1" }, preferences: { discoverable: previewState.discoverable }, conversations: [
      { partner_id: "0", partner_name: "Товарищество", last_message: WELCOME, created_at: "", from_me: false, system: true },
      { partner_id: "2", partner_name: "Роберт", last_message: previewState.messages.at(-1)?.body || "", created_at: previewState.messages.at(-1)?.created_at || "", from_me: previewState.messages.at(-1)?.sender_id === "1" },
    ] };
    if (data?.action === "thread" && data.partner_id === "0") return { result: [{ id: 0, sender_id: "0", recipient_id: "1", body: WELCOME, created_at: "" }] };
    if (data?.action === "thread") {
      if (new URLSearchParams(location.search).get("thread-offline") === "1") throw new Error("communicate_unavailable");
      const messages = previewState.messages.filter(item => (!data.before_id || item.id < Number(data.before_id)) && (!data.after_id || item.id > Number(data.after_id)));
      return data.after_id ? { result: messages.slice(0, 80), has_newer: messages.length > 80 }
        : { result: messages.slice(-80), has_more: messages.length > 80, has_newer: false };
    }
    if (data?.action === "search") return { result: { user_id: "2", nickname: "Роберт", static_id: data.static_id || "", server_code: data.server_code || "", login: data.account_query || "robert" } };
    if (data?.action === "discoverability") {
      previewState.discoverable = data.discoverable === "true";
      return { result: { discoverable: previewState.discoverable } };
    }
    const sent = { id: Date.now(), sender_id: "1", recipient_id: "2", body: data?.message || "", created_at: new Date().toISOString() };
    const delay = Math.min(3000, Math.max(0, Number(new URLSearchParams(location.search).get("send-delay")) || 0));
    if (delay) await new Promise(resolve => window.setTimeout(resolve, delay));
    previewState.messages.push(sent);
    return { result: sent };
  }
  const result = window.blackbirdCommunicate
    ? await window.blackbirdCommunicate.request(action, data)
    : await window.tmodDesktop?.accountRequest?.(action, data);
  if (!result) throw new Error("communicate_unavailable");
  return result;
}

function ChatGlyph({ kind = "chat" }: { kind?: "chat" | "search" | "link" | "copy" | "arrow" | "case" | "atlas" | "check" | "send" | "file" | "image" | "plus" }) {
  const paths = {
    chat: <><path d="M7 18.3 3.5 21v-6.5A7.5 7.5 0 0 1 3 12c0-5 3.8-8.5 9-8.5s9 3.5 9 8.5-3.8 8.5-9 8.5a10.6 10.6 0 0 1-5-1.2Z"/><path d="M8 11h8m-8 4h5"/></>,
    search: <><circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/></>,
    link: <><path d="m9 15 6-6m-7 8-1 1a4.2 4.2 0 0 1-6-6l4-4a4.2 4.2 0 0 1 6 0m2 8a4.2 4.2 0 0 0 6 0l4-4a4.2 4.2 0 0 0-6-6l-1 1" transform="translate(1 0) scale(.92)"/></>,
    copy: <><rect x="8" y="8" width="12" height="13" rx="3"/><path d="M15 5V4a1 1 0 0 0-1-1H5a2 2 0 0 0-2 2v9a1 1 0 0 0 1 1h1"/></>,
    arrow: <><path d="M5 12h14m-5-5 5 5-5 5"/></>,
    case: <><rect x="4" y="6" width="16" height="15" rx="3"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M8 11h8m-8 5h5"/></>,
    atlas: <><circle cx="12" cy="12" r="8"/><ellipse cx="12" cy="12" rx="4" ry="8" transform="rotate(40 12 12)"/><path d="m12 9 1 2 2 1-2 1-1 2-1-2-2-1 2-1Z"/></>,
    check: <path d="m5 12 4 4L19 6"/>,
    send: <><path d="m3.5 4 17 8-17 8 3-8-3-8Z"/><path d="M6.5 12h14"/></>,
    file: <><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6M8 13h8m-8 4h5"/></>,
    image: <><rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="8" cy="8" r="1.5"/><path d="m4 17 5-5 4 4 3-3 5 5"/></>,
    plus: <path d="M12 5v14M5 12h14"/>,
  };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[kind]}</svg>;
}

function VerifiedMark() {
  return <span className="bbc-verified" title="Официальный аккаунт Товарищества" role="img" aria-label="Проверенный аккаунт">
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path className="bbc-verified-seal" d="M12 2.7 15 4l3.2.8 1.1 3.1L21 11l-1.3 3.2-.8 3.2-3.1 1.1-3.1 1.7-3.2-1.3-3.2-.8-1.1-3.1L3.5 12l1.3-3.2.8-3.2 3.1-1.1Z"/><path className="bbc-verified-orbit" d="M3.3 8.6A9.4 9.4 0 0 1 15.4 3.3M20.7 15.4a9.4 9.4 0 0 1-12.1 5.3"/><circle className="bbc-verified-satellite" cx="19.2" cy="5.1" r="1.2"/><path className="bbc-verified-tick" d="m8.1 11.9 2.6 2.6 5.4-5.1" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"/></svg>
  </span>;
}

function SharedLinkCard({ card }: { card: LinkCard }) {
  const [documentPreview, setDocumentPreview] = useState<DocumentPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const cardElement = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);
  const [actionError, setActionError] = useState("");
  const [opening, setOpening] = useState(false);
  const openingRef = useRef(false);
  const copyTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(copyTimer.current), []);
  useEffect(() => {
    setDocumentPreview(null);
    setPreviewLoading(false);
    if (!card.previewable || (!window.blackbirdCommunicate?.linkPreview && !isBrowserPreview()) || !cardElement.current) return;
    let live = true;
    const observer = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting)) return;
      observer.disconnect();
      setPreviewLoading(true);
      void loadDocumentPreview(card.url).then(result => { if (live) { setDocumentPreview(readableDocumentPreview(result)); setPreviewLoading(false); } });
    }, { root: cardElement.current.closest(".bbc-messages"), rootMargin: "80px" });
    observer.observe(cardElement.current);
    return () => { live = false; observer.disconnect(); };
  }, [card.url, card.previewable]);
  const copy = async () => {
    setActionError("");
    try {
      const ok = window.blackbirdCommunicate ? await window.blackbirdCommunicate.copyLink(card.url)
        : await navigator.clipboard.writeText(card.url).then(() => true);
      if (!ok) throw new Error("copy_unavailable");
      setCopied(true);
      window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 1800);
    } catch { setActionError("Не удалось скопировать ссылку. Попробуйте ещё раз."); }
  };
  const open = async () => {
    if (openingRef.current) return;
    openingRef.current = true; setOpening(true); setActionError("");
    try {
      if (window.blackbirdCommunicate) {
        if (!await window.blackbirdCommunicate.openLink(card.url)) throw new Error("link_unavailable");
      } else if (window.tmodDesktop?.openSharedLink) {
        if (!await window.tmodDesktop.openSharedLink(card.url)) throw new Error("link_unavailable");
      } else window.open(card.url, "_blank", "noopener,noreferrer");
    } catch { setActionError("Страница не открылась. Проверьте подключение и доступ к сервису."); }
    finally { openingRef.current = false; setOpening(false); }
  };
  return <div ref={cardElement} className={`bbc-link-card ${card.tone} ${previewLoading ? "loading-preview" : ""}`}>
    <div className="bbc-link-hero"><span className="bbc-link-emblem"><ChatGlyph kind={card.tone === "senate" ? "case" : card.tone === "atlas" ? "atlas" : "link"}/></span><div><small>{card.contour}</small><span>{documentPreview?.resource || card.resource || (card.tone === "external" ? "Внешний сайт" : "Ссылка на сервис")}</span></div>{documentPreview?.status && <span className="bbc-link-status">{documentPreview.status}</span>}</div>
    <div className={`bbc-link-main ${documentPreview ? "has-document" : ""}`}><button className="bbc-link-title" type="button" disabled={opening} onClick={() => void open()}>{documentPreview?.title || card.title}</button><p>{documentPreview?.detail || card.detail}</p></div>
    <div className="bbc-link-actions"><button type="button" disabled={opening} onClick={() => void open()}>{opening ? "Открываем…" : card.tone === "external" ? "Открыть сайт" : "Открыть в Blackbird"}<ChatGlyph kind="arrow"/></button><button className={copied ? "copied" : ""} type="button" title={copied ? "Скопировано" : "Копировать ссылку"} aria-label={copied ? "Скопировано" : "Копировать ссылку"} onClick={() => void copy()}><ChatGlyph kind={copied ? "check" : "copy"}/></button><small aria-live="polite">{previewLoading ? "Загружаем содержание…" : new URL(card.url).hostname}</small></div>
    {actionError && <div className="bbc-link-error" role="alert">{actionError}</div>}
  </div>;
}

function MessageContent({ body }: { body: string }) {
  const { cards, text } = sharedLinksInMessage(body);
  return <>{text && <p>{text}</p>}{cards.map((card, index) => <SharedLinkCard card={card} key={`${card.url}:${index}`}/>)}</>;
}

function AttachmentCard({ attachment }: { attachment: ChatAttachment }) {
  const [preview, setPreview] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const element = useRef<HTMLDivElement>(null);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const imageButton = useRef<HTMLButtonElement>(null);
  const imageDialog = useRef<HTMLDivElement>(null);
  const isImage = CHAT_IMAGE_TYPES.has(attachment.mime_type);
  useEffect(() => {
    if (!expanded) return;
    const dialog = imageDialog.current;
    dialog?.querySelector<HTMLButtonElement>("button")?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setExpanded(false); imageButton.current?.focus(); }
      if (event.key !== "Tab" || !dialog) return;
      const controls = [...dialog.querySelectorAll<HTMLButtonElement>("button:not(:disabled)")];
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, [expanded]);
  useEffect(() => {
    if (!isImage || !window.blackbirdCommunicate || !element.current) return;
    let live = true;
    let objectUrl = "";
    let pending: ReturnType<typeof loadAttachment> | undefined;
    setPreview(""); setError(""); setPreviewLoading(false);
    const observer = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting)) return;
      observer.disconnect();
      setPreviewLoading(true);
      pending = loadAttachment(attachment.id);
      void pending.promise.then(result => {
        if (!live) return;
        setPreviewLoading(false);
        if (!result) { setError("Не удалось открыть изображение"); return; }
        objectUrl = URL.createObjectURL(new Blob([result.bytes as BlobPart], { type: result.mimeType }));
        setPreview(objectUrl);
      });
    }, { root: element.current.closest(".bbc-messages"), rootMargin: "80px" });
    observer.observe(element.current);
    return () => { live = false; observer.disconnect(); pending?.cancel(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [attachment.id, isImage, previewAttempt]);
  const download = async () => {
    if (!window.blackbirdCommunicate || loading) return;
    setLoading(true); setError("");
    try {
      const result = await window.blackbirdCommunicate.attachment(attachment.id);
      const url = URL.createObjectURL(new Blob([result.bytes as BlobPart], { type: result.mimeType }));
      const link = document.createElement("a");
      link.href = url;
      link.download = result.filename;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch { setError("Не удалось скачать файл"); }
    finally { setLoading(false); }
  };
  return <div ref={element} className={`bbc-attachment ${isImage ? "image" : "file"}`}>
    {isImage && (preview ? <button ref={imageButton} type="button" className="bbc-photo-open" aria-label={`Посмотреть изображение ${attachment.filename}`} onClick={() => setExpanded(true)}><img src={preview} alt={attachment.filename} loading="lazy" onError={() => { setPreview(""); setExpanded(false); setError("Изображение повреждено или не поддерживается"); }}/></button>
      : <div className={`bbc-image-placeholder ${previewLoading ? "loading" : ""}`} role="status"><ChatGlyph kind="image"/><span>{previewLoading ? "Открываем фото…" : "Изображение"}</span></div>)}
    <div className="bbc-attachment-info"><span aria-hidden="true"><ChatGlyph kind={isImage ? "image" : "file"}/></span><div><strong>{attachment.filename}</strong><small>{attachmentSizeLabel(attachment.byte_size)} · {isImage ? "Изображение" : "Файл"}</small></div></div>
    <button type="button" disabled={loading} onClick={() => void download()}>{loading ? "Загрузка…" : "Скачать"}</button>
    {error && <small role="alert" className="bbc-attachment-error">{error} {isImage && !preview && <button type="button" onClick={() => setPreviewAttempt(value => value + 1)}>Повторить</button>}</small>}
    {expanded && preview && createPortal(<div className="bbc-photo-backdrop" onClick={event => { if (event.target === event.currentTarget) { setExpanded(false); imageButton.current?.focus(); } }}>
      <div ref={imageDialog} className="bbc-photo-dialog" role="dialog" aria-modal="true" aria-label="Просмотр изображения">
        <header><div><strong>{attachment.filename}</strong><small>Изображение из вашей беседы</small></div><button type="button" aria-label="Закрыть изображение" onClick={() => { setExpanded(false); imageButton.current?.focus(); }}>×</button></header>
        <img src={preview} alt={attachment.filename}/><footer><span>Esc — закрыть</span><button type="button" disabled={loading} onClick={() => void download()}>{loading ? "Скачиваем…" : "Скачать оригинал"}</button></footer>
      </div>
    </div>, document.body)}
  </div>;
}

function PendingFile({ file, onRemove }: { file: File; onRemove: () => void }) {
  const [preview, setPreview] = useState("");
  useEffect(() => {
    if (!CHAT_IMAGE_TYPES.has(file.type)) { setPreview(""); return; }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  return <div className="bbc-pending-file">{preview ? <img src={preview} alt="Предпросмотр выбранного изображения"/> : <span><ChatGlyph kind="file"/></span>}<div><strong>{file.name}</strong><small>{attachmentSizeLabel(file.size)} · будет отправлен вместе с сообщением</small></div><button type="button" onClick={onRemove} aria-label="Убрать файл">×</button></div>;
}

export function BlackbirdCommunicate({ servers, onBack, sharedUrl = "", shareSequence = 0 }: {
  servers: AtlasOverlayServer[]; onBack: () => void; sharedUrl?: string; shareSequence?: number;
}) {
  const [viewerId, setViewerId] = useState("0");
  const viewerRef = useRef("0");
  const [discoverable, setDiscoverable] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [server, setServer] = useState(servers[0]?.code || "phoenix-15");
  const [staticId, setStaticId] = useState("");
  const [accountQuery, setAccountQuery] = useState("");
  const [found, setFound] = useState<SearchResult>();
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchMode, setSearchMode] = useState<"account" | "character">("account");
  const searchRef = useRef<HTMLDivElement>(null);
  const searchButtonRef = useRef<HTMLButtonElement>(null);
  const searchRevision = useRef(0);
  const [partner, setPartner] = useState<string>();
  const partnerRef = useRef<string | undefined>(undefined);
  const conversationRevision = useRef(0);
  const [partnerName, setPartnerName] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [threadLoading, setThreadLoading] = useState(false);
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const olderRequestInFlight = useRef(false);
  const [draft, setDraft] = useState("");
  const composers = useRef(new CommunicateComposers<File>(() => crypto.randomUUID().replaceAll("-", "")));
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [directoryError, setDirectoryError] = useState("");
  const [threadError, setThreadError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [ready, setReady] = useState(false);
  const connectionError = threadError || directoryError;
  const connected = ready && !connectionError;
  const reconnectDirectory = useRef<(() => void) | undefined>(undefined);
  const reconnectThread = useRef<(() => void) | undefined>(undefined);
  const [closing, setClosing] = useState(false);
  const mounted = useRef(false);
  const refreshRevision = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const followMessages = useRef(true);
  const lastMessageId = useRef(0);
  const historyAnchor = useRef<{ height: number; top: number } | null>(null);
  const [readThrough, setReadThrough] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = composerRef.current;
    if (!element) return;
    element.style.height = "0px";
    element.style.height = `${Math.min(126, Math.max(38, element.scrollHeight))}px`;
  }, [draft, partner]);
  useEffect(() => {
    if (!searchOpen) return;
    searchRef.current?.querySelector<HTMLInputElement>("input")?.focus();
    const dismiss = (event: PointerEvent) => {
      if (!searchRef.current?.contains(event.target as Node) && !searchButtonRef.current?.contains(event.target as Node)) { searchRevision.current++; setSearchOpen(false); }
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { searchRevision.current++; setSearchOpen(false); searchButtonRef.current?.focus(); } };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", escape); };
  }, [searchOpen, searchMode]);
  const updateComposer = (patch: { text?: string; file?: File | null }) => {
    const next = composers.current.update(partnerRef.current || "", patch);
    setDraft(next.text); setPendingFile(next.file);
  };
  const refresh = useCallback(async () => {
    const revision = ++refreshRevision.current;
    let data: Record<string, unknown>;
    try { data = await request("communicate"); }
    catch (cause) { if (mounted.current && revision === refreshRevision.current) throw cause; return; }
    if (!mounted.current || revision !== refreshRevision.current) return;
    const next = (data.conversations || []) as Conversation[];
    const viewer = data.viewer as { id?: string } | undefined;
    if (!viewer?.id || !/^[1-9]\d*$/.test(viewer.id) || !Array.isArray(data.conversations)) throw new Error("communicate_directory_invalid");
    viewerRef.current = viewer.id;
    setViewerId(viewer.id);
    setDiscoverable(Boolean((data.preferences as { discoverable?: boolean } | undefined)?.discoverable));
    setConversations(next);
    setDirectoryError("");
    setReady(true);
  }, []);
  useEffect(() => {
    if (!closing) return;
    const timer = window.setTimeout(onBack, 180);
    return () => window.clearTimeout(timer);
  }, [closing, onBack]);
  useEffect(() => {
    mounted.current = true;
    let live = true;
    let inFlight = false;
    const update = async () => {
      if (inFlight || !live) return;
      inFlight = true;
      try { await refresh(); }
      catch { if (live) { setReady(true); setDirectoryError("Восстанавливаем связь с Communicate. Попробуем снова автоматически."); } }
      finally { inFlight = false; }
    };
    void update();
    const timer = window.setInterval(() => void update(), 15_000);
    const reconnect = () => void update();
    reconnectDirectory.current = reconnect;
    window.addEventListener("focus", reconnect);
    window.addEventListener("online", reconnect);
    return () => { live = false; mounted.current = false; reconnectDirectory.current = undefined; refreshRevision.current += 1; window.clearInterval(timer); window.removeEventListener("focus", reconnect); window.removeEventListener("online", reconnect); };
  }, [refresh]);
  useEffect(() => {
    if (!sharedUrl) return;
    const recipient = partnerRef.current && partnerRef.current !== "0" ? partnerRef.current : "";
    const current = composers.current.read(recipient);
    const next = composers.current.update(recipient, { text: current.text ? `${current.text}\n${sharedUrl}` : sharedUrl });
    if (recipient === (partnerRef.current || "")) setDraft(next.text);
  }, [sharedUrl, shareSequence]);
  useEffect(() => {
    if (partner === undefined) { setMessages([]); return; }
    let live = true;
    let inFlight = false;
    const revision = conversationRevision.current;
    let initialized = false;
    let cursor = 0;
    const update = async () => {
      if (inFlight || !live) return;
      inFlight = true;
      try {
        // Forward pages are oldest-first: a busy conversation can accumulate
        // more than 80 messages while offline without leaving a silent gap.
        // Bound each refresh so neither a huge backlog nor a chat switch
        // starts an unbounded request chain.
        for (let page = 0; page < 4; page++) {
          const data = await request("communicate-update", { action: "thread", partner_id: String(partner), ...(cursor ? { after_id: String(cursor) } : {}) });
          if (!live || revision !== conversationRevision.current) return;
          const received = parseChatThread<ChatMessage>(data.result, viewerRef.current, partner);
          if (cursor && typeof data.has_newer !== "boolean") throw new Error("communicate_sync_version_unavailable");
          setMessages(current => mergeThreadSnapshot(current, received));
          if (!initialized) { setHasOlder(data.has_more === true); initialized = true; }
          setThreadLoading(false);
          setThreadError("");
          const nextCursor = received.at(-1)?.id || cursor;
          if (data.has_newer && nextCursor <= cursor) throw new Error("communicate_cursor_not_advancing");
          cursor = Math.max(cursor, nextCursor);
          if (!data.has_newer) break;
        }
      } catch { if (live && revision === conversationRevision.current) { setThreadLoading(false); setThreadError("Не удалось обновить беседу. Восстанавливаем связь…"); } }
      finally { inFlight = false; }
    };
    void update();
    const timer = partner === "0" ? undefined : window.setInterval(() => void update(), 8_000);
    const reconnect = () => void update();
    reconnectThread.current = reconnect;
    window.addEventListener("focus", reconnect);
    window.addEventListener("online", reconnect);
    return () => { live = false; reconnectThread.current = undefined; if (timer) window.clearInterval(timer); window.removeEventListener("focus", reconnect); window.removeEventListener("online", reconnect); };
  }, [partner]);
  const scrollToLatest = useCallback(() => {
    followMessages.current = true;
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
    setReadThrough(lastMessageId.current);
  }, []);
  useLayoutEffect(() => {
    lastMessageId.current = messages.at(-1)?.id || 0;
    const anchor = historyAnchor.current;
    if (anchor && scrollRef.current) {
      scrollRef.current.scrollTop = anchor.top + scrollRef.current.scrollHeight - anchor.height;
      historyAnchor.current = null;
    } else if (followMessages.current) scrollToLatest();
  }, [messages, partner, scrollToLatest]);
  useEffect(() => {
    if (!scrollRef.current || !messageListRef.current) return;
    // Images decode after the message arrives; keep the bottom anchored only
    // while the reader is following the conversation, never while reading up.
    const observer = new ResizeObserver(() => { if (followMessages.current) scrollToLatest(); });
    observer.observe(scrollRef.current);
    observer.observe(messageListRef.current);
    return () => observer.disconnect();
  }, [partner, scrollToLatest]);
  const run = async (task: () => Promise<void>, stillRelevant: () => boolean = () => true) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true); setError("");
    try { await task(); } catch (cause) {
      if (!stillRelevant()) return;
      const code = String(cause);
      setError(code.includes("communicate_recipient_unavailable") ? "Аккаунт собеседника больше недоступен." :
        code.includes("communicate_rate_limited") ? "Слишком много сообщений. Подождите минуту." :
          "Операция не завершена. Проверьте подключение и попробуйте ещё раз.");
    } finally { busyRef.current = false; setBusy(false); }
  };
  const select = (id: string, name: string) => {
    searchRevision.current++;
    // Restore only this recipient's draft and file, never another person's.
    if (id !== partnerRef.current) {
      conversationRevision.current += 1;
      setMessages([]);
      setThreadLoading(true);
      setHasOlder(false);
      setLoadingOlder(false);
      olderRequestInFlight.current = false;
      historyAnchor.current = null;
      const next = composers.current.select(id);
      setDraft(next.text); setPendingFile(next.file);
      setError("");
      setThreadError("");
      followMessages.current = true;
      setReadThrough(0);
    }
    partnerRef.current = id;
    setPartner(id); setPartnerName(name); setFound(undefined); setSearchOpen(false);
  };
  const queueFile = (file: File) => {
    if (file.size <= 0 || file.size > CHAT_FILE_LIMIT) { setError("Файл должен быть не больше 8 МБ."); return; }
    updateComposer({ file });
    setError("");
  };
  const loadOlderMessages = async () => {
    const oldestId = messages[0]?.id;
    if (!partner || partner === "0" || !oldestId || !hasOlder || olderRequestInFlight.current) return;
    const revision = conversationRevision.current;
    olderRequestInFlight.current = true;
    setLoadingOlder(true);
    try {
      const data = await request("communicate-update", { action: "thread", partner_id: partner, before_id: String(oldestId) });
      if (!mounted.current || revision !== conversationRevision.current) return;
      const received = parseChatThread<ChatMessage>(data.result, viewerRef.current, partner);
      const element = scrollRef.current;
      if (element) historyAnchor.current = { height: element.scrollHeight, top: element.scrollTop };
      followMessages.current = false;
      setMessages(current => mergeThreadSnapshot(current, received));
      setHasOlder(data.has_more === true);
    } catch {
      if (mounted.current && revision === conversationRevision.current) setError("Не удалось загрузить историю. Попробуйте ещё раз — сообщения сохранены.");
    } finally {
      if (mounted.current && revision === conversationRevision.current) { olderRequestInFlight.current = false; setLoadingOlder(false); }
    }
  };
  const sendCurrentMessage = () => {
    if (busyRef.current) return;
    if (!partner || partner === "0") return;
    const recipient = partner;
    const current = composers.current.read(recipient);
    const message = current.text.trim();
    if (!message && !current.file) return;
    const sending = composers.current.capture(recipient);
    const sendingFile = sending.file;
    const clientNonce = sending.nonce;
    const stillRelevant = () => mounted.current && partnerRef.current === recipient;
    void run(async () => {
      let data: Record<string, unknown>;
      if (sendingFile) {
        if (!window.blackbirdCommunicate) throw new Error("communicate_attachment_unavailable");
        data = await window.blackbirdCommunicate.upload({
          partnerId: recipient, filename: sendingFile.name, caption: message,
          bytes: new Uint8Array(await sendingFile.arrayBuffer()),
          clientNonce,
        });
      } else {
        data = await request("communicate-update", { action: "send", partner_id: recipient, message, client_nonce: clientNonce });
      }
      if (!isDeliveredChatMessage(data.result, recipient)) throw new Error("communicate_response_invalid");
      const nextDraft = composers.current.acknowledge(sending);
      if (stillRelevant()) {
        followMessages.current = true;
        setMessages(current => mergeDeliveredMessage(current, data.result as ChatMessage));
        setDraft(nextDraft.text);
        setPendingFile(nextDraft.file);
      }
      void refresh().catch(() => { if (mounted.current) setDirectoryError("Сообщение отправлено. Обновляем список диалогов…"); });
    }, stillRelevant);
  };
  const service = partner === "0";
  const unreadCount = unreadChatMessages(messages, readThrough, viewerId);
  const displayName = conversations.find(item => item.partner_id === partner)?.partner_name || partnerName;
  const verified = <VerifiedMark/>;
  const messageLayout = chatMessageLayout(messages);
  return <section className={`bbc-page ${closing ? "is-closing" : ""}`} aria-label="Blackbird Communicate"><div className="bbc-shell">
    <header className="bbc-windowbar"><div className="bbc-windowbrand"><span className="bbc-brand-glyph"><ChatGlyph/></span><div><strong>Communicate</strong><small>Blackbird</small></div></div><span className={`bbc-window-presence ${connected ? "" : "offline"}`} role="status"><i/> {connected ? "На связи" : ready ? "Восстанавливаем связь" : "Подключаемся"}</span><div className="bbc-window-controls"><button onClick={() => void window.blackbirdCommunicate?.minimize()} title="Свернуть" aria-label="Свернуть">−</button><button onClick={() => setClosing(true)} title="Закрыть" aria-label="Закрыть">×</button></div></header>
    <div className="bbc-layout"><aside className="bbc-directory"><div className="bbc-directory-head"><div><small>Личное пространство</small><strong>Сообщения</strong></div><button ref={searchButtonRef} className={searchOpen ? "active" : ""} onClick={() => { searchRevision.current++; setSearchOpen(value => !value); }} title="Найти человека" aria-label="Найти человека" aria-expanded={searchOpen} aria-controls="bbc-person-search"><ChatGlyph kind="search"/></button></div>
      {searchOpen && <div ref={searchRef} className="bbc-search" id="bbc-person-search" role="region" aria-label="Поиск собеседника">
        <div className="bbc-search-modes" role="group" aria-label="Способ поиска"><button type="button" aria-pressed={searchMode === "account"} onClick={() => { searchRevision.current++; setSearchMode("account"); setFound(undefined); }}>Аккаунт</button><button type="button" aria-pressed={searchMode === "character"} onClick={() => { searchRevision.current++; setSearchMode("character"); setFound(undefined); }}>Персонаж</button></div>
        <form key={searchMode} onSubmit={e => {
          e.preventDefault();
          const mode = searchMode;
          const ticket = ++searchRevision.current;
          const stillRelevant = () => mounted.current && ticket === searchRevision.current;
          void run(async () => {
            setFound(undefined);
            const data = await request("communicate-update", mode === "account" ? { action: "search", account_query: accountQuery.trim() } : { action: "search", server_code: server, static_id: staticId });
            if (!stillRelevant()) return;
            setFound((data.result || undefined) as SearchResult | undefined);
            if (!data.result) setError(mode === "account" ? "Аккаунт не найден. Проверьте точный логин или Discord ID." : "Персонаж не найден. Проверьте сервер и привязку персонажа к Atlas.");
          }, stillRelevant);
        }}>
          {searchMode === "account" ? <label>T-Mod логин или Discord ID<input value={accountQuery} maxLength={32} placeholder="Логин или точный ID" onChange={e => { searchRevision.current++; setFound(undefined); setAccountQuery(e.target.value); }}/></label> : <>
            <label>Сервер<input list="bbc-known-servers" value={server} maxLength={50} placeholder="phoenix-15" onChange={e => { searchRevision.current++; setFound(undefined); setServer(e.target.value); }}/><datalist id="bbc-known-servers">{servers.map(item => <option value={item.code} key={item.code}>{item.name}</option>)}</datalist></label>
            <label>Статик<input inputMode="numeric" maxLength={12} placeholder="263345" value={staticId} onChange={e => { searchRevision.current++; setFound(undefined); setStaticId(e.target.value); }}/></label>
          </>}
          <button disabled={busy || (searchMode === "account" ? !accountQuery.trim() : !server.trim() || !/^\d{1,12}$/.test(staticId))}>{busy ? "Ищем…" : "Найти собеседника"}</button>
        </form>
        {found ? <button type="button" className="bbc-found" onClick={() => select(found.user_id, found.nickname)}><strong>{found.nickname}</strong><small>{found.static_id ? `#${found.static_id} · ${found.server_code}` : `@${found.login || found.user_id}`}</small><span>Написать ↗</span></button> : <small className="bbc-search-note">{searchMode === "account" ? "По точному логину можно написать любому участнику Blackbird." : "Поиск среди персонажей, которые разрешили себя находить."}</small>}
      </div>}
      <div className="bbc-conversations">{conversations.map(item => <button className={`${partner === item.partner_id ? "selected" : ""} ${item.system ? "system" : ""}`} key={item.partner_id} onClick={() => select(item.partner_id, item.partner_name || "Участник Blackbird")}><b>{item.system ? "✦" : (item.partner_name || "У").slice(0, 1).toUpperCase()}</b><span><strong>{item.partner_name || "Участник Blackbird"}{item.system && verified}</strong><small>{item.from_me ? "Вы: " : ""}{conversationSnippet(item.last_message)}</small></span>{item.created_at && <time>{new Date(item.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}</time>}</button>)}{ready && !conversations.length && <p className="bbc-list-empty">Диалогов пока нет.<br/>Поиск — вверху справа.</p>}</div>
      <div className="bbc-privacy"><label title="Скрывает поиск по персонажу; по точному логину или Discord ID вам всё равно можно написать"><input type="checkbox" checked={discoverable} disabled={busy} onChange={e => {
        const value = e.target.checked;
        void run(async () => {
          const data = await request("communicate-update", { action: "discoverability", discoverable: String(value) });
          refreshRevision.current += 1;
          setDiscoverable(Boolean((data.result as { discoverable?: boolean })?.discoverable));
        });
      }}/><span>Показывать персонажей в поиске</span></label></div>
    </aside><div className="bbc-chat" key={partner || "empty"}>{partner !== undefined ? <><header><span className={`bbc-avatar ${service ? "official" : ""}`}>{service ? "✦" : displayName.slice(0, 1).toUpperCase()}</span><div><strong>{displayName}{service && verified}</strong><small>{service ? "ОФИЦИАЛЬНЫЙ КАНАЛ · ТОЛЬКО ОБЪЯВЛЕНИЯ" : "ЛИЧНЫЙ ДИАЛОГ"}</small></div><i/></header>
      <div className="bbc-thread-viewport">
        <div className="bbc-messages" aria-live="polite" ref={scrollRef} onScroll={event => {
          followMessages.current = isNearChatBottom(event.currentTarget);
          if (followMessages.current) setReadThrough(lastMessageId.current);
        }}><div className="bbc-message-list" ref={messageListRef}>
          {hasOlder && <button type="button" className="bbc-load-history" disabled={loadingOlder} onClick={() => void loadOlderMessages()}>{loadingOlder ? "Загружаем историю…" : "Предыдущие сообщения ↑"}</button>}
          {!messages.length && <div className={`bbc-empty ${threadLoading ? "loading" : ""}`} role={threadLoading ? "status" : undefined}><b>✦</b><strong>{threadLoading ? "Открываем беседу" : threadError ? "Беседа временно недоступна" : "Начало разговора"}</strong><p>{threadLoading ? "Загружаем сообщения и вложения…" : threadError ? "Повторите подключение, чтобы загрузить сообщения. Черновик можно продолжить здесь." : "Сообщения и карточки контуров видны только вам и собеседнику."}</p></div>}
          {messages.map((item, index) => <Fragment key={item.id}>{messageLayout[index].dayLabel && <div className="bbc-day-divider"><span>{messageLayout[index].dayLabel}</span></div>}<article className={`${item.sender_id === viewerId ? "mine" : "theirs"} ${messageLayout[index].grouped ? "grouped" : ""} ${service ? "official" : ""} ${sharedLinksInMessage(item.body).cards.length ? "with-link" : ""}`}>{service && <small className="bbc-message-author">Товарищество {verified}</small>}<MessageContent body={item.body}/>{item.attachment && <AttachmentCard attachment={item.attachment}/>} {item.created_at && <time>{new Date(item.created_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}{item.sender_id === viewerId && <span className="bbc-delivered" role="img" aria-label="Отправлено" title="Сообщение сохранено на сервере"><ChatGlyph kind="check"/></span>}</time>}</article></Fragment>)}
        </div></div>
        {unreadCount > 0 && <button type="button" className="bbc-new-messages" onClick={scrollToLatest}>Новые сообщения · {unreadCount} <span aria-hidden="true">↓</span></button>}
      </div>
      {service ? <div className="bbc-service-foot">{verified}<span>Проверенный служебный канал. Ответы здесь отключены; для общения выберите участника.</span></div> : <>
        <input ref={fileRef} className="bbc-file-input" type="file" onChange={event => { const file = event.currentTarget.files?.[0]; if (file) queueFile(file); event.currentTarget.value = ""; }}/>
        {pendingFile && <PendingFile key={`${partner}:${pendingFile.name}:${pendingFile.lastModified}`} file={pendingFile} onRemove={() => updateComposer({ file: null })}/>}
        <form className="bbc-composer" onSubmit={e => { e.preventDefault(); sendCurrentMessage(); }}>
          <button type="button" className="bbc-attach-button" onClick={() => fileRef.current?.click()} title="Прикрепить файл" aria-label="Прикрепить файл"><ChatGlyph kind="plus"/></button>
          <textarea ref={composerRef} aria-label="Сообщение" value={draft} maxLength={1000} placeholder="Напишите сообщение…" onChange={e => updateComposer({ text: e.target.value })}
            onPaste={event => { const item = Array.from(event.clipboardData.items).find(entry => entry.kind === "file" && entry.type.startsWith("image/")); const file = item?.getAsFile(); if (file) { event.preventDefault(); queueFile(file); } }}
            onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}/>
          <button disabled={busy || (!draft.trim() && !pendingFile)} title={busy ? "Отправляем…" : "Отправить"} aria-label={busy ? "Отправляем сообщение" : "Отправить"}>{busy ? <span className="bbc-send-progress"/> : <ChatGlyph kind="send"/>}</button>
        </form>
        <div className="bbc-composer-note"><span>Shift + Enter — новая строка</span><span>Файлы: + · Изображения: Ctrl + V</span></div>
      </>}</> : <div className="bbc-empty"><b>✦</b><strong>Ваши люди — здесь.</strong><p>Выберите диалог или найдите человека по T-Mod логину, Discord ID либо персонажу.</p><button type="button" className="bbc-start-chat" onClick={() => setSearchOpen(true)}>Начать диалог ↗</button>{sharedUrl && <small>Ссылка готова к отправке — выберите собеседника.</small>}</div>}</div></div>
    {(error || connectionError) && <div className="bbc-error" role="alert">{error || connectionError}<button type="button" aria-label={error ? "Закрыть сообщение об ошибке" : "Повторить подключение"} onClick={() => {
      if (error) setError("");
      else { reconnectDirectory.current?.(); reconnectThread.current?.(); }
    }}>{error ? "×" : "Повторить"}</button></div>}
  </div></section>;
}
