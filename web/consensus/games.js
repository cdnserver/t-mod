"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = {
    viewer: null,
    match: null,
    csrf: "",
    selected: null,
    busy: false,
    thinking: false,
    timer: null,
    pendingPromotion: null,
  };

  const pieceKinds = {
    K: "king", Q: "queen", R: "rook", B: "bishop", N: "knight", P: "pawn",
    k: "king", q: "queen", r: "rook", b: "bishop", n: "knight", p: "pawn",
  };
  const pieceLabels = {
    K: "белый король", Q: "белый ферзь", R: "белая ладья", B: "белый слон", N: "белый конь", P: "белая пешка",
    k: "чёрный король", q: "чёрный ферзь", r: "чёрная ладья", b: "чёрный слон", n: "чёрный конь", p: "чёрная пешка",
  };
  const promotionNames = { q: "Ферзь", r: "Ладья", b: "Слон", n: "Конь" };
  const promotionKinds = { q: "queen", r: "rook", b: "bishop", n: "knight" };
  const sideLabels = { white: "Белые", black: "Чёрные" };
  const dieDots = {
    1: [5],
    2: [1, 9],
    3: [1, 5, 9],
    4: [1, 3, 7, 9],
    5: [1, 3, 5, 7, 9],
    6: [1, 3, 4, 6, 7, 9],
  };

  let toastTimer = null;

  function element(tag, className = "", text = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== "") node.textContent = String(text);
    return node;
  }

  function icon(name, className = "") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    if (className) svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute(
      "viewBox",
      name.startsWith("piece-")
        ? "0 0 64 64"
        : ["i-games", "i-chess", "i-backgammon"].includes(name)
          ? "0 0 32 32"
          : "0 0 24 24",
    );
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#${name}`);
    svg.append(use);
    return svg;
  }

  function toast(message, error = false) {
    const node = byId("games-toast");
    clearTimeout(toastTimer);
    node.classList.toggle("error", error);
    node.querySelector("span").textContent = String(message || "Готово");
    node.hidden = false;
    toastTimer = setTimeout(() => { node.hidden = true; }, 4300);
  }

  const requestId = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    if (options.body) {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = state.csrf;
      headers["X-Idempotency-Key"] = requestId();
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeout || 15000);
    try {
      const response = await fetch(path, {
        credentials: "same-origin",
        cache: "no-store",
        ...options,
        headers,
        signal: controller.signal,
      });
      if (response.status === 401) {
        if (/^\/games\/[A-Za-z0-9]+/.test(location.pathname)) {
          sessionStorage.setItem("tmod-pending-game", location.pathname);
        }
        location.assign("/login?next=%2Fgames");
        throw new Error("Требуется вход");
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(data.message || data.error || `HTTP ${response.status}`);
        error.code = data.error;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  function openDialog(dialog) {
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (dialog.id === "promotion-dialog") state.pendingPromotion = null;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  }

  function opposite(side) {
    return side === "white" ? "black" : "white";
  }

  function matchLabel(match) {
    return match.game_type === "chess" ? "Шахматы" : "Короткие нарды";
  }

  function boardSides(match) {
    const bottom = match.viewer_side || "white";
    return { bottom, top: opposite(bottom) };
  }

  function resultLabel(match) {
    if (match.result === "draw") return "Ничья";
    if (!match.winner_side) return "Партия завершена";
    if (match.spectator) return `Победили ${sideLabels[match.winner_side].toLowerCase()}`;
    return match.winner_side === match.viewer_side ? "Вы победили" : "Победа соперника";
  }

  function statusLabel(match) {
    if (state.thinking && match.mode === "bot") return "T-Mod думает";
    if (match.status === "waiting") return "Ожидаем соперника";
    if (match.status === "finished") return resultLabel(match);
    if (match.spectator) return `Ходят ${sideLabels[match.turn_side].toLowerCase()}`;
    return match.can_move ? "Ваш ход" : "Ход соперника";
  }

  function relativeTime(value) {
    const timestamp = Date.parse(String(value || ""));
    if (!Number.isFinite(timestamp)) return "недавно";
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 60) return "только что";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч назад`;
    return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" }).format(new Date(timestamp));
  }

  function russianMatchCount(count) {
    const value = Math.abs(Number(count) || 0);
    if (value % 10 === 1 && value % 100 !== 11) return `${value} матч`;
    if ([2, 3, 4].includes(value % 10) && ![12, 13, 14].includes(value % 100)) return `${value} матча`;
    return `${value} матчей`;
  }

  function setLoadingError(message) {
    const loading = byId("games-loading");
    loading.querySelector("h1").textContent = "Игровая арена недоступна";
    loading.querySelector("p").textContent = message || "Попробуйте обновить страницу через несколько секунд.";
  }

  function showApp() {
    byId("games-loading").hidden = true;
    byId("games-app").hidden = false;
  }

  function showLobby() {
    clearInterval(state.timer);
    state.timer = null;
    state.match = null;
    state.selected = null;
    state.thinking = false;
    history.replaceState(null, "", "/games");
    byId("match-screen").hidden = true;
    byId("lobby-screen").hidden = false;
    void loadLobby();
  }

  function matchCard(match) {
    const card = element("button", `match-card${match.game_type === "backgammon" ? " backgammon" : ""}`);
    card.type = "button";

    const head = element("header");
    const gameIcon = element("span", "card-game-icon");
    gameIcon.append(icon(match.game_type === "chess" ? "i-chess" : "i-backgammon"));
    const active = match.status === "active" || match.status === "waiting";
    const status = element("span", `card-status${active ? " active" : ""}`);
    status.append(element("i"), document.createTextNode(statusLabel(match)));
    head.append(gameIcon, status);

    const otherSide = opposite(match.viewer_side || match.host_side || "white");
    const opponent = match.players?.[otherSide]?.name || "Место свободно";
    const copy = element("div");
    copy.append(
      element("h3", "", matchLabel(match)),
      element("p", "", match.mode === "bot" ? opponent : `Соперник: ${opponent}`),
    );

    const footer = element("footer");
    footer.append(
      element("span", "", relativeTime(match.updated_at)),
      (() => {
        const action = element("b", "", match.status === "finished" ? "Посмотреть" : "Продолжить");
        action.append(icon("i-arrow"));
        return action;
      })(),
    );
    card.append(head, copy, footer);
    card.addEventListener("click", () => openMatch(match.id));
    return card;
  }

  function emptyMatches() {
    const empty = element("div", "match-empty");
    const mark = element("span");
    mark.append(icon("i-spark"));
    const copy = element("div");
    copy.append(
      element("h3", "", "Здесь появится ваша первая партия"),
      element("p", "", "Создайте игру с T-Mod Bot или пригласите участника."),
    );
    empty.append(mark, copy);
    return empty;
  }

  async function loadLobby() {
    try {
      const data = await api("/api/games/lobby");
      state.viewer = data.viewer;
      state.csrf = data.viewer.csrf_token;
      const matches = data.matches || [];
      byId("match-list").replaceChildren(...(matches.length ? matches.map(matchCard) : [emptyMatches()]));
      byId("match-count").textContent = russianMatchCount(matches.length);
      showApp();
    } catch (error) {
      if (error.message !== "Требуется вход") setLoadingError(error.message);
    }
  }

  function syncBotLevelVisibility() {
    const mode = byId("create-form").querySelector('input[name="mode"]:checked')?.value;
    byId("bot-level-field").hidden = mode !== "bot";
  }

  function openCreate(gameType = "chess") {
    const form = byId("create-form");
    form.reset();
    const radio = form.querySelector(`input[name="game_type"][value="${gameType}"]`);
    if (radio) radio.checked = true;
    syncBotLevelVisibility();
    openDialog(byId("create-dialog"));
  }

  async function createMatch(event) {
    event.preventDefault();
    if (state.busy) return;
    const form = event.currentTarget;
    const submit = form.querySelector(".create-submit");
    const values = Object.fromEntries(new FormData(form));
    state.busy = true;
    submit.disabled = true;
    submit.querySelector("b").textContent = "Создаём игровую комнату…";
    try {
      const data = await api("/api/games/matches", { method: "POST", body: JSON.stringify(values) });
      closeDialog(byId("create-dialog"));
      await enterMatch(data.match);
      toast(data.match.mode === "friend" ? "Игра создана — отправьте приглашение." : "T-Mod Bot уже за столом.");
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
      submit.disabled = false;
      submit.querySelector("b").textContent = "Создать игру";
      if (state.match) renderMatch();
    }
  }

  async function openMatch(id) {
    try {
      const data = await api(`/api/games/matches/${encodeURIComponent(id)}`);
      state.viewer = data.viewer;
      state.csrf = data.viewer.csrf_token;
      await enterMatch(data.match);
    } catch (error) {
      toast(error.message, true);
      showLobby();
    }
  }

  function startPolling(match) {
    clearInterval(state.timer);
    state.timer = null;
    if (!["active", "waiting"].includes(match.status)) return;
    state.timer = setInterval(() => {
      if (!document.hidden && !state.busy) void refreshMatch();
    }, 4000);
  }

  async function enterMatch(match) {
    state.match = match;
    state.selected = null;
    state.thinking = false;
    history.replaceState(null, "", `/games/${match.id}`);
    byId("lobby-screen").hidden = true;
    byId("match-screen").hidden = false;
    showApp();
    renderMatch();
    startPolling(match);
  }

  async function refreshMatch() {
    if (!state.match) return;
    try {
      const data = await api(`/api/games/matches/${encodeURIComponent(state.match.id)}`);
      if (Number(data.match.version) !== Number(state.match.version)) {
        state.match = data.match;
        state.selected = null;
        renderMatch();
        startPolling(state.match);
      }
    } catch {
      // The next polling pass retries without disturbing the current board.
    }
  }

  function chessPosition(fen) {
    const board = {};
    const ranks = String(fen || "").split(" ")[0].split("/");
    ranks.forEach((rank, row) => {
      let file = 0;
      for (const token of rank) {
        if (/\d/.test(token)) file += Number(token);
        else {
          board[`${"abcdefgh"[file]}${8 - row}`] = token;
          file += 1;
        }
      }
    });
    return board;
  }

  function chessPiece(token) {
    if (!token || !pieceKinds[token]) return null;
    return icon(
      `piece-${pieceKinds[token]}`,
      `piece-svg ${token === token.toUpperCase() ? "white" : "black"}`,
    );
  }

  function renderChess() {
    const match = state.match;
    const boardNode = byId("chess-board");
    const position = chessPosition(match.state.fen);
    const legal = match.legal_moves || [];
    boardNode.hidden = false;
    byId("backgammon-board").hidden = true;
    boardNode.classList.toggle("locked", !match.can_move || state.busy);
    boardNode.replaceChildren();

    const orientation = match.viewer_side === "black" ? "black" : "white";
    const ranks = orientation === "white" ? [8, 7, 6, 5, 4, 3, 2, 1] : [1, 2, 3, 4, 5, 6, 7, 8];
    const files = orientation === "white" ? [..."abcdefgh"] : [..."hgfedcba"];
    const legalDestinations = state.selected
      ? legal.filter((move) => move.startsWith(state.selected)).map((move) => move.slice(2, 4))
      : [];

    for (const rank of ranks) {
      for (const file of files) {
        const square = `${file}${rank}`;
        const token = position[square];
        const button = element(
          "button",
          `chess-square ${(Math.abs("abcdefgh".indexOf(file) - rank) % 2) ? "dark" : "light"}`,
        );
        button.type = "button";
        button.dataset.square = square;
        button.dataset.file = file;
        button.dataset.rank = String(rank);
        button.dataset.showFile = String(rank === ranks[ranks.length - 1]);
        button.dataset.showRank = String(file === files[0]);
        button.setAttribute("role", "gridcell");
        button.setAttribute("aria-label", `${square}${token ? `, ${pieceLabels[token]}` : ", пустая клетка"}`);
        if (token) button.append(chessPiece(token));
        if (state.selected === square) button.classList.add("selected");
        if (legalDestinations.includes(square)) button.classList.add(token ? "capture" : "legal");
        if (String(match.state.last_move || "").slice(0, 4).includes(square)) button.classList.add("last");
        const actionable = legal.some((move) => move.startsWith(square)) || legalDestinations.includes(square);
        button.disabled = state.busy || !match.can_move || !actionable;
        button.addEventListener("click", () => chessClick(square, position));
        boardNode.append(button);
      }
    }
  }

  function choosePromotion(candidates) {
    state.pendingPromotion = candidates;
    const options = byId("promotion-options");
    const side = state.match.viewer_side || "white";
    options.replaceChildren();
    for (const suffix of ["q", "r", "b", "n"]) {
      const move = candidates.find((candidate) => candidate.endsWith(suffix));
      if (!move) continue;
      const button = element("button", "promotion-option");
      button.type = "button";
      button.setAttribute("aria-label", promotionNames[suffix]);
      const piece = icon(`piece-${promotionKinds[suffix]}`, `piece-svg ${side}`);
      button.append(piece);
      button.addEventListener("click", () => {
        state.pendingPromotion = null;
        closeDialog(byId("promotion-dialog"));
        void sendMove({ move });
      });
      options.append(button);
    }
    openDialog(byId("promotion-dialog"));
  }

  function chessClick(square, position) {
    const match = state.match;
    const legal = match.legal_moves || [];
    if (!match.can_move || state.busy) return;
    if (state.selected) {
      const candidates = legal.filter((move) => move.startsWith(state.selected + square));
      if (candidates.length === 1) {
        state.selected = null;
        void sendMove({ move: candidates[0] });
        return;
      }
      if (candidates.length > 1) {
        state.selected = null;
        choosePromotion(candidates);
        return;
      }
    }
    const token = position[square];
    const owns = token && (
      match.viewer_side === "white"
        ? token === token.toUpperCase()
        : token === token.toLowerCase()
    );
    state.selected = owns && legal.some((move) => move.startsWith(square)) ? square : null;
    renderChess();
  }

  function checkerStack(count, side, bottom = false) {
    const stack = element("span", "bg-stack");
    const total = Math.abs(Number(count) || 0);
    const shown = Math.min(total, 5);
    for (let index = 0; index < shown; index += 1) {
      stack.append(element("i", `checker${side === "black" ? " black" : ""}`));
    }
    if (total > 5) stack.append(element("b", "bg-count", total));
    if (bottom) stack.dataset.bottom = "true";
    return stack;
  }

  function pointDescription(index, value) {
    const count = Math.abs(Number(value) || 0);
    if (!count) return `Пункт ${index + 1}, пусто`;
    const side = Number(value) > 0 ? "белых" : "чёрных";
    return `Пункт ${index + 1}, ${count} ${side}`;
  }

  function makeBackgammonPoint(index, position, legalDestinations, legalSources) {
    const match = state.match;
    const value = Number(match.state.board?.[index] || 0);
    const button = element("button", `bg-point ${position}${index % 2 ? " alt" : ""}`);
    button.type = "button";
    button.dataset.point = String(index);
    button.setAttribute("aria-label", pointDescription(index, value));
    button.append(checkerStack(value, value > 0 ? "white" : "black", position === "bottom"));
    if (String(state.selected) === String(index)) button.classList.add("selected");
    if (legalDestinations.includes(String(index))) button.classList.add("legal");
    const actionable = legalSources.has(String(index)) || legalDestinations.includes(String(index));
    button.disabled = state.busy || !match.can_move || !actionable;
    button.addEventListener("click", () => backgammonClick(index));
    return button;
  }

  function sideTray(kind, side, legalDestinations, legalSources) {
    const match = state.match;
    const viewerSide = match.viewer_side;
    const count = Number(match.state[kind]?.[side] || 0);
    const label = kind === "bar" ? "Бар" : "Снято";
    const button = element("button", "bg-tray");
    button.type = "button";
    button.dataset.tray = kind;
    button.dataset.side = side;
    button.setAttribute("aria-label", `${label}: ${count}, ${sideLabels[side].toLowerCase()}`);
    button.append(element("small", "", label), checkerStack(count, side, side === viewerSide));
    if (side === viewerSide && String(state.selected) === kind) button.classList.add("selected");
    if (side === viewerSide && legalDestinations.includes(kind)) button.classList.add("legal");
    const actionable = (
      (kind === "bar" && legalSources.has("bar"))
      || (kind === "off" && legalDestinations.includes("off"))
    );
    button.disabled = state.busy || !match.can_move || side !== viewerSide || !actionable;
    if (side === viewerSide) button.addEventListener("click", () => backgammonClick(kind));
    return button;
  }

  function renderBackgammon() {
    const match = state.match;
    const root = byId("backgammon-board");
    const legal = match.legal_moves || [];
    const orientation = match.viewer_side === "black" ? "black" : "white";
    const topSide = orientation === "white" ? "black" : "white";
    const bottomSide = opposite(topSide);
    const legalDestinations = state.selected !== null
      ? legal.filter((move) => String(move.from) === String(state.selected)).map((move) => String(move.to))
      : [];
    const legalSources = new Set(legal.map((move) => String(move.from)));

    const layout = orientation === "white"
      ? {
          topLeft: [12, 13, 14, 15, 16, 17],
          topRight: [18, 19, 20, 21, 22, 23],
          bottomLeft: [11, 10, 9, 8, 7, 6],
          bottomRight: [5, 4, 3, 2, 1, 0],
        }
      : {
          topLeft: [0, 1, 2, 3, 4, 5],
          topRight: [6, 7, 8, 9, 10, 11],
          bottomLeft: [23, 22, 21, 20, 19, 18],
          bottomRight: [17, 16, 15, 14, 13, 12],
        };

    root.hidden = false;
    byId("chess-board").hidden = true;
    root.replaceChildren();

    for (const [name, indices] of Object.entries(layout)) {
      const position = name.startsWith("top") ? "top" : "bottom";
      const quadrant = element("div", `bg-quadrant ${name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`);
      for (const index of indices) {
        quadrant.append(makeBackgammonPoint(index, position, legalDestinations, legalSources));
      }
      root.append(quadrant);
    }

    const bar = element("div", "bg-bar-rail");
    bar.append(
      sideTray("bar", topSide, legalDestinations, legalSources),
      sideTray("bar", bottomSide, legalDestinations, legalSources),
    );
    const off = element("div", "bg-off-rail");
    off.append(
      sideTray("off", topSide, legalDestinations, legalSources),
      sideTray("off", bottomSide, legalDestinations, legalSources),
    );
    root.append(bar, off);
  }

  function backgammonClick(point) {
    const match = state.match;
    const legal = match.legal_moves || [];
    if (!match.can_move || state.busy) return;
    if (state.selected !== null) {
      const candidate = legal.find(
        (move) => String(move.from) === String(state.selected) && String(move.to) === String(point),
      );
      if (candidate) {
        const source = state.selected;
        state.selected = null;
        void sendMove({ from: source, to: point, die: candidate.die });
        return;
      }
    }
    state.selected = legal.some((move) => String(move.from) === String(point)) ? point : null;
    renderBackgammon();
  }

  function dieFace(value) {
    const die = element("span", "die");
    die.setAttribute("aria-label", `На кости ${value}`);
    for (const position of dieDots[Number(value)] || []) die.append(element("i", `p${position}`));
    return die;
  }

  async function sendMove(payload) {
    if (state.busy || !state.match) return;
    state.busy = true;
    state.thinking = state.match.mode === "bot";
    renderMatch();
    try {
      const data = await api(`/api/games/matches/${encodeURIComponent(state.match.id)}/command`, {
        method: "POST",
        body: JSON.stringify({ action: "move", version: state.match.version, ...payload }),
        timeout: 20000,
      });
      state.match = data.match;
      state.selected = null;
      startPolling(state.match);
    } catch (error) {
      toast(error.message, true);
      await refreshMatch();
    } finally {
      state.busy = false;
      state.thinking = false;
      renderMatch();
    }
  }

  function renderHistory(match) {
    const history = match.state.history || [];
    const container = byId("move-history");
    container.replaceChildren();
    if (!history.length) {
      container.append(element("div", "history-empty", "Первый ход ещё впереди"));
      return;
    }

    history.slice(-24).map((move, localIndex) => ({ move, index: history.length - Math.min(history.length, 24) + localIndex }))
      .reverse()
      .forEach(({ move, index }) => {
        const row = element("div", "move-item");
        if (typeof move === "string") {
          const side = index % 2 === 0 ? "Белые" : "Чёрные";
          row.append(
            element("b", "", `${Math.floor(index / 2) + 1}${index % 2 === 0 ? "." : "…"}`),
            element("span", "", move),
            element("small", "", side),
          );
        } else {
          const from = move.from === "bar" ? "бар" : Number(move.from) + 1;
          const to = move.to === "off" ? "снято" : Number(move.to) + 1;
          row.append(
            element("b", "", `#${index + 1}`),
            element("span", "", `${from} → ${to}`),
            element("small", "", `${sideLabels[move.side]} · ${move.die}`),
          );
        }
        container.append(row);
      });
  }

  function renderSeats(match) {
    const { bottom, top } = boardSides(match);
    const bottomPlayer = match.players?.[bottom] || {};
    const topPlayer = match.players?.[top] || {};

    byId("opponent-label").textContent = match.spectator ? `${sideLabels[top].toUpperCase()} · НАБЛЮДЕНИЕ` : "СОПЕРНИК";
    byId("opponent-name").textContent = topPlayer.name || "Ожидаем игрока";
    byId("opponent-avatar").textContent = topPlayer.bot ? "T" : String(topPlayer.name || "?").charAt(0).toUpperCase();
    byId("opponent-clock").textContent = sideLabels[top].toUpperCase();

    byId("self-label").textContent = match.spectator ? `${sideLabels[bottom].toUpperCase()} · НАБЛЮДЕНИЕ` : "ВАША СТОРОНА";
    byId("self-name").textContent = bottomPlayer.name || state.viewer?.name || "Наблюдатель";
    byId("self-avatar").textContent = bottomPlayer.bot ? "T" : String(bottomPlayer.name || "В").charAt(0).toUpperCase();
    byId("turn-state").textContent = match.spectator
      ? "НАБЛЮДЕНИЕ"
      : match.can_move
        ? "ВАШ ХОД"
        : match.status === "finished"
          ? "ФИНИШ"
          : "ОЖИДАНИЕ";
    byId("turn-state").classList.toggle("active", match.can_move && !state.thinking);
  }

  function detailLabel(match) {
    if (state.thinking && match.mode === "bot") return "Бот анализирует позицию и готовит ответный ход.";
    if (match.status === "waiting") return "Комната готова. Место второго игрока зарезервировано по ссылке.";
    if (match.status === "finished") {
      if (match.result === "draw") return "Соперники завершили партию вничью.";
      return match.winner_side ? `Победили ${sideLabels[match.winner_side].toLowerCase()}.` : "Партия завершена.";
    }
    if (match.spectator) return `Сейчас ходят ${sideLabels[match.turn_side].toLowerCase()}. Позиция обновляется автоматически.`;
    return match.can_move ? "Выберите фигуру или шашку — доступные ходы подсветятся." : "Ожидаем ответ соперника. Позиция обновится автоматически.";
  }

  function renderMatch() {
    const match = state.match;
    if (!match) return;
    const status = statusLabel(match);
    const orbit = byId("match-status").closest(".panel-status").querySelector(".status-orbit");

    byId("match-kicker").textContent = `T-MOD ${match.game_type === "chess" ? "CHESS" : "BACKGAMMON"} · ${String(match.id).slice(0, 8).toUpperCase()}`;
    byId("match-title").textContent = matchLabel(match);
    byId("match-status").textContent = status;
    byId("match-detail").textContent = detailLabel(match);
    orbit.className = `status-orbit${match.status === "finished" ? " finished" : (!match.can_move || state.thinking) ? " waiting" : ""}`;

    renderSeats(match);
    byId("waiting-overlay").hidden = match.status !== "waiting";
    byId("thinking-overlay").hidden = !state.thinking;
    byId("join-panel").hidden = !match.can_join;
    byId("resign-match").hidden = !match.viewer_side || match.status !== "active";
    byId("match-mode").textContent = match.mode === "bot" ? `T-Mod Bot · ${match.bot_level}/3` : "Матч по приглашению";
    byId("match-side").textContent = match.spectator ? "Наблюдатель" : sideLabels[match.viewer_side] || "—";

    const dicePanel = byId("dice-panel");
    dicePanel.hidden = match.game_type !== "backgammon";
    byId("dice-list").replaceChildren(...(match.state.dice || []).map(dieFace));
    renderHistory(match);
    if (match.game_type === "chess") renderChess();
    else renderBackgammon();
  }

  async function joinMatch() {
    if (!state.match || state.busy) return;
    state.busy = true;
    try {
      const data = await api(`/api/games/matches/${encodeURIComponent(state.match.id)}/command`, {
        method: "POST",
        body: JSON.stringify({ action: "join" }),
      });
      state.match = data.match;
      state.selected = null;
      renderMatch();
      startPolling(state.match);
      toast("Вы за игровым столом. Хорошей партии!");
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
      if (state.match) renderMatch();
    }
  }

  async function resign() {
    if (!state.match || state.busy) return;
    state.busy = true;
    try {
      const data = await api(`/api/games/matches/${encodeURIComponent(state.match.id)}/command`, {
        method: "POST",
        body: JSON.stringify({ action: "resign", version: state.match.version }),
      });
      state.match = data.match;
      closeDialog(byId("confirm-game-dialog"));
      renderMatch();
      startPolling(state.match);
      toast("Партия завершена.");
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy = false;
    }
  }

  function fallbackCopy(text) {
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    return copied;
  }

  async function copyInvite() {
    if (!state.match) return;
    const url = `${location.origin}/games/${state.match.id}`;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(url);
      else if (!fallbackCopy(url)) throw new Error("copy_failed");
      toast("Ссылка на игру скопирована.");
    } catch {
      toast("Не удалось скопировать ссылку. Откройте страницу по HTTPS.", true);
    }
  }

  function bind() {
    byId("new-match-top").addEventListener("click", () => openCreate());
    byId("new-match-hero").addEventListener("click", () => openCreate());
    document.querySelectorAll("[data-quick-game]").forEach((button) => {
      button.addEventListener("click", () => openCreate(button.dataset.quickGame));
    });
    byId("create-form").addEventListener("submit", createMatch);
    byId("create-form").querySelectorAll('input[name="mode"]').forEach((input) => {
      input.addEventListener("change", syncBotLevelVisibility);
    });
    document.querySelectorAll("[data-close]").forEach((button) => {
      button.addEventListener("click", () => closeDialog(button.closest("dialog")));
    });
    document.querySelectorAll("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) closeDialog(dialog);
      });
    });
    byId("back-lobby").addEventListener("click", showLobby);
    byId("share-match").addEventListener("click", copyInvite);
    byId("waiting-copy").addEventListener("click", copyInvite);
    byId("join-match").addEventListener("click", joinMatch);
    byId("resign-match").addEventListener("click", () => openDialog(byId("confirm-game-dialog")));
    byId("confirm-resign").addEventListener("click", resign);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && state.match && !state.busy) void refreshMatch();
    });
    globalThis.addEventListener("pagehide", () => clearInterval(state.timer));
  }

  async function boot() {
    bind();
    const pending = sessionStorage.getItem("tmod-pending-game");
    if (pending && location.pathname === "/games") {
      sessionStorage.removeItem("tmod-pending-game");
      history.replaceState(null, "", pending);
    }
    const id = location.pathname.split("/").filter(Boolean)[1];
    if (id) {
      try {
        const data = await api(`/api/games/matches/${encodeURIComponent(id)}`);
        state.viewer = data.viewer;
        state.csrf = data.viewer.csrf_token;
        await enterMatch(data.match);
      } catch (error) {
        if (error.message !== "Требуется вход") {
          toast(error.message, true);
          await loadLobby();
        }
      }
    } else {
      await loadLobby();
    }
  }

  void boot();
})();
