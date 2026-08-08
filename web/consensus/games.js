"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = { viewer: null, match: null, csrf: "", selected: null, busy: false, timer: null };
  const pieces = { K:"♔",Q:"♕",R:"♖",B:"♗",N:"♘",P:"♙",k:"♚",q:"♛",r:"♜",b:"♝",n:"♞",p:"♟" };
  const diceFaces = ["","⚀","⚁","⚂","⚃","⚄","⚅"];
  const el = (tag, className = "", text = "") => { const node = document.createElement(tag); if (className) node.className = className; if (text !== "") node.textContent = String(text); return node; };
  let toastTimer = null;

  function toast(message, error = false) { const node = byId("games-toast"); clearTimeout(toastTimer); node.textContent = String(message || "Готово"); node.style.color = error ? "var(--red)" : "var(--mint)"; node.hidden = false; toastTimer = setTimeout(() => { node.hidden = true; }, 4300); }
  const requestId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  async function api(path, options = {}) {
    const headers = { Accept:"application/json", ...(options.headers || {}) };
    if (options.body) { headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = state.csrf; headers["X-Idempotency-Key"] = requestId(); }
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), options.timeout || 15000);
    try {
      const response = await fetch(path, { credentials:"same-origin", cache:"no-store", ...options, headers, signal:controller.signal });
      if (response.status === 401) { if (/^\/games\/[A-Za-z0-9]+/.test(location.pathname)) sessionStorage.setItem("tmod-pending-game", location.pathname); location.assign("/login?next=%2Fgames"); throw new Error("Требуется вход"); }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) { const error = new Error(data.message || data.error || `HTTP ${response.status}`); error.code = data.error; throw error; }
      return data;
    } finally { clearTimeout(timeout); }
  }
  function openDialog(dialog) { if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", ""); }
  function closeDialog(dialog) { if (dialog.close) dialog.close(); else dialog.removeAttribute("open"); }
  function opponentSide(match) { return match.viewer_side === "white" ? "black" : "white"; }
  function matchLabel(match) { return match.game_type === "chess" ? "Шахматы" : "Нарды"; }
  function statusLabel(match) { if (match.status === "waiting") return "Ожидает соперника"; if (match.status === "finished") return match.result === "draw" ? "Ничья" : "Матч завершён"; if (match.spectator) return "Матч в процессе"; return match.can_move ? "Ваш ход" : "Ход соперника"; }

  function showLobby() { clearInterval(state.timer); state.timer = null; state.match = null; state.selected = null; history.replaceState(null,"","/games"); byId("match-screen").hidden = true; byId("lobby-screen").hidden = false; void loadLobby(); }
  function matchCard(match) {
    const card = el("button","match-card"); card.type = "button";
    const head = el("header"); head.append(el("i","",match.game_type === "chess" ? "♞" : "⚄"), el("b","",statusLabel(match).toUpperCase()));
    const opponent = match.players?.[match.viewer_side === "white" ? "black" : "white"]?.name || "Ожидание игрока";
    card.append(head, el("h3","",`${matchLabel(match)} · ${opponent}`), el("p","",match.mode === "bot" ? `T‑Mod Bot · уровень ${match.bot_level}` : "Приватный матч по приглашению"));
    card.addEventListener("click", () => openMatch(match.id)); return card;
  }
  async function loadLobby() {
    try { const data = await api("/api/games/lobby"); state.viewer = data.viewer; state.csrf = data.viewer.csrf_token; const list = byId("match-list"); list.replaceChildren(...(data.matches || []).map(matchCard)); if (!data.matches?.length) list.append(el("div","match-card","Создайте первую партию — она появится здесь.")); byId("match-count").textContent = `${data.matches?.length || 0} матчей`; byId("games-loading").hidden = true; byId("games-app").hidden = false; }
    catch (error) { if (error.message !== "Требуется вход") { byId("games-loading").querySelector("p").textContent = "Игровой зал временно недоступен."; } }
  }
  function openCreate(gameType = "chess") { byId("create-form").reset(); const radio = byId("create-form").querySelector(`input[value='${gameType}']`); if (radio) radio.checked = true; byId("bot-level-field").hidden = false; openDialog(byId("create-dialog")); }
  async function createMatch(event) {
    event.preventDefault(); if (state.busy) return; const form = event.currentTarget; const values = Object.fromEntries(new FormData(form)); state.busy = true;
    try { const data = await api("/api/games/matches", { method:"POST", body:JSON.stringify(values) }); closeDialog(byId("create-dialog")); await enterMatch(data.match); toast(data.match.mode === "friend" ? "Матч создан. Отправьте приглашение." : "T‑Mod Bot готов к партии."); }
    catch (error) { toast(error.message,true); } finally { state.busy = false; }
  }
  async function openMatch(id) { try { const data = await api(`/api/games/matches/${encodeURIComponent(id)}`); state.viewer = data.viewer; state.csrf = data.viewer.csrf_token; await enterMatch(data.match); } catch (error) { toast(error.message,true); showLobby(); } }
  async function enterMatch(match) { state.match = match; state.selected = null; history.replaceState(null,"",`/games/${match.id}`); byId("lobby-screen").hidden = true; byId("match-screen").hidden = false; renderMatch(); clearInterval(state.timer); if (["active","waiting"].includes(match.status)) state.timer = setInterval(() => { if (!document.hidden && !state.busy) void refreshMatch(); }, 2500); }
  async function refreshMatch() { if (!state.match) return; try { const data = await api(`/api/games/matches/${encodeURIComponent(state.match.id)}`); if (Number(data.match.version) !== Number(state.match.version)) { state.match = data.match; state.selected = null; renderMatch(); } } catch {} }

  function chessPosition(fen) { const board = {}; const ranks = String(fen || "").split(" ")[0].split("/"); ranks.forEach((rank,row) => { let file = 0; for (const token of rank) { if (/\d/.test(token)) file += Number(token); else { board[`${"abcdefgh"[file]}${8-row}`] = token; file += 1; } } }); return board; }
  function renderChess() {
    const match = state.match, boardNode = byId("chess-board"), position = chessPosition(match.state.fen), legal = match.legal_moves || [];
    boardNode.hidden = false; byId("backgammon-board").hidden = true; boardNode.replaceChildren();
    const orientation = match.viewer_side === "black" ? "black" : "white"; const ranks = orientation === "white" ? [8,7,6,5,4,3,2,1] : [1,2,3,4,5,6,7,8]; const files = orientation === "white" ? [..."abcdefgh"] : [..."hgfedcba"];
    const legalDestinations = state.selected ? legal.filter(move => move.startsWith(state.selected)).map(move => move.slice(2,4)) : [];
    for (const rank of ranks) for (const file of files) { const square = `${file}${rank}`, button = el("button",`chess-square ${("abcdefgh".indexOf(file)+rank)%2 ? "dark" : "light"}`,pieces[position[square]] || ""); button.type="button"; button.dataset.square=square; if (state.selected === square) button.classList.add("selected"); if (legalDestinations.includes(square)) button.classList.add(position[square] ? "capture" : "legal"); if (String(match.state.last_move || "").includes(square)) button.classList.add("last"); button.addEventListener("click",() => chessClick(square,position)); boardNode.append(button); }
  }
  function chessClick(square, position) { const match=state.match, legal=match.legal_moves || []; if (!match.can_move) return; if (state.selected) { const candidates=legal.filter(move=>move.startsWith(state.selected+square)); if (candidates.length) { const move=candidates.find(item=>item.endsWith("q")) || candidates[0]; state.selected=null; void sendMove({move}); return; } } const side=match.viewer_side; const token=position[square]; const owns=token && (side === "white" ? token === token.toUpperCase() : token === token.toLowerCase()); state.selected=owns && legal.some(move=>move.startsWith(square)) ? square : null; renderChess(); }

  function checkerStack(count, side) { const stack=el("span","bg-stack"); const shown=Math.min(Math.abs(count),5); for(let i=0;i<shown;i++) stack.append(el("i",`checker ${side === "black" ? "black" : ""}`)); if(Math.abs(count)>5) stack.append(el("b","bg-count",Math.abs(count))); return stack; }
  function renderBackgammon() {
    const match=state.match, root=byId("backgammon-board"), board=match.state.board || [], legal=match.legal_moves || []; root.hidden=false; byId("chess-board").hidden=true; root.replaceChildren();
    const top=el("div","bg-row top"), bottom=el("div","bg-row bottom"); const destinations=state.selected!==null ? legal.filter(m=>String(m.from)===String(state.selected)).map(m=>String(m.to)) : [];
    const makePoint=(index,row)=>{ const button=el("button","bg-point"); button.type="button"; button.dataset.point=String(index); const value=Number(board[index]||0); button.append(checkerStack(value,value>0?"white":"black")); if(String(state.selected)===String(index))button.classList.add("selected"); if(destinations.includes(String(index)))button.classList.add("legal"); button.addEventListener("click",()=>backgammonClick(index)); row.append(button); };
    for(const index of [23,22,21,20,19,18,17,16,15,14,13,12])makePoint(index,top); for(const index of [0,1,2,3,4,5,6,7,8,9,10,11])makePoint(index,bottom);
    const middle=el("div","bg-middle"), bar=el("button","bg-tray"), off=el("button","bg-tray"); bar.type=off.type="button"; bar.dataset.tray="bar"; off.dataset.tray="off"; const viewerSide=match.viewer_side || "white"; bar.append(el("small","","БАР"),checkerStack(Number(match.state.bar?.[viewerSide]||0),viewerSide)); off.append(el("small","","СНЯТО"),checkerStack(Number(match.state.off?.[viewerSide]||0),viewerSide)); if(String(state.selected)==="bar")bar.classList.add("selected"); if(destinations.includes("off"))off.classList.add("legal"); bar.addEventListener("click",()=>backgammonClick("bar")); off.addEventListener("click",()=>backgammonClick("off")); middle.append(bar,off); root.append(top,middle,bottom);
  }
  function backgammonClick(point) { const match=state.match,legal=match.legal_moves||[]; if(!match.can_move)return; if(state.selected!==null){const candidate=legal.find(m=>String(m.from)===String(state.selected)&&String(m.to)===String(point));if(candidate){const source=state.selected;state.selected=null;void sendMove({from:source,to:point,die:candidate.die});return;}} state.selected=legal.some(m=>String(m.from)===String(point))?point:null;renderBackgammon(); }
  async function sendMove(payload) { if(state.busy||!state.match)return;state.busy=true;try{const data=await api(`/api/games/matches/${encodeURIComponent(state.match.id)}/command`,{method:"POST",body:JSON.stringify({action:"move",version:state.match.version,...payload}),timeout:20000});state.match=data.match;state.selected=null;renderMatch();}catch(error){toast(error.message,true);await refreshMatch();}finally{state.busy=false;} }

  function renderHistory(match) { const history=match.state.history||[], container=byId("move-history"); container.replaceChildren(); history.slice(-18).reverse().forEach((move,index)=>{const text=typeof move==="string"?move:`${move.side === "white" ? "Белые" : "Чёрные"}: ${move.from} → ${move.to} · ${move.die}`;container.append(el("div","move-item",`${history.length-index}. ${text}`));});if(!history.length)container.append(el("div","move-item","Партия только начинается.")); }
  function renderMatch() {
    const match=state.match;if(!match)return;const own=match.viewer_side||"white",other=opponentSide(match),selfPlayer=match.players?.[own]||{},opponent=match.players?.[other]||{};
    byId("match-kicker").textContent=`${match.game_type.toUpperCase()} · ${String(match.id).slice(0,8)}`;byId("match-title").textContent=matchLabel(match);byId("self-label").textContent=match.spectator?"БЕЛЫЕ · НАБЛЮДЕНИЕ":"ВАША СТОРОНА";byId("self-name").textContent=selfPlayer.name||state.viewer?.name||"Наблюдатель";byId("self-avatar").textContent=String(selfPlayer.name||"В").charAt(0).toUpperCase();byId("opponent-name").textContent=opponent.name||"Ожидаем игрока";byId("opponent-avatar").textContent=opponent.bot?"T":String(opponent.name||"?").charAt(0).toUpperCase();
    byId("turn-state").textContent=match.can_move?"ВАШ ХОД":match.status==="finished"?"ФИНИШ":"ОЖИДАНИЕ";byId("turn-state").classList.toggle("active",match.can_move);byId("match-status").textContent=statusLabel(match);byId("match-detail").textContent=match.status==="waiting"?"Матч создан. Место соперника зарезервировано по ссылке.":match.status==="finished"?(match.winner_side?`Победили ${match.winner_side==="white"?"белые":"чёрные"}.`:"Партия завершилась вничью."):`Ходят ${match.turn_side==="white"?"белые":"чёрные"}. Позиция обновляется автоматически.`;
    byId("waiting-overlay").hidden=match.status!=="waiting";byId("join-panel").hidden=!match.can_join;byId("resign-match").hidden=!match.viewer_side||match.status!=="active";byId("dice-panel").hidden=match.game_type!=="backgammon";byId("dice-list").replaceChildren(...(match.state.dice||[]).map(value=>el("i","die",diceFaces[Number(value)]||value)));renderHistory(match);if(match.game_type==="chess")renderChess();else renderBackgammon();
  }
  async function joinMatch(){if(!state.match||state.busy)return;state.busy=true;try{const data=await api(`/api/games/matches/${encodeURIComponent(state.match.id)}/command`,{method:"POST",body:JSON.stringify({action:"join"})});state.match=data.match;renderMatch();toast("Вы присоединились к матчу.");}catch(error){toast(error.message,true);}finally{state.busy=false;}}
  async function resign(){if(!state.match||state.busy)return;state.busy=true;try{const data=await api(`/api/games/matches/${encodeURIComponent(state.match.id)}/command`,{method:"POST",body:JSON.stringify({action:"resign",version:state.match.version})});state.match=data.match;closeDialog(byId("confirm-game-dialog"));renderMatch();toast("Партия завершена.");}catch(error){toast(error.message,true);}finally{state.busy=false;}}
  async function copyInvite(){if(!state.match)return;const url=`${location.origin}/games/${state.match.id}`;try{await navigator.clipboard.writeText(url);toast("Ссылка на матч скопирована.");}catch{toast(url);} }

  function bind(){byId("new-match-top").addEventListener("click",()=>openCreate());byId("new-match-hero").addEventListener("click",()=>openCreate());document.querySelectorAll("[data-quick-game]").forEach(button=>button.addEventListener("click",()=>openCreate(button.dataset.quickGame)));byId("create-form").addEventListener("submit",createMatch);byId("create-form").elements.mode.addEventListener("change",event=>{byId("bot-level-field").hidden=event.target.value!=="bot";});document.querySelectorAll("[data-close]").forEach(button=>button.addEventListener("click",()=>closeDialog(button.closest("dialog"))));byId("back-lobby").addEventListener("click",showLobby);byId("share-match").addEventListener("click",copyInvite);byId("waiting-copy").addEventListener("click",copyInvite);byId("join-match").addEventListener("click",joinMatch);byId("resign-match").addEventListener("click",()=>openDialog(byId("confirm-game-dialog")));byId("confirm-resign").addEventListener("click",resign);globalThis.addEventListener("pagehide",()=>clearInterval(state.timer));}
  async function boot(){bind();const pending=sessionStorage.getItem("tmod-pending-game");if(pending&&location.pathname==="/games"){sessionStorage.removeItem("tmod-pending-game");history.replaceState(null,"",pending);}const id=location.pathname.split("/").filter(Boolean)[1];if(id){try{const data=await api(`/api/games/matches/${encodeURIComponent(id)}`);state.viewer=data.viewer;state.csrf=data.viewer.csrf_token;byId("games-loading").hidden=true;byId("games-app").hidden=false;await enterMatch(data.match);}catch(error){if(error.message!=="Требуется вход"){toast(error.message,true);await loadLobby();}}}else await loadLobby();}
  void boot();
})();
