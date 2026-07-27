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
let state = null;
let timer = null;
let fetching = false;

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
  const seconds = Math.max(0, Math.floor((new Date(deadline).getTime() - Date.now()) / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  return hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
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
    const title = item.source_url ? document.createElement("a") : document.createElement("span");
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

function render(data) {
  state = data;
  const session = data.session;
  const active = Boolean(data.active && session);
  text("queue-value", data.queue.length);
  renderList("queue-list", data.queue, "queue");
  renderList("results-list", data.recent_results, "results");
  text("updated-at", `обновлено ${new Date(data.updated_at).toLocaleTimeString("ru-RU")}`);

  if (!session) {
    text("session-eyebrow", "СИСТЕМА ГОТОВА");
    text("session-title", "Консенсус не проводится");
    text("session-subtitle", "Панель ожидает начало следующего пленарного заседания.");
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
    renderBlocks({});
    renderParticipants([]);
    return;
  }

  text("session-eyebrow", `ПЛЕНАРНЫЙ КОНСЕНСУС · ${session.plenary_number}`);
  text("session-title", active ? "Заседание в процессе" : "Заседание завершено");
  text("session-subtitle", `Ведущий: ${session.leader.name} · обновление в реальном времени`);
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
  renderBlocks(session.blocks);
  renderParticipants(session.participants);
}

async function fetchState({ first = false } = {}) {
  if (!token || fetching) return;
  fetching = true;
  try {
    const response = await fetch("/api/state", {
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (response.status === 401) {
      sessionStorage.removeItem("t-consensus-token");
      token = "";
      login.hidden = false;
      dashboard.hidden = true;
      loginError.textContent = "Ключ не принят. Проверьте его в Discord.";
      setConnection("offline", "нет доступа");
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
    if (first) loginError.textContent = "Панель недоступна. Проверьте контейнер и порт.";
  } finally {
    fetching = false;
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  token = tokenInput.value.trim();
  sessionStorage.setItem("t-consensus-token", token);
  await fetchState({ first: true });
});

setInterval(() => {
  text("clock", new Date().toLocaleTimeString("ru-RU"));
  if (state?.session) {
    text("timer-value", formatTimer(state.session.timer_deadline));
  }
}, 250);

if (token) {
  fetchState({ first: true });
}
timer = setInterval(fetchState, 1000);
window.addEventListener("beforeunload", () => clearInterval(timer));
