import { useEffect, useState } from "react";

interface Character { id: number; nickname: string; static_id: string; is_public: boolean; }
interface Binding { id: number; server_code?: string; server_label?: string; faction_code?: string; faction_label?: string; }
interface CatalogItem { code: string; label?: string; name?: string; }

export function BlackbirdCharacters() {
  const [characters, setCharacters] = useState<Character[]>([]);
  const [bindings, setBindings] = useState<Binding[]>([]);
  const [catalog, setCatalog] = useState<{servers:CatalogItem[]; factions:CatalogItem[]}>({servers:[],factions:[]});
  const [drafts, setDrafts] = useState<Record<number, { nickname: string; static_id: string }>>({});
  const [scope, setScope] = useState<Record<number,{server_code:string;faction_code:string}>>({});
  const [nickname, setNickname] = useState("");
  const [staticId, setStaticId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const refresh = async () => {
    const data = await window.tmodDesktop?.accountRequest?.("characters");
    if (!data) throw new Error("character_session_unavailable");
    setCharacters((data.characters || []) as Character[]);
    setBindings((data.bindings || []) as Binding[]);
    setCatalog((data.catalog || {servers:[],factions:[]}) as {servers:CatalogItem[];factions:CatalogItem[]});
    setLoaded(true);
  };
  useEffect(() => { let active = true; void refresh().catch(() => { if (active) { setError("Персонажи пока недоступны. Проверьте соединение и доступ к Товариществу."); setLoaded(true); } }); return () => { active = false; }; }, []);
  const mutate = async (values: Record<string,string>) => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const result = await window.tmodDesktop?.accountRequest?.("characters-update", values);
      if (!result) throw new Error("unavailable");
      setCharacters((result.characters || []) as Character[]);
      setBindings((result.bindings || []) as Binding[]);
      setCatalog((result.catalog || {servers:[],factions:[]}) as {servers:CatalogItem[];factions:CatalogItem[]});
      if (values.action === "add") { setNickname(""); setStaticId(""); }
    } catch (cause) {
      const code = String(cause);
      setError(code.includes("profile_static_taken") ? "Этот статик уже закреплён за другим аккаунтом." :
        code.includes("profile_character_limit") ? "Можно добавить не больше трёх персонажей." :
        code.includes("profile_static_invalid") ? "Статик должен состоять только из цифр." :
        "Не удалось сохранить персонажа. Проверьте имя, статик и соединение.");
    } finally { setBusy(false); }
  };
  return <div className="bb-character-settings">
    <section className="bbs-group"><small>ВАША ИГРОВАЯ ЛИЧНОСТЬ</small><h2>Персонажи аккаунта</h2><p>Персонажи синхронизируются с T-Mod и доступны для Atlas Overlay. Статик и сервер используются для точного поиска в Communicate только после вашего согласия.</p>
      {!loaded && <p role="status">Загружаем персонажей…</p>}
      <div className="bb-character-list">{characters.map((character, index) => {
        const binding = bindings.find(item => Number(item.id) === Number(character.id));
        const draft = drafts[character.id] || { nickname: character.nickname, static_id: character.static_id };
        const currentScope = scope[character.id] || { server_code: binding?.server_code || catalog.servers[0]?.code || "phoenix-15", faction_code: binding?.faction_code || catalog.factions[0]?.code || "lspd" };
        return <article key={character.id}><div className="bb-character-index">0{index + 1}</div><div className="bb-character-content"><div className="bb-character-meta"><strong>{character.nickname}</strong><small>#{character.static_id}{binding?.server_label || binding?.server_code ? ` · ${binding.server_label || binding.server_code}` : " · сервер не привязан"}</small></div>
          <div className="bb-character-edit"><input aria-label={`Имя ${character.nickname}`} value={draft.nickname} maxLength={48} onChange={e => setDrafts(value => ({ ...value, [character.id]: { ...draft, nickname: e.target.value } }))}/><input aria-label={`Статик ${character.nickname}`} value={draft.static_id} maxLength={12} inputMode="numeric" onChange={e => setDrafts(value => ({ ...value, [character.id]: { ...draft, static_id: e.target.value } }))}/><button disabled={busy || draft.nickname === character.nickname && draft.static_id === character.static_id} onClick={() => void mutate({action:"edit",character_id:String(character.id),...draft})}>Сохранить</button></div>
          <div className="bb-character-controls"><label><input type="checkbox" checked={character.is_public} disabled={busy} onChange={e => void mutate({action:"visibility",character_id:String(character.id),is_public:String(e.target.checked)})}/> Показывать в открытом профиле</label><button disabled={busy} onClick={() => { if (window.confirm(`Удалить ${character.nickname} из аккаунта?`)) void mutate({action:"delete",character_id:String(character.id)}); }}>Удалить</button></div>
          <div className="bb-character-scope"><label>Сервер<select value={currentScope.server_code} onChange={e => setScope(value => ({ ...value, [character.id]: { ...currentScope, server_code:e.target.value } }))}>{catalog.servers.map(item => <option key={item.code} value={item.code}>{item.label || item.name || item.code}</option>)}</select></label><label>Фракция<select value={currentScope.faction_code} onChange={e => setScope(value => ({ ...value, [character.id]: { ...currentScope, faction_code:e.target.value } }))}>{catalog.factions.map(item => <option key={item.code} value={item.code}>{item.label || item.name || item.code}</option>)}</select></label><button disabled={busy || !currentScope.server_code || !currentScope.faction_code} onClick={() => void mutate({action:"bind",character_id:String(character.id),...currentScope})}>Привязать</button></div>
        </div></article>;
      })}</div>
      {loaded && characters.length === 0 && !error && <p>Пока нет персонажей. Добавьте первого ниже.</p>}
    </section>
    {loaded && characters.length < 3 && <section className="bbs-group"><h2>Добавить персонажа</h2><div className="bb-character-add"><label>Имя персонажа<input value={nickname} maxLength={48} placeholder="Имя Фамилия" onChange={e => setNickname(e.target.value)}/></label><label>Статик<input value={staticId} maxLength={12} inputMode="numeric" placeholder="263345" onChange={e => setStaticId(e.target.value)}/></label><button disabled={busy || nickname.trim().length < 2 || !/^\d{1,12}$/.test(staticId)} onClick={() => void mutate({action:"add",nickname,static_id:staticId})}>Добавить персонажа</button></div></section>}
    <section className="bbs-group"><h2>Как работает поиск</h2><p>Communicate ищет по связке «сервер + статик». Вы сами включаете видимость персонажа и отдельно разрешаете поиск в Communicate. Настройки голоса и оверлея — в разделе «Atlas Overlay».</p></section>
    {error && <p className="bbs-message" role="alert">{error} <button onClick={() => void refresh().catch(() => setError("Соединение пока недоступно."))}>Повторить</button></p>}
  </div>;
}
