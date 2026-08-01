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
const RESULT_LABELS = {
  accepted: "принят",
  rejected: "отклонён",
  vetoed: "вето",
  oral: "устное решение",
};
const CATEGORY_LABELS = {
  ordinary: "Обычное",
  significant: "Значимое",
  supreme: "Верховное",
};

const initialQuery = new URLSearchParams(window.location.search);
let token = sessionStorage.getItem("t-consensus-token") || "";
let selectedMode = initialQuery.get("mode")
  || sessionStorage.getItem("t-consensus-mode")
  || "";
let requestedBillId = Number(initialQuery.get("bill") || 0);
let state = null;
let pollTimer = null;
let fetching = false;
let commanding = false;
let stateSignature = "";
let messageTimer = null;
let observerTab = sessionStorage.getItem("t-consensus-observer-tab") || "participants";
let dialogBill = null;
let dialogResult = null;
let libraryItems = [];
let libraryFilter = "all";
let libraryLoading = false;
let observerFeedSignature = "";
let participantSignature = "";
let controlsSignature = "";
let visualOutcomeKey = "";
let verdictAnimationTimer = null;
let billDialogReturnFocus = null;
let libraryDialogReturnFocus = null;

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

function stableSignature(value) {
  try {
    return JSON.stringify(value);
  } catch (error) {
    return String(value);
  }
}

function payloadSignature(payload) {
  if (!payload || typeof payload !== "object") return "";
  const { updated_at: _updatedAt, ...stablePayload } = payload;
  return stableSignature(stablePayload);
}

function setDataLoading(visible) {
  byId("data-loading").hidden = !visible;
  if (visible) dashboard.setAttribute("inert", "");
  else dashboard.removeAttribute("inert");
}

function schedulePoll(delay = null) {
  clearTimeout(pollTimer);
  const nextDelay = delay ?? (document.hidden ? 15000 : state?.active ? 2500 : 8000);
  pollTimer = setTimeout(async () => {
    if (!dashboard.hidden) await fetchState();
    schedulePoll();
  }, nextDelay);
}

function formatNumber(value) {
  return String(Number(value || 0)).padStart(3, "0");
}

