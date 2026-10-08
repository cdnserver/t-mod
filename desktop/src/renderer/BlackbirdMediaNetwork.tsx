import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { trustedDiscordAvatar } from "./AccountAvatar";
import { describeSharedLink } from "../shared/communicate-links";
import { MediaComposers, MediaReadGate, isPublishedMediaPost, mergeMediaPosts } from "../shared/media-flow";
import "./blackbird-media-network.css";

interface Profile { user_id: string; display_name: string; bio: string; cover_theme: string; is_public: boolean; avatar_revision?: string | null; cover_revision?: string | null }
interface Post { id: number; author_id: string; display_name: string; kind: "post" | "rollback"; body: string; source_url: string; created_at: string; avatar_revision?: string | null }
type Tab = "feed" | "people" | "rollback";
type MediaImage = { bytes: Uint8Array; mimeType: string; revision: string } | null;
const MediaImageCache = createContext<Map<string, Promise<MediaImage>> | null>(null);
const demoProfile: Profile = { user_id: "1", display_name: "Роберт", bio: "Идеи превращаются в системы.", cover_theme: "orbit", is_public: true };
const demoFeed: Post[] = [{ id: 1, author_id: "1", display_name: "Роберт", kind: "post", body: "Добро пожаловать в Медиасеть Blackbird. Профиль появляется здесь только с согласия владельца.", source_url: "", created_at: new Date().toISOString() }];

async function request(action: "media" | "media-update", data?: Record<string, string>): Promise<Record<string, unknown>> {
  if (import.meta.env.DEV && new URLSearchParams(location.search).get("media-preview") === "1" && !window.tmodDesktop) {
    if (action === "media") return { viewer: { id: "1" }, profile: demoProfile, feed: demoFeed };
    if (data?.action === "search") return { result: [demoProfile] };
    if (data?.action === "feed") return { result: data.before_id || data.kind === "rollback" ? [] : demoFeed, has_more: false };
    if (data?.action === "profile_view") return { result: demoProfile };
    if (data?.action === "profile_posts") return { result: data.before_id ? [] : demoFeed, has_more: false };
    throw new Error("preview_read_only");
  }
  const result = await window.tmodDesktop?.accountRequest?.(action, data);
  if (!result) throw new Error("media_unavailable");
  return result;
}

