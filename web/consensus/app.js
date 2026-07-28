"use strict";

const byId = (id) => document.getElementById(id);
const login = byId("login");
const dashboard = byId("dashboard");
const loginForm = byId("login-form");
const loginError = byId("login-error");
const tokenInput = byId("token");
const connectionDot = byId("connection-dot");
const connectionText = byId("connection-text");
const BLOCK_STATES = {
  yes: ["за", "yes"],
  no: ["против", "no"],
  abstain: ["воздержался", "abstain"],
  inactive: ["неактивен", "inactive"],
  hidden: ["скрыто", "hidden"],
  pending: ["ожидание", "pending"],
};

let token = sessionStorage.getItem("t-consensus-token") || "";
let selectedMode = new URLSearchParams(window.location.search).get("mode")
  || sessionStorage.getItem("t-consensus-mode")
  || "";
let state = null;
let pollTimer = null;
let fetching = false;
let commanding = false;
let messageTimer = null;

function text(id, value) {
  byId(id).textContent = String(value ?? "—");
}

function setConnection(mode, label) {
  connectionDot.className = `connection-dot ${mode}`;
  connectionText.textContent = label;
}

function clearNode(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function formatNumber(value) {
  return String(Number(value || 0)).padStart(3, "0");
}

function formatTimer(deadline) {
  if (!deadline) return "—";
  const seconds = Math.max(
    0,
    Math.floor((new Date(deadline).getTime() - Date.now()) / 1000),
  );
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

function showCommandMessage(message, kind = "success") {
  const node = byId("command-message");
  node.textContent = message;
  node.className = `command-message ${kind}`;
  node.hidden = false;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => {
    node.hidden = true;
  }, 7000);
}

function renderBlocks(blocks = {}) {
  document.querySelectorAll(".block-card").forEach((card) => {
    const stateName = blocks[card.dataset.block] || "pending";
    const [label, className] = BLOCK_STATES[stateName] || BLOCK_STATES.pending;
    card.classList.remove("yes", "no", "abstain", "inactive", "hidden", "pending");
    card.classList.add(className);
    card.querySelector(".block-state").textContent = label;
  });
}

function renderParticipants(participants = []) {
  const container = byId("participants");
  clearNode(container);
  text("participants-count", participants.length);
  if (!participants.length) {
    container.className = "participants empty-state";
    container.textContent = "Нет активной регистрации";
    return;
  }
  container.className = "participants";
  participants.forEach((participant) => {
    const row = document.createElement("div");
    row.className = "participant";
    const dot = document.createElement("span");
    dot.className = `participant-dot${participant.confirmed ? " ready" : ""}`;
    const identity = document.createElement("div");
    const name = document.createElement("strong");
    const role = document.createElement("small");
    name.textContent = participant.name;
    role.textContent = participant.confirmed
      ? participant.kind
      : `${participant.kind} · ожидает подтверждение`;
    identity.append(name, role);
    const vote = document.createElement("span");
    vote.className = `vote-state${participant.voted ? " done" : ""}`;
    vote.textContent = participant.voted ? "голос принят" : "ожидание";
    row.append(dot, identity, vote);
    container.append(row);
  });
}

function renderList(containerId, items, kind) {
  const container = byId(containerId);
  clearNode(container);
  if (!items.length) {
    container.className = "stack-list empty-state";
    container.textContent = kind === "queue" ? "Очередь пуста" : "Решений пока нет";
    return;
  }
  container.className = "stack-list";
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "list-row";
    const number = document.createElement("span");
    number.className = "list-number";
    number.textContent = `№${formatNumber(item.bill_number)}`;
    const title = item.source_url
      ? document.createElement("a")
      : document.createElement("span");
    title.className = "list-title";
    title.textContent = item.title || "Без названия";
    if (item.source_url) {
      title.href = item.source_url;
      title.target = "_blank";
      title.rel = "noreferrer";
    }
    const meta = document.createElement("span");
    const status = String(item.status || "");
    meta.className = `list-meta ${status}`;
    meta.textContent = kind === "queue"
      ? "в очереди"
      : ({ accepted: "принят", rejected: "отклонён", vetoed: "вето" }[status] || status);
    row.append(number, title, meta);
    container.append(row);
  });
}