function formatPercent(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric.toLocaleString("ru-RU", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })}%`;
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function categoryLabel(value) {
  return CATEGORY_LABELS[String(value || "ordinary")] || "Обычное";
}

function visualStage(session) {
  const stage = String(session?.stage || "idle");
  return [
    "registration",
    "voting",
    "finalizing",
    "discussion_type",
    "discussion",
    "paused",
    "after_result",
  ].includes(stage)
    ? stage.replace("_", "-")
    : "idle";
}

function applyVisualState(data) {
  const session = data.session;
  const bill = session?.current_bill || null;
  const result = currentResult(session, bill);
  const active = Boolean(data.active && session);
  const stage = visualStage(session);
  const rawOutcome = String(result?.status || "");
  const outcome = ["accepted", "rejected", "vetoed", "oral"].includes(rawOutcome)
    ? rawOutcome
    : active
      ? "pending"
      : "idle";

  document.body.dataset.stage = stage;
  document.body.dataset.outcome = outcome;
  document.body.dataset.mode = data.mode === "simulation" ? "simulation" : "live";

  const nextOutcomeKey = result
    ? `${data.mode || "live"}:${result.bill_id || bill?.id || bill?.bill_number || "bill"}:${outcome}:${result.overall_percent ?? ""}`
    : "";
  if (nextOutcomeKey && nextOutcomeKey !== visualOutcomeKey) {
    document.body.classList.remove("verdict-transition");
    void document.body.offsetWidth;
    document.body.classList.add("verdict-transition");
    clearTimeout(verdictAnimationTimer);
    verdictAnimationTimer = setTimeout(() => {
      document.body.classList.remove("verdict-transition");
    }, 1800);
    text(
      "result-announcer",
      `${RESULT_LABELS[outcome] || "Результат зафиксирован"}. Общий консенсус ${formatPercent(result.overall_percent)}.`,
    );
  }
  visualOutcomeKey = nextOutcomeKey;
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
  text("participants-count", participants.length);
  const nextSignature = stableSignature(
    participants.map((participant) => [
      participant.user_id,
      participant.name,
      participant.kind,
      Boolean(participant.confirmed),
      Boolean(participant.voted),
    ]),
  );
  if (nextSignature === participantSignature) return;
  participantSignature = nextSignature;
  clearNode(container);
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
  const nextSignature = stableSignature(
    items.map((item) => [
      item.id || item.bill_id,
      item.bill_number,
      item.title,
      item.status,
      item.overall_percent,
    ]),
  );
  if (container.dataset.signature === nextSignature) return;
  container.dataset.signature = nextSignature;
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
    const title = document.createElement("button");
    title.type = "button";
    title.className = "list-title list-title-button";
    title.textContent = item.title || "Без названия";
    title.addEventListener("click", () => openBillRecord(item));
    const meta = document.createElement("span");
    const status = String(item.status || "");
    meta.className = `list-meta ${status}`;
    meta.textContent = kind === "queue"
      ? "в очереди"
      : `${RESULT_LABELS[status] || status || "решение"} · ${formatPercent(item.overall_percent)}`;
    row.append(number, title, meta);
    container.append(row);
  });
}

function currentResult(session, bill) {
  if (!session || !bill) return null;
  if (
    session.current_result
    && Number(session.current_result.bill_number) === Number(bill.bill_number)
  ) {
    return session.current_result;
  }
  return [...(session.results || [])]
    .reverse()
    .find((item) => Number(item.bill_number) === Number(bill.bill_number)) || null;
}

function renderObserverBlocks(blocks = {}) {
  document.querySelectorAll("#observer-blocks [data-block]").forEach((node) => {
    const stateName = blocks[node.dataset.block] || "pending";
    const [label, className] = BLOCK_STATES[stateName] || BLOCK_STATES.pending;
    node.className = className;
    node.querySelector("em").textContent = label;
  });
}

function observerFeedRow(title, meta, options = {}) {
  const node = options.button
    ? document.createElement("button")
    : options.href
      ? document.createElement("a")
      : document.createElement("div");
  node.className = "observer-feed-row";
  if (options.button) node.type = "button";
  if (options.href) {
    node.href = options.href;
    node.target = "_blank";
    node.rel = "noreferrer";
  }
  const heading = document.createElement("strong");
  const detail = document.createElement("span");
  heading.textContent = title;
  detail.textContent = meta;
  node.append(heading, detail);
  return node;
}

function renderObserverFeed(data) {
  const feed = byId("observer-feed");
  const session = data.session;
  const participants = session?.participants || [];
  const results = session?.results?.length
    ? session.results
    : data.recent_results || [];
  text("observer-participants-count", participants.length);
  text("observer-tab-queue-count", (data.queue || []).length);
  text("observer-results-count", results.length);

  if (!["participants", "queue", "results"].includes(observerTab)) {
    observerTab = "participants";
  }
  document.querySelectorAll("#observer-tabs [data-tab]").forEach((button) => {
    const active = button.dataset.tab === observerTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  const sourceItems = observerTab === "participants"
    ? participants.map((item) => [
      item.user_id,
      item.name,
      item.kind,
      item.confirmed,
      item.voted,
    ])
    : observerTab === "queue"
      ? (data.queue || []).map((item) => [
        item.id || item.bill_id,
        item.bill_number,
        item.title,
        item.status,
      ])
      : results.map((item) => [
        item.bill_id || item.id,
        item.bill_number,
        item.title,
        item.status,
        item.overall_percent,
      ]);
  const nextSignature = stableSignature([observerTab, sourceItems]);
  if (nextSignature === observerFeedSignature) return;
  const previousScrollTop = feed.scrollTop;
  const wasNearBottom = feed.scrollHeight - feed.clientHeight - previousScrollTop < 24;
  observerFeedSignature = nextSignature;
  clearNode(feed);

  let items = [];
  if (observerTab === "participants") {
    items = participants;
    participants.forEach((participant) => {
      const meta = participant.confirmed
        ? `${participant.kind} · ${participant.voted ? "голос принят" : "ожидается голос"}`
        : `${participant.kind} · ожидается подтверждение`;
      const row = observerFeedRow(participant.name, meta);
      row.classList.add(participant.confirmed ? "ready" : "waiting");
      feed.append(row);
    });
  } else if (observerTab === "queue") {
    items = data.queue || [];
    items.forEach((item) => {
      const row = observerFeedRow(
        `№${formatNumber(item.bill_number)} · ${item.title || "Без названия"}`,
        "готов к рассмотрению · открыть текст",
        { button: true },
      );
      row.addEventListener("click", () => openBillRecord(item));
      feed.append(row);
    });
  } else {
    items = results;
    results.forEach((item) => {
      const row = observerFeedRow(
        `№${formatNumber(item.bill_number)} · ${item.title || "Без названия"}`,
        `${RESULT_LABELS[item.status] || item.status || "решение"} · общий ${formatPercent(item.overall_percent)}`,
        { button: Boolean(item.bill || item.bill_id) },
      );
      row.classList.add(item.status || "waiting");
      if (item.bill || item.bill_id) {
        row.addEventListener(
          "click",
          () => openBillRecord(item),
        );
      }
      feed.append(row);
    });
  }

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "observer-feed-empty";
    empty.textContent = observerTab === "participants"
      ? "Состав ещё не зарегистрирован"
      : observerTab === "queue"
        ? "Очередь законопроектов пуста"
        : "Зафиксированных решений пока нет";
    feed.append(empty);
  }
  feed.scrollTop = wasNearBottom
    ? feed.scrollHeight
    : Math.min(previousScrollTop, Math.max(0, feed.scrollHeight - feed.clientHeight));
}

function renderObserver(data) {
  const session = data.session;
  const bill = session?.current_bill || null;
  const result = currentResult(session, bill);
  const active = Boolean(data.active && session);
  const resultStatus = byId("observer-result-status");

  text(
    "observer-eyebrow",
    session
      ? `${selectedMode === "simulation" ? "УЧЕБНЫЙ" : "ПЛЕНАРНЫЙ"} КОНСЕНСУС · ${session.plenary_number}`
      : "ТЕКУЩЕЕ ЗАСЕДАНИЕ",
  );
  text(
    "observer-session-title",
    session
      ? active ? "Заседание в процессе" : "Заседание завершено"
      : "Консенсус не проводится",
  );
  text(
    "observer-session-detail",
    session
      ? `Ведущий: ${session.leader.name} · ${session.stage_label.toLowerCase()}`
      : "Экран ожидает начало следующего пленарного заседания.",
  );
  text("observer-stage", session?.stage_label || "Ожидание");
  byId("observer-stage").className = `stage-badge stage-${visualStage(session)}${active ? "" : " idle"}`;

  text(
    "observer-bill-number",
    bill ? `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}` : "ПРОЕКТ НЕ ВЫБРАН",
  );
  text("observer-bill-title", bill?.title || "Между законопроектами");
  text("observer-bill-author", bill?.author?.name || "—");
  text("observer-bill-date", formatDate(bill?.created_at));
  text("observer-bill-category", categoryLabel(bill?.decision_category));
  text(
    "observer-bill-summary",
    bill?.summary || "Полный текст появится после выбора законопроекта.",
  );
  const openBill = byId("open-bill-dialog");
  openBill.disabled = !bill;
  dialogBill = bill;
  dialogResult = result;
  const sourceLink = byId("observer-bill-link");
  sourceLink.hidden = !bill?.source_url;
  if (bill?.source_url) sourceLink.href = bill.source_url;

  const requiredPercent = Number(
    result?.required_percent
    || session?.rules?.acceptance_percent
    || 50,
  );
  text("observer-required-percent", formatPercent(requiredPercent));
  byId("observer-threshold-mark").style.left = `${Math.max(0, Math.min(100, requiredPercent))}%`;

  if (result) {
    const status = String(result.status || "");
    resultStatus.className = `result-status ${status}`;
    resultStatus.textContent = RESULT_LABELS[status] || status || "зафиксирован";
    text("observer-overall-percent", formatPercent(result.overall_percent));
    text(
      "observer-result-detail",
      `${RESULT_LABELS[status] || "Результат зафиксирован"} · точный итог голосования`,
    );
    text("observer-internal-percent", formatPercent(result.internal_percent));
    text("observer-opposed-percent", formatPercent(result.opposed_percent));
    byId("observer-result-bar").style.width = `${Math.max(0, Math.min(100, Number(result.overall_percent) || 0))}%`;
  } else {
    const status = session?.stage === "voting" || session?.stage === "finalizing"
      ? "голосование"
      : session?.stage_label?.toLowerCase() || "ожидание";
    resultStatus.className = "result-status waiting";
    resultStatus.textContent = status;
    text("observer-overall-percent", "скрыт");
    text(
      "observer-result-detail",
      "Точный процент появится после фиксации — текущие направления не раскрываются",
    );
    text("observer-internal-percent", "—");
    text("observer-opposed-percent", "—");
    byId("observer-result-bar").style.width = "0%";
  }

  text(
    "observer-quorum",
    session ? `${session.quorum.confirmed}/${session.quorum.invited}` : "—",
  );
  text(
    "observer-quorum-detail",
    session
      ? `${formatPercent(session.quorum.percent)} · ${session.quorum.ready ? "собран" : "ожидание"}`
      : "нет сессии",
  );
  text(
    "observer-votes",
    session ? `${session.voting.received}/${session.voting.expected}` : "—",
  );
  text("observer-timer", formatTimer(session?.timer_deadline));
  text("observer-queue-count", (data.queue || []).length);
  renderObserverBlocks(session?.blocks || {});
  renderObserverFeed(data);
}

function showBillDialog(bill, result = null) {
  if (!bill) return;
  billDialogReturnFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  dialogBill = bill;
  dialogResult = result;
  const billId = Number(bill.id || bill.bill_id || 0);
  byId("copy-bill-link").disabled = !Number.isInteger(billId) || billId <= 0;
  if (Number.isInteger(billId) && billId > 0) {
    const url = new URL(window.location.href);
    url.searchParams.set("bill", String(billId));
    if (selectedMode) url.searchParams.set("mode", selectedMode);
    window.history.replaceState(null, "", url);
  }
  text("bill-dialog-number", `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}`);
  text("bill-dialog-title", bill.title || "Без названия");
  text("bill-dialog-author", bill.author?.name || "Автор не указан");
  text("bill-dialog-date", formatDate(bill.created_at));
  text("bill-dialog-category", categoryLabel(bill.decision_category));
  text("bill-dialog-summary", bill.summary || "Текст предложения не сохранён.");
  const materials = String(bill.materials || "").trim();
  byId("bill-dialog-materials-wrap").hidden = !materials;
  text("bill-dialog-materials", materials || "Материалы не приложены.");
  const link = byId("bill-dialog-link");
  link.hidden = !bill.source_url;
  if (bill.source_url) link.href = bill.source_url;
  const resultPanel = byId("bill-dialog-result");
  resultPanel.hidden = !result;
  if (result) {
    const status = String(result.status || "");
    text(
      "bill-dialog-result-status",
      RESULT_LABELS[status] || status || "зафиксировано",
    );
    byId("bill-dialog-result-status").className = status;
    text("bill-dialog-overall", formatPercent(result.overall_percent));
    text("bill-dialog-internal", formatPercent(result.internal_percent));
    text("bill-dialog-opposed", formatPercent(result.opposed_percent));
    text("bill-dialog-required", formatPercent(result.required_percent));
  }
  const dialog = byId("bill-dialog");
  const dialogArticle = dialog.querySelector("article");
  if (dialogArticle) dialogArticle.scrollTop = 0;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  byId("close-bill-dialog").focus({ preventScroll: true });
}

function closeBillDialog() {
  const dialog = byId("bill-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  if (billDialogReturnFocus?.isConnected) billDialogReturnFocus.focus();
  billDialogReturnFocus = null;
  const url = new URL(window.location.href);
  url.searchParams.delete("bill");
  window.history.replaceState(null, "", url);
}

async function copyBillLink() {
  const billId = Number(dialogBill?.id || dialogBill?.bill_id || 0);
  if (!Number.isInteger(billId) || billId <= 0) return;
  const url = new URL(window.location.href);
  url.searchParams.set("bill", String(billId));
  if (selectedMode) url.searchParams.set("mode", selectedMode);
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(url.toString());
    } else {
      const helper = document.createElement("textarea");
      helper.value = url.toString();
      helper.readOnly = true;
      helper.style.position = "fixed";
      helper.style.left = "-9999px";
      document.body.append(helper);
      helper.select();
      const copied = document.execCommand("copy");
      helper.remove();
      if (!copied) throw new Error("clipboard_unavailable");
    }
    showCommandMessage("Ссылка на законопроект скопирована.");
  } catch {
    showCommandMessage("Не удалось скопировать ссылку. Скопируйте адрес браузера.", "error");
  }
}

async function openBillRecord(item) {
  const knownBill = item?.summary !== undefined
    ? item
    : item?.bill?.summary !== undefined
      ? item.bill
      : null;
  const knownResult = item?.result || (
    item?.overall_percent !== undefined ? item : null
  );
  if (knownBill && String(knownBill.summary || "").trim()) {
    showBillDialog(knownBill, knownResult);
    return;
  }
  const billId = Number(item?.id || item?.bill_id || item?.bill?.id || 0);
  if (!Number.isInteger(billId) || billId <= 0) {
    showCommandMessage("Полный текст этого проекта ещё не сохранён.", "error");
    return;
  }
  setConnection("", "загрузка проекта");
  try {
    const response = await fetch(
      `/api/bills/${billId}?mode=${encodeURIComponent(selectedMode)}`,
      {
        headers: authHeaders(),
        credentials: "same-origin",
        cache: "no-store",
      },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    showBillDialog(payload.bill, payload.result);
    setConnection("online", "обновляется");
  } catch (error) {
    showCommandMessage(
      "Не удалось открыть законопроект. Обновите панель и повторите.",
      "error",
    );
    setConnection("offline", "ошибка загрузки");
  }
}

function catalogStatus(item) {
  if (item.result) {
    const status = String(item.result.status || "");
    return `${RESULT_LABELS[status] || status || "решение"} · ${formatPercent(item.result.overall_percent)}`;
  }
  return {
    draft: "готовится",
    queued: "в очереди",
    requeued: "повторное рассмотрение",
    under_consideration: "рассматривается",
  }[String(item.status || "")] || String(item.status || "законопроект");
}

function renderBillLibrary() {
  const list = byId("bill-library-list");
  clearNode(list);
  const query = byId("bill-library-search").value.trim().toLocaleLowerCase("ru-RU");
  const filtered = libraryItems.filter((item) => {
    const matchesFilter = libraryFilter === "all"
      || (libraryFilter === "decided" && Boolean(item.result))
      || (
        libraryFilter === "queue"
        && ["draft", "queued", "requeued", "under_consideration"].includes(
          String(item.status || ""),
        )
        && !item.result
      );
    if (!matchesFilter) return false;
    if (!query) return true;
    return [
      formatNumber(item.bill_number),
      item.title,
      item.author?.name,
    ].some((value) => String(value || "").toLocaleLowerCase("ru-RU").includes(query));
  });
  text(
    "bill-library-count",
    `${filtered.length} из ${libraryItems.length}`,
  );
  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "library-empty";
    empty.textContent = libraryLoading
      ? "Загружаем законопроекты…"
      : "По этому запросу законопроектов нет";
    list.append(empty);
    return;
  }
  filtered.forEach((item) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "library-row";
    const number = document.createElement("span");
    number.className = "library-number";
    number.textContent = `№${formatNumber(item.bill_number)}`;
    const content = document.createElement("span");
    content.className = "library-content";
    const title = document.createElement("strong");
    const author = document.createElement("small");
    title.textContent = item.title || "Без названия";
    author.textContent = `${item.author?.name || "Автор не указан"} · ${formatDate(item.created_at)}`;
    content.append(title, author);
    const status = document.createElement("span");
    status.className = `library-status ${item.result?.status || item.status || ""}`;
    status.textContent = catalogStatus(item);
    row.append(number, content, status);
    row.addEventListener("click", async () => {
      closeBillLibrary();
      await openBillRecord(item);
    });
    list.append(row);
  });
}

async function openBillLibrary() {
  const dialog = byId("bill-library-dialog");
  libraryDialogReturnFocus = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  if (libraryLoading) return;
  libraryLoading = true;
  libraryItems = [];
  text("bill-library-status", "обновление каталога");
  renderBillLibrary();
  try {
    const response = await fetch(
      `/api/bills?mode=${encodeURIComponent(selectedMode)}`,
      {
        headers: authHeaders(),
        credentials: "same-origin",
        cache: "no-store",
      },
    );
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    libraryItems = Array.isArray(payload.items) ? payload.items : [];
    text("bill-library-status", `контур: ${selectedMode === "simulation" ? "симуляция" : "рабочий"}`);
  } catch (error) {
    text("bill-library-status", "каталог временно недоступен");
  } finally {
    libraryLoading = false;
    renderBillLibrary();
  }
}

function closeBillLibrary() {
  const dialog = byId("bill-library-dialog");
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
  if (libraryDialogReturnFocus?.isConnected) libraryDialogReturnFocus.focus();
  libraryDialogReturnFocus = null;
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
  byId("reactor-link").hidden = !viewer.administrator;
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
  const capabilities = new Set(data.capabilities || []);
  const viewer = data.viewer || {};
  const session = data.session;
  panel.hidden = capabilities.size === 0;
  const nextSignature = stableSignature({
    mode: selectedMode,
    capabilities: [...capabilities].sort(),
    commanding,
    session_key: session?.key || "",
    stage: session?.stage || "",
    stage_label: session?.stage_label || "",
    bill_id: session?.current_bill?.id || null,
    viewer: viewer.name || "",
  });
  if (nextSignature === controlsSignature) return;
  controlsSignature = nextSignature;
  clearNode(container);
  if (panel.hidden) return;

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
  applyVisualState(data);
  const observerMode = !(data.capabilities || []).length;
  document.body.classList.toggle("observer-screen-mode", observerMode);
  byId("observer-screen").hidden = !observerMode;
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
    byId("stage-badge").className = "stage-badge stage-idle idle";
    text("quorum-value", "—");
    text("quorum-detail", "нет активной сессии");
    text("votes-value", "—");
    text("timer-value", "—");
    text("timer-detail", "таймер не запущен");
    text("bill-title", "Проект не выбран");
    text("bill-number", "—");
    byId("bill-link").hidden = true;
    byId("admin-open-current-bill").disabled = true;
    byId("discussion-link").hidden = true;
    renderBlocks({});
    renderParticipants([]);
    renderObserver(data);
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
  byId("stage-badge").className = `stage-badge stage-${visualStage(session)}${active ? "" : " idle"}`;
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
  const result = currentResult(session, bill);
  text("bill-title", bill?.title || "Между законопроектами");
  text("bill-number", bill ? `ЗАКОНОПРОЕКТ №${formatNumber(bill.bill_number)}` : "ПРОЕКТ НЕ ВЫБРАН");
  const adminOpenBill = byId("admin-open-current-bill");
  adminOpenBill.disabled = !bill;
  adminOpenBill.onclick = bill
    ? () => showBillDialog(bill, result)
    : null;
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
  renderObserver(data);
}

function authHeaders() {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function fetchState({ first = false, blocking = false } = {}) {
  if (fetching || commanding) return;
  fetching = true;
  if (first || blocking) setDataLoading(true);
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
    const payload = await response.json();
    const nextSignature = payloadSignature(payload);
    if (!first && stateSignature && nextSignature !== stateSignature && document.hidden) {
      globalThis.TModTabSignal?.pulse("Консенсус обновлён");
    }
    if (first || nextSignature !== stateSignature) {
      render(payload);
      stateSignature = nextSignature;
    }
    login.hidden = true;
    dashboard.hidden = false;
    loginError.textContent = "";
    setConnection("online", "обновляется");
    if (
      first
      && Number.isInteger(requestedBillId)
      && requestedBillId > 0
    ) {
      const billId = requestedBillId;
      requestedBillId = 0;
      await openBillRecord({ id: billId });
    }
  } catch (error) {
    setConnection("offline", "связь потеряна");
    if (first) loginError.textContent = "Панель недоступна. Проверьте контейнер T-Mod, Caddy и DNS.";
  } finally {
    fetching = false;
    if (first || blocking) setDataLoading(false);
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
    libraryItems = [];
    await fetchState({ blocking: true });
  });
});

document.querySelectorAll("#observer-tabs [data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    observerTab = button.dataset.tab || "participants";
    sessionStorage.setItem("t-consensus-observer-tab", observerTab);
    if (state) renderObserverFeed(state);
  });
});

byId("open-bill-dialog").addEventListener(
  "click",
  () => showBillDialog(dialogBill, dialogResult),
);
byId("close-bill-dialog").addEventListener("click", closeBillDialog);
byId("bill-dialog-done").addEventListener("click", closeBillDialog);
byId("copy-bill-link").addEventListener("click", copyBillLink);
byId("bill-dialog").addEventListener("click", (event) => {
  if (event.target === byId("bill-dialog")) closeBillDialog();
});
byId("open-bill-library").addEventListener("click", openBillLibrary);
byId("close-bill-library").addEventListener("click", closeBillLibrary);
byId("bill-library-dialog").addEventListener("click", (event) => {
  if (event.target === byId("bill-library-dialog")) closeBillLibrary();
});
byId("bill-library-search").addEventListener("input", renderBillLibrary);
document.querySelectorAll("#bill-library-filters [data-filter]").forEach((button) => {
  button.addEventListener("click", () => {
    libraryFilter = button.dataset.filter || "all";
    document.querySelectorAll("#bill-library-filters [data-filter]").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    renderBillLibrary();
  });
});

setInterval(() => {
  text("clock", new Date().toLocaleTimeString("ru-RU"));
  if (state?.session) {
    const timer = formatTimer(state.session.timer_deadline);
    text("timer-value", timer);
    text("observer-timer", timer);
  }
}, 1000);

fetchState({ first: true }).finally(() => schedulePoll());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !dashboard.hidden) {
    void fetchState();
    schedulePoll();
  }
});
window.addEventListener("beforeunload", () => clearTimeout(pollTimer));