function useProfileImage(userId: string, kind: "avatar" | "cover", revision?: string | null): string {
  const [url, setUrl] = useState("");
  const cache = useContext(MediaImageCache);
  useEffect(() => {
    setUrl("");
    if (!revision || !userId || !window.tmodDesktop?.mediaAsset) return;
    let live = true;
    let objectUrl = "";
    const key = `${userId}:${kind}:${revision}`;
    let pending = cache?.get(key);
    if (!pending) {
      pending = window.tmodDesktop.mediaAsset(userId, kind);
      cache?.set(key, pending);
      void pending.catch(() => { if (cache && cache.get(key) === pending) cache.delete(key); });
    }
    void pending.then(asset => {
      if (!live || !asset || asset.revision !== revision) return;
      objectUrl = URL.createObjectURL(new Blob([asset.bytes as BlobPart], { type: asset.mimeType }));
      setUrl(objectUrl);
    }).catch(() => { /* A private or removed image falls back to the profile theme. */ });
    return () => { live = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [cache, userId, kind, revision]);
  return url;
}

function MediaAvatar({ userId, revision, name, fallbackUrl, className = "bb-media-avatar" }: {
  userId: string; revision?: string | null; name: string; fallbackUrl?: string | null; className?: string;
}) {
  const image = useProfileImage(userId, "avatar", revision);
  const source = image || trustedDiscordAvatar(fallbackUrl);
  return <span className={className} aria-hidden="true">{source ? <img src={source} alt="" draggable={false}/> : name.slice(0, 1).toUpperCase()}</span>;
}

function Cover({ theme, userId = "", revision }: { theme: string; userId?: string; revision?: string | null }) {
  const image = useProfileImage(userId, "cover", revision);
  return <div className={`bb-media-cover bb-media-cover-${["orbit", "night", "silver"].includes(theme) ? theme : "orbit"} ${image ? "has-image" : ""}`} aria-hidden="true">{image && <img src={image} alt="" draggable={false}/>}<span/><i/><b/></div>;
}

function MediaPost({ item, viewerId, ownAvatar, onView, onDelete, onLink }: {
  item: Post; viewerId: string; ownAvatar?: string | null;
  onView: (id: string) => void; onDelete: (id: number) => void; onLink: (url: string) => void;
}) {
  const card = item.source_url ? describeSharedLink(item.source_url) : undefined;
  const date = new Date(item.created_at);
  return <article className="bb-media-post"><header>
    <MediaAvatar userId={item.author_id} revision={item.avatar_revision} name={item.display_name} fallbackUrl={item.author_id === viewerId ? ownAvatar : null} className="bb-media-person-mark"/>
    <div><button onClick={() => onView(item.author_id)}>{item.display_name}</button><small>{Number.isFinite(date.getTime()) ? date.toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" }) : ""} · {item.kind === "rollback" ? "Откат" : "Публикация"}</small></div>
    {item.author_id === viewerId && <button className="bb-media-delete" aria-label="Удалить запись" onClick={() => onDelete(item.id)}>×</button>}
  </header><p>{item.body}</p>{card && <a href={card.url} onClick={event => { event.preventDefault(); onLink(card.url); }}><span>↗</span><div><small>{card.contour}</small><strong>{card.title}</strong><p>{card.detail}</p></div><b>Открыть ↗</b></a>}</article>;
}

export function BlackbirdMediaNetwork({ name, avatarUrl, onBack }: { name: string; avatarUrl?: string | null; onBack: () => void }) {
  const initialName = useRef(name);
  const [tab, setTab] = useState<Tab>("feed");
  const [profile, setProfile] = useState<Profile>();
  const [viewerId, setViewerId] = useState("");
  const [feed, setFeed] = useState<Post[]>([]);
  const [people, setPeople] = useState<Profile[]>([]);
  const [selected, setSelected] = useState<Profile>();
  const selectedState = useRef<Profile | undefined>(undefined);
  selectedState.current = selected;
  const [selectedPosts, setSelectedPosts] = useState<Post[]>([]);
  const [editing, setEditing] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [draftBio, setDraftBio] = useState("");
  const [draftTheme, setDraftTheme] = useState("orbit");
  const [draftPublic, setDraftPublic] = useState(false);
  const composers = useRef(new MediaComposers(() => crypto.randomUUID().replaceAll("-", "")));
  const [, setComposerRevision] = useState(0);
  const postKind = tab === "rollback" ? "rollback" : "post";
  const { body: postBody, source: sourceUrl } = composers.current.read(postKind);
  const updateComposer = (patch: { body?: string; source?: string }) => {
    composers.current.update(postKind, patch);
    setComposerRevision(value => value + 1);
  };
  const [search, setSearch] = useState("");
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [searched, setSearched] = useState(false);
  const [ready, setReady] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState("");
  const imageCache = useMemo(() => new Map<string, Promise<MediaImage>>(), [viewerId]);
  const avatarFileRef = useRef<HTMLInputElement>(null);
  const coverFileRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const mounted = useRef(false);
  const mutationBusy = useRef(false);
  const reads = useRef(new MediaReadGate());
  const screen = useRef("feed");

  useEffect(() => {
    if (!editing || !dialogRef.current) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    dialog.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); if (!mutationBusy.current) setEditing(false); }
      if (event.key !== "Tab") return;
      const focusable = [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled),input:not([type="file"]),textarea,[tabindex="0"]')].filter(element => element.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) { event.preventDefault(); first.focus(); }
    };
    dialog.addEventListener("keydown", keyboard);
    return () => { dialog.removeEventListener("keydown", keyboard); if (previous?.isConnected) previous.focus(); };
  }, [editing]);

  const failure = (cause: unknown) => {
    const code = String(cause);
    setError(code.includes("preview_read_only") ? "В предпросмотре изменения не сохраняются." : code.includes("media_rate_limited") ? "Не более шести публикаций в час." : code.includes("media_profile_not_public") ? "Сначала опубликуйте профиль." : code.includes("media_asset_") ? "Изображение должно быть PNG, JPG или WebP, не больше 8 МБ и не меньше 96 × 96 пикселей." : code.includes("media_source") ? "Укажите корректную ссылку на видео или материал (http или https)." : "Не удалось выполнить действие. Проверьте данные и подключение, затем повторите попытку.");
  };
  const read = async <T,>(operation: () => Promise<T>, apply: (value: T) => void) => {
    const ticket = reads.current.begin();
    setReading(true); setError("");
    try {
      const result = await operation();
      if (mounted.current && reads.current.accepts(ticket)) apply(result);
    } catch (cause) {
      if (mounted.current && reads.current.accepts(ticket)) failure(cause);
    } finally {
      if (mounted.current && reads.current.accepts(ticket)) setReading(false);
    }
  };

  useEffect(() => {
    mounted.current = true;
    const ticket = reads.current.begin();
    screen.current = "feed";
    setReady(false); setReading(false); setTab("feed"); setSelected(undefined); setError("");
    void request("media").then(data => {
      if (!mounted.current || !reads.current.accepts(ticket)) return;
      const mine = data.profile as Profile;
      setProfile(mine); setViewerId(String((data.viewer as { id?: string })?.id || ""));
      setDraftName(mine.display_name || initialName.current); setDraftBio(mine.bio || "");
      setDraftTheme(mine.cover_theme || "orbit"); setDraftPublic(Boolean(mine.is_public));
      setFeed((data.feed || []) as Post[]); setHasMore(data.has_more === true); setReady(true);
    }).catch(() => { if (mounted.current && reads.current.accepts(ticket)) { setReady(true); setError("Медиасеть недоступна. Проверьте подключение и повторите попытку."); } });
    return () => { mounted.current = false; reads.current.invalidate(); };
  }, [attempt]);
  const run = async (operation: () => Promise<void>) => {
    if (mutationBusy.current) return;
    mutationBusy.current = true;
    setBusy(true); setError("");
    try { await operation(); }
    catch (cause) {
      if (mounted.current) failure(cause);
    } finally { mutationBusy.current = false; if (mounted.current) setBusy(false); }
  };
  const loadFeed = (next: Tab, append = false) => void read(
    () => request("media-update", { action: "feed", kind: next === "rollback" ? "rollback" : "all", ...(append && feed.length ? { before_id: String(feed[feed.length - 1].id) } : {}) }),
    data => { setFeed(current => append ? mergeMediaPosts(current, (data.result || []) as Post[]) : (data.result || []) as Post[]); setHasMore(data.has_more === true); },
  );
  const refreshVisible = () => {
    const destination = screen.current;
    if (destination === "feed" || destination === "rollback") loadFeed(destination);
    else if (destination.startsWith("profile:")) view(destination.slice(8));
  };
  const openTab = (next: Tab) => {
    screen.current = next; reads.current.invalidate(); setReading(false);
    setTab(next); setSelected(undefined); setSelectedPosts([]); setFeed([]); setHasMore(false); setError("");
    if (next !== "people") loadFeed(next);
  };
  const edit = () => {
    if (!profile || mutationBusy.current) return;
    setDraftName(profile.display_name || name); setDraftBio(profile.bio || "");
    setDraftTheme(profile.cover_theme || "orbit"); setDraftPublic(profile.is_public); setError(""); setEditing(true);
  };
  const closeEditor = () => { if (!mutationBusy.current) setEditing(false); };
  const save = () => void run(async () => {
    const data = await request("media-update", { action: "profile", display_name: draftName.trim(), bio: draftBio.trim(), cover_theme: draftTheme, is_public: String(draftPublic) });
    if (!mounted.current) return;
    const updated = data.result as Profile;
    setProfile(updated); setSelected(current => current?.user_id === viewerId ? updated : current); setEditing(false);
    setFeed(current => current.filter(item => updated.is_public || item.author_id !== viewerId).map(item => item.author_id === viewerId ? { ...item, display_name: updated.display_name } : item));
    if (["feed", "rollback"].includes(screen.current)) loadFeed(screen.current as Tab);
  });
  const publish = () => void run(async () => {
    const sending = composers.current.capture(postKind);
    const kind = sending.kind;
    const destination = tab;
    const body = sending.body.trim(), source = kind === "rollback" ? sending.source.trim() : "";
    const data = await request("media-update", { action: "post", kind, body, source_url: source, client_nonce: sending.nonce });
    if (!mounted.current) return;
    if (!isPublishedMediaPost(data.result, viewerId, sending)) throw new Error("media_response_invalid");
    composers.current.acknowledge(sending);
    setComposerRevision(value => value + 1);
    if (screen.current === destination) {
      setFeed(current => mergeMediaPosts(current, [{ ...data.result as Post, avatar_revision: profile?.avatar_revision }]));
    }
    if (screen.current === destination || screen.current === `profile:${viewerId}`) refreshVisible();
  });
  const find = () => void read(() => request("media-update", { action: "search", query: search.trim() }), data => { setPeople((data.result || []) as Profile[]); setSearched(true); });
  const changeSearch = (value: string) => {
    reads.current.invalidate();
    setReading(false); setSearch(value); setPeople([]); setSearched(false); setError("");
  };
  const view = (id: string) => {
    screen.current = `profile:${id}`;
    if (selectedState.current?.user_id !== id) { setSelected(undefined); setSelectedPosts([]); }
    setHasMore(false);
    void read(async () => {
      const data = id === viewerId ? { result: profile } : await request("media-update", { action: "profile_view", user_id: id });
      const posts = data.result ? await request("media-update", { action: "profile_posts", user_id: id }) : { result: [] };
      return { person: data.result as Profile | undefined, posts };
    }, ({ person, posts }) => {
      if (!person) { setSelected(undefined); setSelectedPosts([]); setError("Профиль больше не опубликован."); return; }
      setSelected(person); setSelectedPosts((posts.result || []) as Post[]); setHasMore(posts.has_more === true);
    });
  };
  const more = () => {
    if (selected) void read(() => request("media-update", { action: "profile_posts", user_id: selected.user_id, before_id: String(selectedPosts[selectedPosts.length - 1].id) }), data => { setSelectedPosts(current => mergeMediaPosts(current, (data.result || []) as Post[])); setHasMore(data.has_more === true); });
    else loadFeed(tab, true);
  };
  const remove = (id: number) => void run(async () => {
    const data = await request("media-update", { action: "delete", post_id: String(id) });
    if (data.result && mounted.current) {
      setFeed(current => current.filter(item => item.id !== id)); setSelectedPosts(current => current.filter(item => item.id !== id));
      refreshVisible();
    }
  });
  const openLink = async (url: string) => {
    try {
      if (!await window.tmodDesktop?.openSharedLink?.(url)) setError("Не удалось открыть ссылку. Проверьте подключение и доступ к сервису.");
    } catch { setError("Не удалось открыть ссылку. Повторите попытку."); }
  };
  const uploadImage = (kind: "avatar" | "cover", file: File) => void run(async () => {
    if (!window.tmodDesktop?.mediaUpload) throw new Error("preview_read_only");
    if (!(["image/png", "image/jpeg", "image/webp"].includes(file.type)) || file.size > 8 * 1024 * 1024) throw new Error("media_asset_invalid");
    const result = await window.tmodDesktop.mediaUpload(kind, new Uint8Array(await file.arrayBuffer()));
    if (!mounted.current) return;
    const key = kind === "avatar" ? "avatar_revision" : "cover_revision";
    setProfile(current => current ? { ...current, [key]: result.revision } : current);
    setSelected(current => current?.user_id === viewerId ? { ...current, [key]: result.revision } : current);
    if (kind === "avatar") {
      setFeed(current => current.map(post => post.author_id === viewerId ? { ...post, avatar_revision: result.revision } : post));
      setSelectedPosts(current => current.map(post => post.author_id === viewerId ? { ...post, avatar_revision: result.revision } : post));
    }
  });
  const removeImage = (kind: "avatar" | "cover") => void run(async () => {
    if (!window.tmodDesktop?.mediaRemove) throw new Error("preview_read_only");
    await window.tmodDesktop.mediaRemove(kind);
    if (!mounted.current) return;
    const key = kind === "avatar" ? "avatar_revision" : "cover_revision";
    setProfile(current => current ? { ...current, [key]: null } : current);
    setSelected(current => current?.user_id === viewerId ? { ...current, [key]: null } : current);
    if (kind === "avatar") {
      setFeed(current => current.map(post => post.author_id === viewerId ? { ...post, avatar_revision: null } : post));
      setSelectedPosts(current => current.map(post => post.author_id === viewerId ? { ...post, avatar_revision: null } : post));
    }
  });
  const visible = selected || profile;
  const retryRead = () => {
    if (!profile) setAttempt(current => current + 1);
    else if (screen.current.startsWith("profile:")) view(screen.current.slice(8));
    else if (tab === "people") find();
    else loadFeed(tab);
  };

  return <MediaImageCache.Provider value={imageCache}><section className="bb-media" aria-label="Медиасеть Blackbird">
    <header className="bb-media-top"><button onClick={onBack}>← Blackbird</button><span>МЕДИАСЕТЬ / ТЕХНОЛОГИИ ТОВАРИЩЕСТВА</span><i>{profile?.is_public ? "ПРОФИЛЬ ОПУБЛИКОВАН" : "ПРИВАТНЫЙ ПРОФИЛЬ"}</i></header>
    <div className="bb-media-app">
      <nav className="bb-media-nav" aria-label="Разделы Медиасети"><div className="bb-media-nav-brand"><small>BLACKBIRD</small><strong>Медиасеть<span>.</span></strong></div>
        <button disabled={!ready || !profile} className={tab === "feed" && !selected ? "active" : ""} onClick={() => openTab("feed")}>⌂ <span>Лента</span></button>
        <button disabled={!ready || !profile} className={tab === "people" && !selected ? "active" : ""} onClick={() => openTab("people")}>⌕ <span>Люди</span></button>
        <button disabled={!ready || !profile} className={tab === "rollback" && !selected ? "active" : ""} onClick={() => openTab("rollback")}>▣ <span>Откаты</span></button>
        <div className="bb-media-nav-bottom"><button disabled={!profile} className={selected?.user_id === viewerId ? "active" : ""} onClick={() => view(viewerId)}>◉ <span>Мой профиль</span></button></div>
      </nav>
      <main className="bb-media-main">
        {error && <div className="bb-media-error" role="alert">{error}<button disabled={reading || busy || !ready} onClick={retryRead}>Обновить</button><button aria-label="Скрыть ошибку" onClick={() => setError("")}>×</button></div>}
        {!ready ? <div className="bb-media-loading" role="status">Открываем Медиасеть…</div> : !profile ? <div className="bb-media-empty"><p>Не удалось подключиться к Медиасети.</p><button onClick={() => setAttempt(current => current + 1)}>Повторить подключение</button></div> : reading && !selected && (screen.current.startsWith("profile:") || (tab !== "people" && !feed.length)) ? <div className="bb-media-loading" role="status">Загружаем…</div> : selected ? <>
          <div className="bb-media-section-head"><button onClick={() => openTab(tab)}>← Назад</button><small>{selected.user_id === viewerId ? "МОЙ ПРОФИЛЬ" : "ПУБЛИЧНЫЙ ПРОФИЛЬ"}</small>{selected.user_id === viewerId && <button disabled={busy} onClick={edit}>Редактировать</button>}</div>
          <article className="bb-media-profile-full"><Cover theme={selected.cover_theme} userId={selected.user_id} revision={selected.cover_revision}/><div className="bb-media-profile-full-body"><MediaAvatar userId={selected.user_id} revision={selected.avatar_revision} name={selected.display_name} fallbackUrl={selected.user_id === viewerId ? avatarUrl : null}/><h1>{selected.display_name}</h1><p>{selected.bio || "Пока ничего о себе не рассказал."}</p><small>{selected.is_public ? "Профиль опубликован владельцем аккаунта" : "Приватный профиль · виден только вам"}</small></div></article>
          <div className="bb-media-profile-posts"><h2>Публикации</h2>{selectedPosts.length ? selectedPosts.map(item => <MediaPost key={item.id} item={{ ...item, display_name: selected.display_name, avatar_revision: selected.avatar_revision }} viewerId={viewerId} ownAvatar={avatarUrl} onView={view} onDelete={remove} onLink={openLink}/>) : <div className="bb-media-empty">Публикаций пока нет.</div>}</div>
        </> : tab === "people" ? <>
          <div className="bb-media-section-head"><div><small>ЛЮДИ ТОВАРИЩЕСТВА</small><h1>Найти человека</h1></div></div>
          <form className="bb-media-search" onSubmit={event => { event.preventDefault(); find(); }}><input value={search} onChange={event => changeSearch(event.target.value)} minLength={2} maxLength={64} placeholder="Имя или логин аккаунта" aria-label="Поиск людей"/><button disabled={reading || search.trim().length < 2}>Искать</button></form>
          <p className="bb-media-muted">В поиске видны только люди, которые сами открыли профиль.</p>
          <div className="bb-media-people">{people.map(person => <button key={person.user_id} onClick={() => view(person.user_id)}><MediaAvatar userId={person.user_id} revision={person.avatar_revision} name={person.display_name} className="bb-media-person-mark"/><strong>{person.display_name}</strong><small>{person.bio || "Профиль участника"}</small><b>↗</b></button>)}</div>
          {searched && !people.length && <div className="bb-media-empty">Никого не нашли. Попробуйте другое имя или логин.</div>}
        </> : <>
          <div className="bb-media-section-head"><div><small>{tab === "rollback" ? "АРХИВ МОМЕНТОВ" : "ОБЩИЙ ПОТОК"}</small><h1>{tab === "rollback" ? "Откаты" : "Лента"}<span>.</span></h1><p>{tab === "rollback" ? "Публикации со ссылкой на исходное видео или материал." : "Люди, мысли и события экосистемы."}</p></div><button disabled={busy} onClick={edit}>Настроить профиль</button><button onClick={() => loadFeed(tab)}>Обновить</button></div>
          {!profile?.is_public ? <div className="bb-media-private"><strong>Ваш профиль пока приватный.</strong><p>Публиковать записи и появляться в поиске можно только после вашего согласия.</p><button onClick={edit}>Открыть настройки профиля ↗</button></div> : <div className="bb-media-compose"><MediaAvatar userId={viewerId} revision={profile.avatar_revision} name={profile.display_name || name} fallbackUrl={avatarUrl}/><div><textarea aria-label={postKind === "rollback" ? "Описание отката" : "Текст публикации"} value={postBody} onChange={event => updateComposer({ body: event.target.value })} maxLength={2000} placeholder={tab === "rollback" ? "Что произошло? Добавьте контекст к откату…" : "Что у вас на уме?"}/>{tab === "rollback" && <input aria-label="Ссылка на видео или материал" value={sourceUrl} onChange={event => updateComposer({ source: event.target.value })} maxLength={2048} type="url" placeholder="Ссылка на видео или материал"/>}<footer><span>{postBody.length} / 2000</span><button disabled={busy || postBody.trim().length < 3 || (tab === "rollback" && !sourceUrl.trim())} onClick={publish}>{busy ? "Отправляем…" : "Опубликовать ↗"}</button></footer></div></div>}
          <div className="bb-media-feed">{feed.map(item => <MediaPost key={item.id} item={{ ...item, avatar_revision: item.author_id === viewerId ? profile?.avatar_revision : item.avatar_revision }} viewerId={viewerId} ownAvatar={avatarUrl} onView={view} onDelete={remove} onLink={openLink}/>)}{!feed.length && <div className="bb-media-empty">Пока здесь тихо. Первая история может быть вашей.</div>}</div>
        </>}
        {ready && profile && (selected || tab !== "people") && hasMore && <button className="bb-media-load-more" disabled={reading} onClick={more}>{reading ? "Загружаем…" : "Показать ещё"}</button>}
      </main>
      <aside className="bb-media-side">{visible && <article className="bb-media-mini-profile"><Cover theme={visible.cover_theme} userId={visible.user_id} revision={visible.cover_revision}/><div><MediaAvatar userId={visible.user_id} revision={visible.avatar_revision} name={visible.display_name} fallbackUrl={selected ? null : avatarUrl}/><h2>{visible.display_name}</h2><p>{visible.bio || "История ещё не рассказана."}</p><small>{visible.is_public ? "Публичный профиль" : "Виден только вам"}</small>{(!selected || selected.user_id === viewerId) && <button onClick={edit}>Редактировать профиль ↗</button>}</div></article>}<div className="bb-media-side-note"><small>О ПРОСТРАНСТВЕ</small><p>Закрытые данные из контуров Сената и Atlas сюда не переносятся автоматически.</p></div></aside>
    </div>
    {editing && <div className="bb-media-dialog-backdrop" role="presentation" onClick={closeEditor}><section ref={dialogRef} tabIndex={-1} className="bb-media-dialog" role="dialog" aria-modal="true" aria-label="Редактирование профиля" onClick={event => event.stopPropagation()}>
      <div className="bb-media-dialog-head"><div><small>ВАША ПУБЛИЧНАЯ СТРАНИЦА</small><h2>Профиль</h2></div><button disabled={busy} onClick={closeEditor} aria-label="Закрыть">×</button></div>
      {error && <div className="bb-media-error" role="alert">{error}</div>}
      <div className="bb-media-image-editor">
        <div><MediaAvatar userId={viewerId} revision={profile?.avatar_revision} name={profile?.display_name || name} fallbackUrl={avatarUrl}/><div><strong>Аватар</strong><small>Квадратное изображение, до 8 МБ</small><button type="button" disabled={busy} onClick={() => avatarFileRef.current?.click()}>Выбрать фото</button>{profile?.avatar_revision && <button type="button" disabled={busy} onClick={() => removeImage("avatar")}>Сбросить</button>}</div><input ref={avatarFileRef} type="file" accept="image/png,image/jpeg,image/webp" onChange={event => { const file = event.currentTarget.files?.[0]; if (file) uploadImage("avatar", file); event.currentTarget.value = ""; }}/></div>
        <div><Cover theme={draftTheme} userId={viewerId} revision={profile?.cover_revision}/><div><strong>Обложка</strong><small>Панорама профиля, до 8 МБ</small><button type="button" disabled={busy} onClick={() => coverFileRef.current?.click()}>Выбрать фото</button>{profile?.cover_revision && <button type="button" disabled={busy} onClick={() => removeImage("cover")}>Сбросить</button>}</div><input ref={coverFileRef} type="file" accept="image/png,image/jpeg,image/webp" onChange={event => { const file = event.currentTarget.files?.[0]; if (file) uploadImage("cover", file); event.currentTarget.value = ""; }}/></div>
      </div>
      <p className="bb-media-image-note">Фото и обложка сохраняются сразу. Кнопка «Сохранить» применяет имя, описание и видимость профиля.</p>
      <label>Имя<input value={draftName} maxLength={64} onChange={event => setDraftName(event.target.value)}/></label><label>О себе<textarea value={draftBio} maxLength={500} onChange={event => setDraftBio(event.target.value)} placeholder="Несколько слов о себе"/></label>
      <div className="bb-media-themes"><small>ОБЛОЖКА · ВСТРОЕННЫЕ ТЕМЫ</small>{(["orbit", "night", "silver"] as const).map(theme => <button key={theme} className={draftTheme === theme ? "active" : ""} onClick={() => setDraftTheme(theme)}><Cover theme={theme}/><span>{theme === "orbit" ? "Орбита" : theme === "night" ? "Ночь" : "Серебро"}</span></button>)}</div>
      <label className="bb-media-public-toggle"><input type="checkbox" checked={draftPublic} onChange={event => setDraftPublic(event.target.checked)}/><span><strong>Опубликовать профиль</strong><small>Вас смогут найти и увидеть ваши записи. Если отключить, профиль, фото и записи исчезнут из поиска и ленты.</small></span></label>
      <footer><button disabled={busy} onClick={closeEditor}>Отмена</button><button disabled={busy || draftName.trim().length < 2} onClick={save}>{busy ? "Сохраняем…" : "Сохранить"}</button></footer>
    </section></div>}
  </section></MediaImageCache.Provider>;
}