function renderMode(data) {
  const available = new Set(data.available_modes || ["live"]);
  selectedMode = data.mode || "live";
  sessionStorage.setItem("t-consensus-mode", selectedMode);
  document.querySelectorAll("#mode-switch button").forEach((button) => {
    const mode = button.dataset.mode;
    button.disabled = !available.has(mode);
    button.classList.toggle("active", mode === selectedMode);
    button.setAttribute("aria-pressed", String(mode === selectedMode));
  });
  const simulated = selectedMode === "simulation";
  byId("simulation-banner").hidden = !simulated;
  dashboard.classList.toggle("simulation-mode", simulated);
}

function renderViewer(data) {
  const viewer = data.viewer || {};
  const chip = byId("viewer-chip");
  chip.hidden = !viewer.authenticated && !viewer.legacy_read_only;
  if (chip.hidden) return;
  text("viewer-name", viewer.name || "Наблюдатель");
  const role = viewer.leader
    ? "ведущий"
    : viewer.chair
      ? "председатель"
      : viewer.legacy_read_only
        ? "только просмотр"
        : "наблюдатель";
  text("viewer-role", role);
}

function actionButton(label, action, payload = {}, options = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `control-button ${options.kind || "secondary"}`;
  button.textContent = label;
  button.disabled = commanding;
  button.addEventListener("click", async () => {
    let commandPayload = { ...payload };
    if (options.confirm && !window.confirm(options.confirm)) return;
    if (options.reason) {
      const reason = window.prompt(
        "Причина паузы:",
        "Консенсус приостановлен ведущим.",
      );
      if (reason === null) return;
      commandPayload.reason = reason;
    }
    if (options.currentRoster) {
      const unconfirmed = state?.session?.participants
        ?.filter((participant) => !participant.confirmed)
        .map((participant) => participant.name) || [];
      if (unconfirmed.length) {
        const accepted = window.confirm(
          `Не подтвердились: ${unconfirmed.join(", ")}.\n\nНачать текущим составом?`,
        );
        if (!accepted) return;
        commandPayload.confirm_current_roster = true;
      }
    }
    await sendCommand(action, commandPayload);
  });
  return button;
}

function controlGroup(title, description = "") {
  const group = document.createElement("div");
  group.className = "control-group";
  const heading = document.createElement("div");
  const strong = document.createElement("strong");
  const small = document.createElement("small");
  strong.textContent = title;
  small.textContent = description;
  heading.append(strong, small);
  const actions = document.createElement("div");
  actions.className = "control-actions";
  group.append(heading, actions);
  return { group, actions };
}

function renderControls(data) {
  const panel = byId("operator-panel");
  const container = byId("operator-controls");
  clearNode(container);
  const capabilities = new Set(data.capabilities || []);
  const viewer = data.viewer || {};
  const session = data.session;
  panel.hidden = !viewer.authenticated && capabilities.size === 0;
  if (panel.hidden) return;

  if (!capabilities.size) {
    text("operator-eyebrow", "РЕЖИМ НАБЛЮДЕНИЯ");
    text("operator-title", "Прямой экран заседания");
    text(
      "operator-description",
      "Панель показывает актуальный этап, законопроект и ход заседания. Управляющие действия доступны только текущему ведущему.",
    );
    text("operator-lock", "без права изменения");
    const note = document.createElement("div");
    note.className = "observer-note";
    note.textContent = "Данные обновляются каждую секунду. Голоса не раскрываются до фиксации результата.";
    container.append(note);
    return;
  }

  text("operator-eyebrow", selectedMode === "simulation" ? "УЧЕБНЫЙ ПУЛЬТ" : "ПУЛЬТ ВЕДУЩЕГО");
  text("operator-title", session ? `Этап: ${session.stage_label}` : "Подготовка заседания");
  text(
    "operator-description",
    selectedMode === "simulation"
      ? "Команды управляют изолированной симуляцией и не затрагивают рабочую базу."
      : "Команды проходят через тот же координатор и сразу отражаются в Discord.",
  );
  text("operator-lock", `доступ: ${viewer.name || "ведущий"}`);

  if (capabilities.has("open_registration")) {
    const { group, actions } = controlGroup(
      "Новое заседание",
      "Система повторно проверит очередь, голосовой канал и кворум.",
    );
    actions.append(actionButton("Открыть регистрацию", "open_registration", {}, { kind: "primary" }));
    container.append(group);
  }

  if (capabilities.has("confirm_next") || capabilities.has("confirm_all")) {
    const { group, actions } = controlGroup("Учебная регистрация", "Подтверждения фейковых участников");
    if (capabilities.has("confirm_next")) actions.append(actionButton("Следующее подтверждение", "confirm_next"));
    if (capabilities.has("confirm_all")) actions.append(actionButton("Подтвердить всех", "confirm_all", {}, { kind: "primary" }));
    container.append(group);
  }

  if (capabilities.has("start_vote") || capabilities.has("resend_invitations")) {
    const { group, actions } = controlGroup("Регистрация", "Приглашения и переход к первому проекту");
    if (capabilities.has("start_vote")) {
      actions.append(actionButton("Начать голосование", "start_vote", {}, { kind: "primary", currentRoster: true }));
    }
    if (capabilities.has("resend_invitations")) {
      actions.append(actionButton("Повторить приглашения", "resend_invitations"));
    }
    container.append(group);
  }

  if (capabilities.has("leader_vote")) {
    const { group, actions } = controlGroup("Голос ведущего", "Можно изменить до фиксации результата");
    actions.append(
      actionButton("За", "leader_vote", { vote: "yes" }, { kind: "positive" }),
      actionButton("Против", "leader_vote", { vote: "no" }, { kind: "danger" }),
      actionButton("Воздержаться", "leader_vote", { vote: "abstain" }),
    );
    container.append(group);
  }

  if (capabilities.has("set_timer")) {
    const { group, actions } = controlGroup("Таймер", "Автоматическая фиксация по истечении времени");
    [
      ["30 сек", 30],
      ["1 мин", 60],
      ["3 мин", 180],
      ["5 мин", 300],
    ].forEach(([label, seconds]) => {
      actions.append(actionButton(label, "set_timer", { seconds }));
    });
    container.append(group);
  }

  if (capabilities.has("fake_vote") || capabilities.has("fake_scenario")) {
    const { group, actions } = controlGroup("Фейковые участники", "Проверка маршрутов симулятора");
    if (capabilities.has("fake_vote")) actions.append(actionButton("Следующий голос", "fake_vote"));
    if (capabilities.has("fake_scenario")) {
      actions.append(
        actionButton("Все за", "fake_scenario", { scenario: "accepted" }, { kind: "positive" }),
        actionButton("Спорный", "fake_scenario", { scenario: "mixed" }),
        actionButton("Все против", "fake_scenario", { scenario: "rejected" }, { kind: "danger" }),
      );
    }
    container.append(group);
  }

  if (capabilities.has("request_discussion")) {
    const { group, actions } = controlGroup("Дискуссия", "Учебный запрос дискуссии");
    actions.append(actionButton("Запросить дискуссию", "request_discussion"));
    container.append(group);
  }

  if (capabilities.has("choose_discussion")) {
    const { group, actions } = controlGroup("Тип дискуссии", "Будет создан штатный канал Discord");
    ["Правовая", "Фактическая", "Процедурная", "Иная"].forEach((discussionType) => {
      actions.append(actionButton(discussionType, "choose_discussion", { discussion_type: discussionType }));
    });
    container.append(group);
  }

  if (capabilities.has("oral_result")) {
    const { group, actions } = controlGroup("Учебный устный итог", "Используется только в симуляции");
    actions.append(
      actionButton("Устно принято", "oral_result", { status: "accepted" }, { kind: "positive" }),
      actionButton("Устно отклонено", "oral_result", { status: "rejected" }, { kind: "danger" }),
    );
    container.append(group);
  }

  const lifecycle = controlGroup("Управление этапом", "Безопасные переходы текущего заседания");
  if (capabilities.has("finalize_vote")) {
    lifecycle.actions.append(actionButton(
      "Завершить голосование",
      "finalize_vote",
      { confirm: true },
      { confirm: "Зафиксировать текущий результат досрочно?", kind: "primary" },
    ));
  }
  if (capabilities.has("retry_finalization")) {
    lifecycle.actions.append(actionButton("Повторить фиксацию", "retry_finalization", {}, { kind: "primary" }));
  }
  if (capabilities.has("end_discussion")) {
    lifecycle.actions.append(actionButton("Завершить дискуссию", "end_discussion", {}, { kind: "positive" }));
  }
  if (capabilities.has("pause")) {
    lifecycle.actions.append(actionButton("Пауза", "pause", {}, { reason: true }));
  }
  if (capabilities.has("resume")) {
    lifecycle.actions.append(actionButton("Продолжить", "resume", {}, { kind: "positive" }));
  }
  if (capabilities.has("next_bill")) {
    lifecycle.actions.append(actionButton("Следующий проект", "next_bill", {}, { kind: "primary" }));
  }
  if (capabilities.has("veto")) {
    lifecycle.actions.append(actionButton(
      "Применить вето",
      "veto",
      { confirm: true },
      { confirm: "Применить право вето к текущему проекту?", kind: "danger" },
    ));
  }
  if (capabilities.has("cancel_session")) {
    lifecycle.actions.append(actionButton(
      "Отменить регистрацию",
      "cancel_session",
      { confirm: true },
      { confirm: "Отменить заседание без итогового протокола?", kind: "danger" },
    ));
  }
  if (capabilities.has("finish_session")) {
    lifecycle.actions.append(actionButton(
      "Завершить заседание",
      "finish_session",
      { confirm: true },
      { confirm: "Завершить всё заседание и сформировать итоговый протокол?", kind: "danger" },
    ));
  }
  if (lifecycle.actions.childElementCount) container.append(lifecycle.group);
}

function render(data) {
  state = data;
  renderMode(data);
  renderViewer(data);
  renderControls(data);
  const session = data.session;
  const active = Boolean(data.active && session);
  text("queue-value", data.queue.length);
  renderList("queue-list", data.queue, "queue");
  renderList(
    "results-list",
    session?.results?.length ? session.results : data.recent_results,
    "results",
  );
  text("updated-at", `обновлено ${new Date(data.updated_at).toLocaleTimeString("ru-RU")}`);

  if (!session) {
    text("session-eyebrow", selectedMode === "simulation" ? "СИМУЛЯТОР ГОТОВ" : "СИСТЕМА ГОТОВА");
    text("session-title", selectedMode === "simulation" ? "Симуляция не запущена" : "Консенсус не проводится");
    text(
      "session-subtitle",
      selectedMode === "simulation"
        ? "Запустите симулятор в панели администрирования Discord."
        : "Панель ожидает начало следующего пленарного заседания.",
    );
    text("stage-badge", "Ожидание");
    byId("stage-badge").className = "stage-badge idle";
    text("quorum-value", "—");
    text("quorum-detail", "нет активной сессии");
    text("votes-value", "—");
    text("timer-value", "—");
    text("timer-detail", "таймер не запущен");
    text("bill-title", "Проект не выбран");
    text("bill-number", "—");
    byId("bill-link").hidden = true;
    byId("discussion-link").hidden = true;
    renderBlocks({});
    renderParticipants([]);
    return;
  }

  text(
    "session-eyebrow",
    selectedMode === "simulation"
      ? "УЧЕБНЫЙ КОНСЕНСУС · СИНХРОНИЗИРОВАН С DISCORD"
      : `ПЛЕНАРНЫЙ КОНСЕНСУС · ${session.plenary_number}`,
  );
  text(
    "session-title",
    active
      ? selectedMode === "simulation" ? "Симуляция в процессе" : "Заседание в процессе"
      : selectedMode === "simulation" ? "Симуляция завершена" : "Заседание завершено",
  );
  const stageContext = session.pause_reason
    ? ` · ${session.pause_reason}`
    : session.discussion?.type
      ? ` · ${session.discussion.type} дискуссия`
      : "";
  text("session-subtitle", `Ведущий: ${session.leader.name} · обновление в реальном времени${stageContext}`);
  text("stage-badge", session.stage_label);
  byId("stage-badge").className = `stage-badge${active ? "" : " idle"}`;
  text("quorum-value", `${session.quorum.confirmed}/${session.quorum.invited}`);
  text(
    "quorum-detail",
    session.quorum.ready
      ? `кворум подтверждён · ${session.quorum.percent}%`
      : `кворум ещё не собран · ${session.quorum.percent}%`,
  );
  text("votes-value", `${session.voting.received}/${session.voting.expected}`);
  text("votes-detail", "направления скрыты до результата");
  text("timer-value", formatTimer(session.timer_deadline));
  text("timer-detail", session.timer_deadline ? "до автоматической фиксации" : "таймер не запущен");

  const bill = session.current_bill;
  text("bill-title", bill?.title || "Между законопроектами");
  text("bill-number", bill ? `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}` : "ПРОЕКТ НЕ ВЫБРАН");
  const billLink = byId("bill-link");
  billLink.hidden = !bill?.source_url;
  if (bill?.source_url) billLink.href = bill.source_url;
  const discussionLink = byId("discussion-link");
  discussionLink.hidden = !session.discussion?.channel_url;
  if (session.discussion?.channel_url) {
    discussionLink.href = session.discussion.channel_url;
  }
  renderBlocks(session.blocks);
  renderParticipants(session.participants);
}

function authHeaders() {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function fetchState({ first = false } = {}) {
  if (fetching || commanding) return;
  fetching = true;
  try {
    const query = selectedMode ? `?mode=${encodeURIComponent(selectedMode)}` : "";
    const response = await fetch(`/api/state${query}`, {
      headers: authHeaders(),
      credentials: "same-origin",
      cache: "no-store",
    });
    if (response.status === 401) {
      sessionStorage.removeItem("t-consensus-token");
      token = "";
      login.hidden = false;
      dashboard.hidden = true;
      if (first) loginError.textContent = "Откройте персональную ссылку в Discord.";
      setConnection("offline", "требуется вход");
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    login.hidden = true;
    dashboard.hidden = false;
    loginError.textContent = "";
    setConnection("online", "обновляется");
  } catch (error) {
    setConnection("offline", "связь потеряна");
    if (first) loginError.textContent = "Панель недоступна. Проверьте контейнер и Cloudflare Tunnel.";
  } finally {
    fetching = false;
  }
}

async function sendCommand(action, payload = {}) {
  if (!state?.viewer?.authenticated || commanding) return;
  commanding = true;
  renderControls(state);
  setConnection("", "выполняется");
  const session = state.session;
  const idempotencyKey = window.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}-command`;
  try {
    const response = await fetch("/api/command", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.viewer.csrf_token || "",
        "X-Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        mode: selectedMode,
        action,
        session_key: session?.key || "",
        revision: session?.revision || 0,
        bill_id: session?.current_bill?.id ?? null,
        payload,
      }),
    });
    const result = await response.json();
    if (!response.ok) {
      showCommandMessage(result.message || "Команда не выполнена.", "error");
      if (response.status === 401 || response.status === 403) await fetchState();
      else setTimeout(fetchState, 150);
      return;
    }
    render(result.state);
    showCommandMessage(result.message || "Команда выполнена.");
    setConnection("online", "обновляется");
  } catch (error) {
    showCommandMessage("Связь прервалась. Состояние будет проверено автоматически.", "error");
    setConnection("offline", "проверка состояния");
    setTimeout(fetchState, 500);
  } finally {
    commanding = false;
    if (state) renderControls(state);
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  token = tokenInput.value.trim();
  sessionStorage.setItem("t-consensus-token", token);
  await fetchState({ first: true });
});

document.querySelectorAll("#mode-switch button").forEach((button) => {
  button.addEventListener("click", async () => {
    if (button.disabled || button.dataset.mode === selectedMode) return;
    selectedMode = button.dataset.mode || "live";
    sessionStorage.setItem("t-consensus-mode", selectedMode);
    await fetchState();
  });
});

setInterval(() => {
  text("clock", new Date().toLocaleTimeString("ru-RU"));
  if (state?.session) text("timer-value", formatTimer(state.session.timer_deadline));
}, 250);

fetchState({ first: true });
pollTimer = setInterval(fetchState, 1000);
window.addEventListener("beforeunload", () => clearInterval(pollTimer));
