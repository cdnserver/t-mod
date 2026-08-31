/* SGL Case OS — case-centric internal workspace. */
"use strict";

const $ = (id) => document.getElementById(id);
const app = $("app");
const STATUS = {
  reserved: "Резерв", created: "Открыт", awaiting_situation: "Ждёт обстоятельства",
  awaiting_link: "Ждёт иск", awaiting_close: "Готов к закрытию", closed: "Закрыт",
  archived: "В архиве", error: "Нужна проверка", draft: "Черновик",
  publishing: "Публикуется", published: "Опубликован", failed: "Ошибка",
  tracked: "Под наблюдением", changed: "Изменено", issued: "Ожидает оплаты",
  proofs_submitted: "Доказательства готовы", confirmed: "Подтверждено",
  open: "Открыта", done: "Готово", cancelled: "Отменена",
};
const EVENT_COPY = {
  reserved: "Зарезервирован номер нового дела", channel_created: "Создан защищённый Discord-канал",
  params_saved: "Сохранены данные клиента", situation_saved: "Добавлены обстоятельства дела",
  claim_link_saved: "Привязана ссылка на иск", closed: "Дело закрыто и передано в архивную очередь",
  web_case_updated: "Обновлена карточка дела", web_message_sent: "Сообщение отправлено из SGL",
  discord_message_created: "Новое сообщение в Discord", discord_message_updated: "Сообщение отредактировано в Discord",
  discord_message_deleted: "Сообщение удалено в Discord", receipt_created: "Создана квитанция",
  receipt_proofs_submitted: "Добавлены подтверждения оплаты", receipt_confirmed: "Оплата подтверждена",
  forum_draft_created: "Создан черновик иска", forum_draft_updated: "Обновлён черновик иска",
  forum_published: "Иск опубликован на форуме", forum_topic_changed: "Forum Watch заметил изменение темы",
  atlas_analysis_saved: "Atlas сохранил анализ по делу", task_created: "Создана задача",
  task_updated: "Обновлена задача", task_completed: "Задача выполнена",
  notification_acknowledged: "Сигнал отмечен как разобранный", error: "Кейс требует технической проверки",
};
const PLAYBOOKS = {
  claim: {
    label: "Иск",
    tasks: [
      ["Проверить карточку клиента", "Сверить контакты, static ID и полномочия перед подготовкой позиции.", "high"],
      ["Собрать хронологию и доказательства", "Разложить факты по датам, отметить пробелы и приложенные материалы.", "high"],
      ["Подготовить проект иска", "Собрать структурированный черновик и вынести спорные места на ручную проверку.", "normal"],
    ],
  },
  appeal: {
    label: "Апелляция",
    tasks: [
      ["Зафиксировать процессуальный срок", "Проверить дату решения и крайний срок подачи.", "critical"],
      ["Собрать решение и материалы", "Убедиться, что в досье есть исходное решение и ключевые доказательства.", "high"],
      ["Собрать основания апелляции", "Отделить проверенные доводы от предположений и подготовить план позиции.", "normal"],
    ],
  },
  consultation: {
    label: "Консультация",
    tasks: [
      ["Уточнить исходные факты", "Собрать минимальный набор обстоятельств и ссылок на материалы.", "high"],
      ["Сформировать варианты действий", "Подготовить понятный план, риски и вопросы для клиента.", "normal"],
    ],
  },
};
const CASE_TABS = ["overview", "conversation", "timeline", "decisions", "evidence", "finance", "forum", "atlas", "archive"];
const state = {
  bootstrap: null,
  operations: { queue: [], notifications: [], events: [], counts: {}, health: {} },
  forum: { publications: [], observations: [], watch: {} },
  directory: { clients: [], lawyers: [] },
  tasks: [], workView: "open", workQuery: "", taskDialog: { caseNumber: null, editId: null },
  evidenceFilter: "all", messageReply: null,
  memberPicker: { formId: "", target: "", role: "", scope: "staff", filter: "all", query: "", members: [], timer: null, request: 0 },
  palette: { query: "", activeIndex: -1, results: [] },
  modalFocus: {},
  refreshing: false, lastRefreshAt: 0, refreshTimer: null,
  screen: "today", caseNumber: null, detail: null, caseTab: "overview",
  caseView: "all", caseQuery: "", createStep: 1, casePoll: null, directoryTimer: null,
};

const text = (value) => String(value == null ? "" : value);
const esc = (value) => text(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#039;");
const svg = (name) => '<svg><use href="#sgl-i-' + name + '"></use></svg>';
const num = (value) => "№" + String(value || 0).padStart(3, "0");
const client = (item) => item && (item.client_display || item.client_nick) || "Клиент не указан";
const status = (value) => STATUS[value] || value || "—";
const uid = () => globalThis.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + "-" + Math.random();

function stamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return text(value);
  const now = new Date();
  const difference = date.getTime() - now.getTime();
  const absolute = Math.abs(difference);
  if (absolute < 45 * 1000) return difference >= 0 ? "сейчас" : "только что";
  if (absolute < 12 * 60 * 60 * 1000 && Intl.RelativeTimeFormat) {
    const units = absolute < 60 * 60 * 1000 ? "minute" : "hour";
    const divider = units === "minute" ? 60 * 1000 : 60 * 60 * 1000;
    return new Intl.RelativeTimeFormat("ru-RU", { numeric: "auto" }).format(Math.round(difference / divider), units);
  }
  const day = (entry) => new Date(entry.getFullYear(), entry.getMonth(), entry.getDate()).getTime();
  const deltaDays = Math.round((day(date) - day(now)) / (24 * 60 * 60 * 1000));
  const clock = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" }).format(date);
  if (deltaDays === 0) return "сегодня, " + clock;
  if (deltaDays === -1) return "вчера, " + clock;
  if (deltaDays === 1) return "завтра, " + clock;
  return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
}
function fullStamp(value) {
  if (!value) return "без срока";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return text(value).replace("T", " ").slice(0, 16);
  return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
function safeUrl(value) {
  try {
    const url = new URL(text(value));
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch (_) {
    return "";
  }
}
function clear(node) {
  if (node) node.replaceChildren();
}
function toast(message, failure) {
  const node = $("sgl-toast");
  node.textContent = message;
  node.classList.toggle("is-error", Boolean(failure));
  node.setAttribute("role", failure ? "alert" : "status");
  node.setAttribute("aria-live", failure ? "assertive" : "polite");
  node.setAttribute("aria-atomic", "true");
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 4600);
}
function syncStatus(mode, copy) {
  const node = $("sgl-sync-status");
  if (!node) return;
  node.dataset.state = mode || "ready";
  const label = node.querySelector("span");
  if (label) label.textContent = copy || "Данные актуальны";
}
function storedSidebarState() {
  try { return localStorage.getItem("sgl:sidebar") === "compact"; } catch (_) { return false; }
}
function setSidebarCompact(compact) {
  app.classList.toggle("is-sidebar-compact", Boolean(compact));
  const button = $("sgl-sidebar-toggle");
  if (button) {
    button.setAttribute("aria-expanded", String(!compact));
    button.setAttribute("aria-label", compact ? "Развернуть навигацию" : "Свернуть навигацию");
    button.title = compact ? "Развернуть навигацию" : "Свернуть навигацию";
  }
  try { localStorage.setItem("sgl:sidebar", compact ? "compact" : "wide"); } catch (_) {}
}
function modal(id, show) {
  const node = $(id);
  if (!node) return;
  if (show) {
    if (!node.open) {
      const origin = document.activeElement;
      if (origin instanceof HTMLElement && !node.contains(origin)) state.modalFocus[id] = origin;
      if (node.showModal) node.showModal();
      else node.setAttribute("open", "");
      requestAnimationFrame(() => {
        if (!node.open) return;
        const focusTarget = node.querySelector("[autofocus], input:not([type=hidden]):not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled])");
        if (focusTarget) focusTarget.focus({ preventScroll: true });
      });
    }
  } else if (node.open && node.close) {
    node.close();
  } else {
    node.removeAttribute("open");
    restoreModalFocus(id);
  }
}
function restoreModalFocus(id) {
  const origin = state.modalFocus[id];
  delete state.modalFocus[id];
  if (!(origin instanceof HTMLElement) || !document.contains(origin) || origin.matches(":disabled, [hidden]") || origin.closest("[hidden], [aria-hidden=true]")) return;
  requestAnimationFrame(() => origin.focus({ preventScroll: true }));
}
function pill(value) {
  return '<span class="sgl-status-pill" data-status="' + esc(value) + '">' + esc(status(value)) + "</span>";
}
function empty(copy) {
  return '<div class="sgl-empty">' + esc(copy) + "</div>";
}
function memberMeta(item) {
  return item && (item.client_display || item.client_nick) || "";
}
function memberName(item) {
  return text(item && (item.display_name || item.lawyer_nick || item.name) || "Участник SGL");
}
function memberInitials(value) {
  const words = text(value).trim().split(/\s+/).filter(Boolean);
  return (words.slice(0, 2).map((word) => word.slice(0, 1)).join("") || "SG").toLocaleUpperCase("ru-RU");
}
function knownLawyerIds() {
  return new Set((state.directory.lawyers || []).map((item) => text(item.discord_user_id || item.user_id || item.id)).filter(Boolean));
}
function knownSecretaryIds() {
  const cases = state.bootstrap && state.bootstrap.cases && state.bootstrap.cases.items || [];
  return new Set(cases.map((item) => text(item.secretary_id)).filter(Boolean));
}
function memberRole(item) {
  const id = text(item && item.id);
  if (knownLawyerIds().has(id)) return "lawyer";
  if (knownSecretaryIds().has(id)) return "secretary";
  return "staff";
}
function memberRoleLabel(role) {
  return { lawyer: "Адвокат", secretary: "Секретарь", staff: "Команда" }[role] || "Команда";
}
function memberPickerEmpty(title, copy) {
  return '<div class="sgl-member-picker-empty" data-member-picker-empty>' + svg("people") + "<strong>" + esc(title) + "</strong><span>" + esc(copy) + "</span></div>";
}
function renderMemberPicker() {
  const picker = state.memberPicker;
  const results = $("sgl-member-picker-results");
  if (!results || !picker.formId) return;
  const activeInput = $(picker.formId) && $(picker.formId).elements[picker.target];
  const selected = text(activeInput && activeInput.value);
  const members = (picker.members || []).filter((item) => {
    const role = memberRole(item);
    if (picker.scope === "lawyer" && role !== "lawyer") return false;
    if (picker.filter !== "all" && role !== picker.filter) return false;
    return true;
  });
  document.querySelectorAll("[data-member-picker-filter]").forEach((button) => {
    const incompatible = picker.scope === "lawyer" && button.dataset.memberPickerFilter !== "lawyer";
    button.hidden = incompatible;
    button.setAttribute("aria-pressed", String(button.dataset.memberPickerFilter === picker.filter));
  });
  if (!members.length) {
    results.innerHTML = memberPickerEmpty(picker.members.length ? "Нет подходящих участников" : "Команда не найдена", picker.scope === "lawyer" ? "Для ведущего адвоката доступны только люди из реестра адвокатов." : "Измените запрос или снимите фильтр, чтобы увидеть команду.");
  } else {
    results.innerHTML = members.map((item) => {
      const id = text(item.id);
      const role = memberRole(item);
      const display = memberName(item);
      const meta = [item.name && item.name !== display ? item.name : "", "Discord " + id].filter(Boolean).join(" · ");
      return '<button type="button" class="sgl-member-picker-result" role="option" data-select-member-picker="' + esc(id) + '" data-role="' + esc(role) + '" aria-selected="' + String(id === selected) + '"><span class="sgl-member-picker-avatar">' + esc(memberInitials(display)) + '</span><span class="sgl-member-picker-result-copy"><b>' + esc(display) + '</b><small>' + esc(meta) + '</small></span><em>' + esc(memberRoleLabel(role)) + "</em></button>";
    }).join("");
  }
  const status = $("sgl-member-picker-status");
  if (status) status.textContent = members.length ? "Найдено сотрудников: " + members.length + ". Выберите человека для назначения." : "Подходящих сотрудников не найдено.";
}
async function loadMemberPickerMembers(query) {
  const picker = state.memberPicker;
  if (!picker.formId) return;
  const request = ++picker.request;
  const results = $("sgl-member-picker-results");
  if (results) results.setAttribute("aria-busy", "true");
  const status = $("sgl-member-picker-status");
  if (status) status.textContent = "Ищу участников рабочего контура…";
  try {
    const result = await api("/api/sgl/members?q=" + encodeURIComponent(query || ""));
    if (request !== state.memberPicker.request) return;
    state.memberPicker.members = Array.isArray(result.members) ? result.members : [];
    renderMemberPicker();
  } catch (error) {
    if (request !== state.memberPicker.request) return;
    if (results) results.innerHTML = memberPickerEmpty("Поиск сейчас недоступен", error.message || "Не удалось получить список сотрудников.");
    if (status) status.textContent = "Список сотрудников временно недоступен.";
  } finally {
    if (request === state.memberPicker.request && results) results.setAttribute("aria-busy", "false");
  }
}
function openMemberPicker(trigger) {
  const formId = trigger.dataset.memberPickerForm;
  const target = trigger.dataset.memberPickerTarget;
  const form = $(formId);
  const field = form && form.elements[target];
  if (!form || !field) return;
  clearTimeout(state.memberPicker.timer);
  const nextRequest = Number(state.memberPicker.request || 0) + 1;
  state.memberPicker = {
    formId, target, role: trigger.dataset.memberPickerRole || "Сотрудник", scope: trigger.dataset.memberPickerScope || "staff",
    filter: trigger.dataset.memberPickerScope === "lawyer" ? "lawyer" : "all", query: "", members: [], timer: null, request: nextRequest,
  };
  $("sgl-member-picker-role").textContent = state.memberPicker.role;
  $("sgl-member-picker-target-copy").textContent = field.value ? "Сейчас назначен Discord ID " + field.value + ". Можно выбрать другого сотрудника." : "Выберите человека из доступной команды.";
  const query = $("sgl-member-picker-query");
  query.value = "";
  const clearButton = document.querySelector("[data-member-picker-clear]");
  if (clearButton) clearButton.hidden = Boolean(field.required);
  const results = $("sgl-member-picker-results");
  if (results) results.innerHTML = memberPickerEmpty("Загружаю команду", "Проверяем доступных сотрудников Discord…");
  modal("sgl-member-picker-dialog", true);
  requestAnimationFrame(() => query.focus());
  void loadMemberPickerMembers("");
}
function updateMemberField(field, item) {
  const id = text(item.id);
  field.value = id;
  if (field.tagName === "SELECT" && !Array.from(field.options).some((option) => option.value === id)) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = memberName(item) + " · " + id;
    field.append(option);
    field.value = id;
  }
  field.title = memberName(item) + " · Discord " + id;
  field.classList.add("is-picked");
  const help = field.closest(".sgl-member-field") && field.closest(".sgl-member-field").querySelector("small");
  if (help) {
    if (!help.dataset.defaultCopy) help.dataset.defaultCopy = help.textContent;
    help.textContent = "Выбран: " + memberName(item) + " · Discord " + id;
  }
}
function selectMemberPicker(memberId) {
  const picker = state.memberPicker;
  const member = (picker.members || []).find((item) => text(item.id) === String(memberId));
  const field = picker.formId && $(picker.formId) && $(picker.formId).elements[picker.target];
  if (!member || !field) return;
  updateMemberField(field, member);
  modal("sgl-member-picker-dialog", false);
  toast(memberName(member) + " назначен: " + picker.role + ".");
}
function clearMemberPicker() {
  const picker = state.memberPicker;
  const field = picker.formId && $(picker.formId) && $(picker.formId).elements[picker.target];
  if (!field || field.required) return;
  field.value = "";
  field.title = "";
  field.classList.remove("is-picked");
  const help = field.closest(".sgl-member-field") && field.closest(".sgl-member-field").querySelector("small");
  if (help && help.dataset.defaultCopy) help.textContent = help.dataset.defaultCopy;
  modal("sgl-member-picker-dialog", false);
}
function scheduleMemberPickerSearch(value) {
  clearTimeout(state.memberPicker.timer);
  state.memberPicker.query = value;
  state.memberPicker.timer = setTimeout(() => { void loadMemberPickerMembers(value); }, 200);
}

async function api(url, options) {
  const settings = options || {};
  const headers = Object.assign({}, settings.headers || {});
  const method = text(settings.method || "GET").toUpperCase();
  const multipart = settings.body instanceof FormData;
  if (settings.body && !multipart && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (!["GET", "HEAD"].includes(method)) {
    headers["X-CSRF-Token"] = state.bootstrap && state.bootstrap.viewer ? state.bootstrap.viewer.csrf_token || "" : "";
    headers["X-Idempotency-Key"] = uid();
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), settings.timeout || 60000);
  try {
    const response = await fetch(url, {
      method, headers, body: settings.body, credentials: "same-origin", cache: "no-store", signal: controller.signal,
    });
    if (response.status === 401) {
      location.assign("/login?next=%2Fsgl");
      throw new Error("Нужен вход в T-Mod.");
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.message || "Ошибка SGL (" + response.status + ")");
    return body;
  } finally {
    clearTimeout(timer);
  }
}

function removePublicAssets() {
  document.querySelectorAll("[data-public-only]").forEach((node) => node.remove());
}
function publicScript(src) {
  if (document.querySelector('script[data-public-runtime="' + src + '"]')) return;
  const script = document.createElement("script");
  script.src = src;
  script.defer = true;
  script.dataset.publicRuntime = src;
  document.body.append(script);
}
function showPublic(data) {
  document.body.classList.add("public-mode");
  document.body.classList.remove("sgl-admin-mode");
  const info = data.public || {};
  ["public-discord-top", "public-discord", "public-discord-bottom", "public-discord-mobile"].forEach((id) => {
    if ($(id) && info.discord_url) $(id).href = info.discord_url;
  });
  ["public-secretary", "public-secretary-bottom"].forEach((id) => {
    if ($(id) && info.secretary_url) $(id).href = info.secretary_url;
  });
  app.hidden = true;
  $("public-site").hidden = false;
  $("loading").hidden = true;
  publicScript("/sgl/assets/site.js?v=20260825-sgl-a11y-v3");
  publicScript("/sgl/assets/app-ui.js?v=20260820-public");
}

function routeScreen() {
  const requested = location.hash.replace(/^#/, "").trim();
  return ["today", "work", "cases", "atlas", "forum", "people", "archive"].includes(requested) ? requested : "today";
}
function normalizeCaseTab(value) {
  return CASE_TABS.includes(value) ? value : "overview";
}
function goScreen(name, replace) {
  history[replace ? "replaceState" : "pushState"]({}, "", "/sgl#" + name);
  void route();
}
function goCase(number, tab, replace) {
  const value = Number(number);
  if (!value) return;
  history[replace ? "replaceState" : "pushState"]({}, "", "/sgl/cases/" + value + "#" + (tab || "overview"));
  void route();
}
async function route() {
  const match = location.pathname.match(/^\/sgl\/cases\/(\d+)\/?$/);
  if (match) {
    state.caseTab = normalizeCaseTab(location.hash.replace(/^#/, "") || "overview");
    await openCase(Number(match[1]));
  } else {
    showScreen(routeScreen());
  }
}
function pageHeader(name) {
  const labels = {
    today: ["SGL / сегодня", "Рабочая очередь"],
    work: ["SGL / работа", "Задачи команды"],
    cases: ["SGL / кейсы", "Реестр дел"],
    atlas: ["SGL / atlas", "Atlas Intelligence"],
    forum: ["SGL / forum watch", "Контроль публикаций"],
    people: ["SGL / люди", "Реестр бюро"],
    archive: ["SGL / архив", "Evidence Vault"],
  };
  const label = labels[name] || labels.today;
  $("sgl-breadcrumb").textContent = label[0];
  $("sgl-page-title").textContent = label[1];
}
function stopPolling() {
  if (state.casePoll) clearInterval(state.casePoll);
  state.casePoll = null;
}
function renderCaseLoading(number) {
  const workspace = $("sgl-case-workspace");
  workspace.hidden = false;
  workspace.setAttribute("aria-busy", "true");
  $("sgl-case-kicker").textContent = "SGL / ДЕЛО " + num(number);
  $("sgl-case-title").textContent = "Открываю рабочее пространство…";
  $("sgl-case-header-meta").innerHTML = '<span class="sgl-status-pill">загружаем материалы</span>';
  $("sgl-case-health").innerHTML = "";
  $("sgl-case-identity").innerHTML = empty("Собираем участников, маршрут и доступы дела…");
  $("sgl-case-sidecar").innerHTML = empty("Проверяем следующие действия команды…");
  $("sgl-case-panel").innerHTML = empty("Загружаю материалы дела…");
  $("sgl-case-discord").hidden = true;
  $("sgl-case-close").hidden = true;
}
function showScreen(name) {
  state.screen = ["today", "work", "cases", "atlas", "forum", "people", "archive"].includes(name) ? name : "today";
  state.caseNumber = null;
  state.detail = null;
  state.messageReply = null;
  stopPolling();
  $("sgl-case-workspace").hidden = true;
  $("sgl-case-workspace").removeAttribute("aria-busy");
  document.querySelectorAll(".sgl-screen").forEach((node) => node.classList.toggle("is-active", node.dataset.screen === state.screen));
  document.querySelectorAll(".sgl-admin-nav [data-go]").forEach((node) => node.classList.toggle("is-active", node.dataset.go === state.screen));
  pageHeader(state.screen);
  renderScreen();
}

function normalize(value) {
  return text(value).trim().toLocaleLowerCase("ru-RU");
}
function caseMatch(item, query) {
  const needle = normalize(query);
  if (!needle) return true;
  return [
    item.case_number, item.client_display, item.client_nick, item.static_id,
    item.lead_lawyer_display, item.secretary_display, item.request_type,
  ].join(" ").toLocaleLowerCase("ru-RU").includes(needle);
}
function nextAction(item) {
  const queue = state.operations.queue || [];
  const found = queue.find((entry) => Number(entry.case_number) === Number(item.case_number));
  if (found) return { title: found.title, tab: found.tab || "overview", priority: found.priority || "normal" };
  if (["closed", "archived"].includes(item.status)) return { title: "Архивная история", tab: "archive", priority: "low" };
  if (!item.situation_text) return { title: "Запросить обстоятельства", tab: "evidence", priority: "high" };
  if (item.status === "awaiting_link" && !item.claim_link) return { title: "Подготовить иск", tab: "forum", priority: "high" };
  return { title: "Продолжить ведение дела", tab: "overview", priority: "normal" };
}
function caseHealthSignals(item) {
  const signals = [];
  const tasks = state.detail && state.detail.tasks || [];
  const overdue = tasks.filter(taskIsOverdue);
  const decisions = state.detail && state.detail.decisions || [];
  const receipts = state.detail && state.detail.receipts || [];
  const observations = state.detail && state.detail.forum_observations || [];
  if (overdue.length) signals.push({ tone: "critical", tab: "overview", label: overdue.length + " просроч. задача", detail: overdue[0].title });
  if (!item.situation_text) signals.push({ tone: "attention", tab: "evidence", label: "Нужны факты", detail: "Нет описания обстоятельств" });
  if (observations.some((entry) => entry.status === "changed" || entry.status === "error")) signals.push({ tone: "attention", tab: "forum", label: "Forum Watch", detail: "Есть изменение или ошибка проверки" });
  if (receipts.some((entry) => !["confirmed", "cancelled"].includes(entry.status))) signals.push({ tone: "attention", tab: "finance", label: "Оплата", detail: "Есть неподтверждённая квитанция" });
  if (decisions.some((entry) => ["open", "read"].includes(entry.status))) signals.push({ tone: "violet", tab: "decisions", label: "Внутр. решение", detail: "Ждёт прочтения или подтверждения" });
  if (!signals.length) signals.push({ tone: "ready", tab: "overview", label: "Контур стабилен", detail: "Нет блокирующих сигналов" });
  return signals.slice(0, 4);
}
function renderCaseHealth(item) {
  const signals = caseHealthSignals(item);
  $("sgl-case-health").innerHTML = signals.map((signal) => '<button data-case-tab="' + esc(signal.tab) + '" data-tone="' + esc(signal.tone) + '"><i></i><span><b>' + esc(signal.label) + '</b><small>' + esc(signal.detail) + '</small></span>' + svg("arrow") + "</button>").join("");
}
function filteredCases() {
  const payment = new Set((state.operations.queue || []).filter((item) => item.kind === "receipt").map((item) => Number(item.case_number)));
  const forum = new Set([].concat(state.forum.publications || [], state.forum.observations || []).map((item) => Number(item.case_number)));
  return (state.bootstrap && state.bootstrap.cases ? state.bootstrap.cases.items : []).filter((item) => {
    if (!caseMatch(item, state.caseQuery)) return false;
    if (state.caseView === "inbox") return ["reserved", "created"].includes(item.status);
    if (state.caseView === "active") return !["closed", "archived", "reserved"].includes(item.status);
    if (state.caseView === "waiting") return ["awaiting_situation", "awaiting_link"].includes(item.status);
    if (state.caseView === "payment") return payment.has(Number(item.case_number));
    if (state.caseView === "forum") return forum.has(Number(item.case_number));
    if (state.caseView === "closed") return ["closed", "archived"].includes(item.status);
    return true;
  });
}

function renderNav() {
  const counts = state.operations.counts || {};
  $("sgl-nav-queue").textContent = text((state.operations.queue || []).length);
  $("sgl-nav-tasks").textContent = text((state.tasks || []).filter((item) => item.status === "open").length);
  $("sgl-nav-cases").textContent = text(counts.open_cases || 0);
  $("sgl-nav-forum").textContent = text(counts.forum_alerts || 0);
  $("sgl-inbox-count").textContent = (state.operations.notifications || []).length ? text((state.operations.notifications || []).length) : "";
}
function renderHealth() {
  const health = state.operations.health || {};
  const rows = [
    ["Discord", health.discord === "ready" ? "Доступен для синхронизации" : "Недоступен сейчас", health.discord === "ready" ? "ready" : "attention"],
    ["Atlas AI", health.atlas === "on_demand" ? "Запускается только по запросу к кейсу" : "Неизвестно", "manual"],
    ["Forum Watch", health.forum_watch === "scheduled" ? "Автопроверка раз в " + Math.round((health.forum_interval_seconds || 900) / 60) + " мин." : "Выключен — нужна авторизованная браузерная сессия", health.forum_watch === "scheduled" ? "ready" : "disabled"],
    ["Публикация форума", health.forum_publish === "manual_ready" ? "Готова к явной ручной публикации" : "Выключена в конфигурации", health.forum_publish === "manual_ready" ? "manual" : "disabled"],
  ];
  $("sgl-health-list").innerHTML = rows.map((row) => '<div class="sgl-health-row is-' + row[2] + '"><i></i><div><b>' + esc(row[0]) + '</b><small>' + esc(row[1]) + '</small></div><span>' + (row[2] === "ready" ? "готов" : row[2] === "manual" ? "вручную" : row[2] === "disabled" ? "выкл." : "проверить") + "</span></div>").join("");
  const side = $("sgl-side-health");
  side.classList.toggle("is-healthy", health.discord === "ready");
  side.querySelector("b").textContent = health.discord === "ready" ? "Контур доступен" : "Нужна проверка";
  side.querySelector("small").textContent = health.forum_watch === "scheduled" ? "Discord · Atlas · Forum Watch" : "Discord · Atlas · Forum Watch выключен";
}
function renderToday() {
  const counts = state.operations.counts || {};
  const queue = state.operations.queue || [];
  const cards = [
    ["blue", counts.open_cases || 0, "Активные дела", "в рабочем контуре"],
    ["amber", counts.open_tasks || 0, "Открытые задачи", "назначены команде"],
    ["red", queue.filter((item) => ["critical", "high"].includes(item.priority)).length, "Требуют внимания", "есть приоритетный следующий шаг"],
    ["violet", counts.atlas_notes || 0, "Выводы Atlas", "сохранены в досье"],
  ];
  $("sgl-today-summary").innerHTML = cards.map((card) => '<article class="sgl-summary-item" data-tone="' + card[0] + '"><small>' + esc(card[2]) + "</small><strong>" + esc(card[1]) + "</strong><span>" + esc(card[3]) + "</span></article>").join("");
  $("sgl-triage-list").innerHTML = queue.length ? queue.slice(0, 10).map((item) => {
    const detail = item.detail || memberMeta(item);
    const tail = item.due_at ? fullStamp(item.due_at) : item.priority === "critical" ? "срочно" : "открыть";
    return '<button class="sgl-triage-item" data-open-case="' + esc(item.case_number) + '" data-open-tab="' + esc(item.tab || "overview") + '" data-priority="' + esc(item.priority || "normal") + '"><i></i><div><strong>' + esc(item.title) + "</strong><small>" + esc(num(item.case_number) + " · " + detail) + '</small></div><span class="sgl-priority-label">' + esc(tail) + "</span></button>";
  }).join("") : empty("Нет незакрытых сигналов. Можно спокойно продолжать работу по текущим делам.");
  const inbox = state.operations.notifications || [];
  $("sgl-inbox-list").innerHTML = inbox.length ? inbox.slice(0, 7).map((item) => {
    const meta = [item.case_number ? num(item.case_number) : "", item.body || "", stamp(item.created_at)].filter(Boolean).join(" · ");
    return '<article class="sgl-inbox-item" data-severity="' + esc(item.severity || "info") + '"><button class="sgl-inbox-open" data-open-case="' + esc(item.case_number || "") + '" data-open-tab="' + esc(item.tab || "overview") + '"><strong>' + esc(item.title) + "</strong><small>" + esc(meta) + '</small></button><button class="sgl-ack-button" data-acknowledge="' + esc(item.id) + '">Разобрано</button></article>';
  }).join("") : empty("Новых уведомлений нет. Forum Watch и команда не оставили неразобранных сигналов.");
  const events = state.operations.events || [];
  $("sgl-activity-list").innerHTML = events.length ? events.slice(0, 9).map((item) => {
    const kind = String(item.action || "").includes("atlas") ? "atlas" : /(confirmed|completed|published|created|saved)/.test(item.action || "") ? "success" : /(error|changed|failed)/.test(item.action || "") ? "attention" : "neutral";
    const meta = [item.case_number ? num(item.case_number) : "", memberMeta(item), stamp(item.created_at)].filter(Boolean).join(" · ");
    return '<button class="sgl-activity-item" data-kind="' + kind + '" data-open-case="' + esc(item.case_number || "") + '" data-open-tab="' + esc(eventTab(item.action)) + '"><i></i><div><strong>' + esc(humanEvent(item)) + "</strong><small>" + esc(meta) + '</small></div><span class="sgl-item-time">Открыть</span></button>';
  }).join("") : empty("История операций появится после первых действий по делам.");
  renderHealth();
  renderNav();
}
function humanEvent(item) {
  return (EVENT_COPY[item.action] || "Зафиксировано действие по делу") + (item.actor_display ? " · " + item.actor_display : "");
}

function taskIsOverdue(item) {
  if (!item || item.status !== "open" || !item.due_at) return false;
  const due = new Date(item.due_at).getTime();
  return Number.isFinite(due) && due < Date.now();
}
function taskDueCopy(item) {
  if (!item.due_at) return "без срока";
  if (item.status === "done") return "закрыта " + stamp(item.completed_at || item.updated_at);
  if (item.status === "cancelled") return "отменена";
  return taskIsOverdue(item) ? "просрочена · " + fullStamp(item.due_at) : fullStamp(item.due_at);
}
function taskPriority(value) {
  return { critical: "Критический", high: "Высокий", normal: "Обычный", low: "Низкий" }[value] || "Обычный";
}
function taskMatches(item, query) {
  const needle = normalize(query);
  if (!needle) return true;
  return [item.title, item.description, item.case_number, item.case_title, item.case_request_type, item.owner_display].join(" ").toLocaleLowerCase("ru-RU").includes(needle);
}
function filteredTasks() {
  const viewer = Number(state.bootstrap && state.bootstrap.viewer && state.bootstrap.viewer.id || 0);
  return (state.tasks || []).filter((item) => {
    if (!taskMatches(item, state.workQuery)) return false;
    if (state.workView === "open") return item.status === "open";
    if (state.workView === "mine") return item.status === "open" && Number(item.owner_id) === viewer;
    if (state.workView === "overdue") return taskIsOverdue(item);
    if (state.workView === "unassigned") return item.status === "open" && !item.owner_id;
    if (state.workView === "done") return item.status === "done";
    return true;
  });
}
function renderWork() {
  const tasks = state.tasks || [];
  const open = tasks.filter((item) => item.status === "open");
  const viewer = Number(state.bootstrap && state.bootstrap.viewer && state.bootstrap.viewer.id || 0);
  const cards = [
    [open.length, "Открыто", "в рабочей очереди", "blue"],
    [open.filter(taskIsOverdue).length, "Просрочено", "требует решения", "red"],
    [open.filter((item) => Number(item.owner_id) === viewer).length, "Мои задачи", "назначены вам", "violet"],
    [open.filter((item) => !item.owner_id).length, "Без владельца", "нужно распределить", "amber"],
  ];
  $("sgl-work-summary").innerHTML = cards.map((item) => '<article class="sgl-work-summary-item" data-tone="' + item[3] + '"><strong>' + esc(item[0]) + '</strong><div><b>' + esc(item[1]) + '</b><span>' + esc(item[2]) + '</span></div></article>').join("");
  const rows = filteredTasks();
  $("sgl-work-list").innerHTML = rows.map((item) => {
    const complete = item.status === "open" ? '<button class="sgl-work-action is-complete" data-task-status="done" data-task-id="' + esc(item.id) + '" title="Отметить выполненной">' + svg("check") + '</button>' : "";
    return '<tr data-task-priority="' + esc(item.priority || "normal") + '"><td><button class="sgl-work-task-name" data-edit-task="' + esc(item.id) + '"><strong>' + esc(item.title) + '</strong><small>' + esc(item.description || item.source || "без дополнительного контекста") + '</small></button></td><td><button class="sgl-work-case" data-open-case="' + esc(item.case_number) + '" data-open-tab="overview"><b>' + esc(num(item.case_number)) + '</b><span>' + esc(item.case_title || "Кейс") + '</span></button></td><td><span class="sgl-work-owner' + (item.owner_id ? "" : " is-empty") + '">' + esc(item.owner_display || "Не назначен") + '</span></td><td><span class="sgl-work-due' + (taskIsOverdue(item) ? " is-overdue" : "") + '">' + esc(taskDueCopy(item)) + '</span></td><td><span class="sgl-priority-chip" data-priority="' + esc(item.priority || "normal") + '">' + esc(taskPriority(item.priority)) + '</span></td><td>' + pill(item.status) + '</td><td><div class="sgl-work-row-actions">' + complete + '<button class="sgl-work-action" data-edit-task="' + esc(item.id) + '" title="Редактировать">' + svg("more") + '</button></div></td></tr>';
  }).join("");
  $("sgl-work-empty").hidden = Boolean(rows.length);
  document.querySelectorAll("[data-work-view]").forEach((node) => node.classList.toggle("is-active", node.dataset.workView === state.workView));
}
function eventTab(action) {
  const name = text(action);
  if (name.includes("atlas")) return "atlas";
  if (name.includes("forum")) return "forum";
  if (name.includes("receipt")) return "finance";
  if (name.includes("message")) return "conversation";
  return "timeline";
}
function renderCases() {
  const items = filteredCases();
  $("sgl-case-list").innerHTML = items.map((item) => {
    const action = nextAction(item);
    return '<tr tabindex="0" aria-label="Открыть дело ' + esc(num(item.case_number) + ". " + client(item) + ". Следующее действие: " + action.title) + '" data-open-case="' + esc(item.case_number) + '" data-open-tab="overview"><td><span class="sgl-case-number">' + esc(num(item.case_number)) + '</span><span class="sgl-table-sub">' + esc(stamp(item.updated_at)) + '</span></td><td><span class="sgl-table-main">' + esc(client(item)) + '</span><span class="sgl-table-sub">' + esc(item.request_type || "Вид работы не указан") + '</span></td><td><span class="sgl-table-main">' + esc(item.lead_lawyer_display || "Адвокат не назначен") + '</span><span class="sgl-table-sub">' + esc(item.secretary_display || "Секретарь не назначен") + '</span></td><td>' + pill(item.status) + '</td><td><span class="sgl-table-main">' + esc(action.title) + '</span><span class="sgl-table-sub">' + esc(action.tab === "finance" ? "финансовый контур" : "следующий шаг") + '</span></td><td><span class="sgl-table-main">' + esc(stamp(item.updated_at)) + '</span><span class="sgl-table-sub">' + esc(item.event_count ? item.event_count + " событий" : "нет событий") + '</span></td><td><button class="sgl-table-action" data-open-case="' + esc(item.case_number) + '" data-open-tab="' + esc(action.tab) + '">' + svg("arrow") + "</button></td></tr>";
  }).join("");
  $("sgl-case-empty").hidden = Boolean(items.length);
}
function renderAtlasIndex() {
  const cases = (state.bootstrap && state.bootstrap.cases ? state.bootstrap.cases.items : []).slice().sort((left, right) => {
    const order = { critical: 0, high: 1, normal: 2, low: 3 };
    return (order[nextAction(left).priority] || 2) - (order[nextAction(right).priority] || 2);
  });
  $("sgl-atlas-case-picker").innerHTML = cases.length ? cases.slice(0, 9).map((item) => '<button data-open-case="' + esc(item.case_number) + '" data-open-tab="atlas"><b>' + esc(num(item.case_number)) + '</b><div><strong>' + esc(client(item)) + '</strong><small>' + esc(nextAction(item).title) + '</small></div>' + svg("arrow") + "</button>").join("") : empty("Сначала создайте кейс — Atlas работает только в его закрытом контексте.");
}
function observation(publication) {
  return (state.forum.observations || []).find((item) => Number(item.publication_id) === Number(publication.id)) || null;
}
function caseByNumber(number) {
  return (state.bootstrap && state.bootstrap.cases ? state.bootstrap.cases.items || [] : []).find((item) => Number(item.case_number) === Number(number)) || null;
}
function renderForum() {
  const enabled = Boolean(state.forum.watch && state.forum.watch.enabled);
  const watch = state.forum.watch || {};
  const runner = watch.runner || {};
  $("sgl-forum-health").className = "sgl-forum-health " + (enabled ? "is-running" : "is-disabled");
  $("sgl-forum-health").innerHTML = "<div><strong>" + (enabled ? "Автопроверка активна" : "Forum Watch выключен") + "</strong><span>" + esc(enabled ? "Проверка публикаций выполняется раз в " + Math.round((watch.interval_seconds || 900) / 60) + " мин. Отслеживается тем: " + (watch.tracked_publications || 0) + ". " + (runner.last_finished_at ? "Последний проход: " + stamp(runner.last_finished_at) + "." : "Первый проход ещё не завершён.") : "Для запуска настройте SGL_FORUM_WATCH_ENABLED и авторизованную сессию браузера. Вход и captcha остаются ручными.") + "</span></div><b>" + (enabled ? runner.state === "checking" ? "CHECKING" : "SCHEDULED" : "DISABLED") + "</b>";
  const alerts = state.forum.alerts || [];
  $("sgl-forum-alert-center").innerHTML = '<header><div><span>СИГНАЛЫ FORUM WATCH</span><h2>Изменения, которые ждут решения</h2></div><b>' + esc((watch.unacknowledged_alerts != null ? watch.unacknowledged_alerts : alerts.filter((item) => !item.acknowledged_at).length) + " открыто") + '</b></header><div class="sgl-forum-alert-list">' + (alerts.length ? alerts.slice(0, 8).map((item) => {
    const pending = !item.acknowledged_at;
    return '<article class="sgl-forum-alert ' + (pending ? "is-pending" : "is-acknowledged") + '" data-severity="' + esc(item.severity || "info") + '"><div class="sgl-forum-alert-copy"><span>' + esc(item.case_number ? num(item.case_number) : "FORUM") + " · " + (pending ? "нужно разобрать" : "разобрано") + '</span><strong>' + esc(item.title || "Сигнал Forum Watch") + '</strong><p>' + esc(item.body || "Изменение зафиксировано без текста.") + '</p><small>' + esc(stamp(item.created_at || item.updated_at)) + '</small></div><div class="sgl-forum-alert-actions"><button class="sgl-inline-action" data-open-case="' + esc(item.case_number || "") + '" data-open-tab="forum">Открыть кейс</button>' + (pending ? '<button class="sgl-inline-action" data-forum-alert-task="' + esc(item.id) + '">В задачу</button><button class="sgl-inline-action" data-acknowledge="' + esc(item.id) + '">Разобрано</button>' : "") + '</div></article>';
  }).join("") : empty("Forum Watch пока не зафиксировал сигналов. История появится после проверки опубликованных тем.")) + "</div>";
  const items = state.forum.publications || [];
  $("sgl-forum-list").innerHTML = items.map((item) => {
    const watch = observation(item);
    const relatedCase = caseByNumber(item.case_number);
    const signal = watch && watch.status === "changed" ? "Требует разбора" : watch && watch.status === "error" ? "Нужен ручной вход" : "Нет нового сигнала";
    const delivered = watch && watch.last_notified_at ? "доставлено" : watch && ["changed", "error"].includes(watch.status) ? "не доставлено" : "—";
    return '<tr data-open-case="' + esc(item.case_number) + '" data-open-tab="forum"><td><span class="sgl-case-number">' + esc(num(item.case_number)) + '</span><span class="sgl-table-sub">' + esc(relatedCase ? client(relatedCase) : "Кейс") + '</span></td><td><span class="sgl-table-main">' + esc(item.title || "Черновик иска") + '</span><span class="sgl-table-sub">' + esc(item.forum_url || item.target_url || "Ссылка не задана") + '</span></td><td>' + pill(watch && watch.status === "changed" ? "changed" : item.status) + '</td><td><span class="sgl-table-main">' + esc(stamp(watch && watch.last_checked_at || item.updated_at)) + '</span><span class="sgl-table-sub">' + esc(watch && watch.last_error || "Нет ошибок") + '</span></td><td><span class="sgl-table-main">' + esc(signal) + '</span><span class="sgl-table-sub">' + esc(watch && watch.thread_excerpt || "") + '</span></td><td><span class="sgl-table-main">' + esc(delivered) + '</span><span class="sgl-table-sub">' + esc(watch && watch.last_notified_at ? stamp(watch.last_notified_at) : "") + '</span></td><td><button class="sgl-table-action" data-open-case="' + esc(item.case_number) + '" data-open-tab="forum">' + svg("arrow") + "</button></td></tr>";
  }).join("");
  $("sgl-forum-empty").hidden = Boolean(items.length);
}
function plural(value) {
  const n = Math.abs(Number(value)) % 100;
  const tail = n % 10;
  if (n > 10 && n < 20) return "профилей";
  if (tail > 1 && tail < 5) return "профиля";
  if (tail === 1) return "профиль";
  return "профилей";
}
function renderPeople() {
  const clients = state.directory.clients || [];
  const lawyers = state.directory.lawyers || [];
  $("sgl-client-count").textContent = clients.length + " " + plural(clients.length);
  $("sgl-lawyer-count").textContent = lawyers.length + " " + plural(lawyers.length);
  $("sgl-client-list").innerHTML = peopleRows(clients, "client");
  $("sgl-lawyer-list").innerHTML = peopleRows(lawyers, "lawyer");
}
function peopleRows(items, kind) {
  if (!items.length) return empty("Подходящих профилей пока нет.");
  return items.slice(0, 35).map((item) => {
    const name = item.client_nick || item.lawyer_nick || "Без имени";
    const meta = kind === "client" ? [item.static_id && "static " + item.static_id, item.phone, item.discord_user_id && "Discord " + item.discord_user_id].filter(Boolean).join(" · ") : [item.static_id && "static " + item.static_id, item.phone, item.email].filter(Boolean).join(" · ");
    return '<article class="sgl-person-row"><span class="sgl-avatar' + (kind === "lawyer" ? " is-lawyer" : "") + '">' + esc(name.slice(0, 1).toUpperCase()) + '</span><div><strong>' + esc(name) + '</strong><small>' + esc(meta || "Данные профиля не заполнены") + "</small></div></article>";
  }).join("");
}
function renderArchive() {
  const items = state.bootstrap && state.bootstrap.archives ? state.bootstrap.archives.items : [];
  $("sgl-archive-list").innerHTML = items.map((item) => '<tr><td><span class="sgl-case-number">' + esc(num(item.case_number)) + '</span><span class="sgl-table-sub">' + esc(item.original_channel_name || "Архив дела") + '</span></td><td><span class="sgl-table-main">' + esc(stamp(item.snapshot_completed_at || item.created_at)) + '</span><span class="sgl-table-sub">' + (item.source_deleted_at ? "снимок закреплён" : "снимок создаётся") + '</span></td><td><span class="sgl-table-main">' + esc((item.message_count || 0) + " сообщений") + '</span><span class="sgl-table-sub">' + esc((item.attachment_count || 0) + " файлов") + '</span></td><td>' + pill(item.status || (item.source_deleted_at ? "archived" : "created")) + '</td><td>' + (item.source_deleted_at ? '<button class="sgl-inline-action" data-restore-archive="' + esc(item.case_number) + '">Восстановить</button>' : '<span class="sgl-table-sub">ожидание</span>') + "</td></tr>").join("");
  $("sgl-archive-empty").hidden = Boolean(items.length);
}
function renderScreen() {
  if (state.screen === "today") renderToday();
  if (state.screen === "work") renderWork();
  if (state.screen === "cases") renderCases();
  if (state.screen === "atlas") renderAtlasIndex();
  if (state.screen === "forum") renderForum();
  if (state.screen === "people") renderPeople();
  if (state.screen === "archive") renderArchive();
}

async function refreshCore(silent) {
  if (state.refreshing) return;
  state.refreshing = true;
  syncStatus("loading", "Обновляю данные");
  try {
    const data = await Promise.all([api("/api/sgl/bootstrap"), api("/api/sgl/operations"), api("/api/sgl/forum/operations"), api("/api/sgl/tasks?limit=200")]);
    state.bootstrap = data[0];
    state.operations = data[1];
    state.forum = data[2];
    state.tasks = data[3].tasks || [];
    state.lastRefreshAt = Date.now();
    $("sgl-viewer-name").textContent = state.bootstrap.viewer && state.bootstrap.viewer.name || "Сотрудник SGL";
    renderScreen();
    if (state.detail) renderCase();
    syncStatus("ready", "Актуально сейчас");
    if (!silent) toast("Данные SGL обновлены.");
  } catch (error) {
    syncStatus("error", "Нет синхронизации");
    if (!silent) toast(error.message || "Не удалось обновить SGL.", true);
  } finally {
    state.refreshing = false;
  }
}
async function loadDirectory(query) {
  try {
    state.directory = await api("/api/sgl/directory?q=" + encodeURIComponent(query || ""));
    if (state.screen === "people") renderPeople();
    clientProfiles();
  } catch (error) {
    toast(error.message || "Не удалось загрузить реестр.", true);
  }
}

async function openCase(number) {
  if (!number) return;
  if (state.caseNumber !== number || !state.detail) {
    state.messageReply = null;
    stopPolling();
    // Do not let the periodic core refresh repaint the previously opened case
    // while the next dossier is still on the wire.
    state.caseNumber = null;
    state.detail = null;
    renderCaseLoading(number);
    try {
      state.detail = await api("/api/sgl/cases/" + number);
      if (state.detail.permissions && state.detail.permissions.manage) {
        try {
          const journal = await api("/api/sgl/cases/" + number + "/decisions?resolved=1");
          state.detail.decisions = journal.decisions || [];
        } catch (_) {
          state.detail.decisions = [];
        }
      } else {
        state.detail.decisions = [];
      }
      state.caseNumber = number;
      state.casePoll = setInterval(() => { void pollMessages(); }, 15000);
    } catch (error) {
      $("sgl-case-workspace").removeAttribute("aria-busy");
      toast(error.message || "Не удалось открыть дело.", true);
      goScreen("cases", true);
      return;
    }
  }
  document.querySelectorAll(".sgl-screen").forEach((node) => node.classList.remove("is-active"));
  document.querySelectorAll(".sgl-admin-nav [data-go]").forEach((node) => node.classList.toggle("is-active", node.dataset.go === "cases"));
  $("sgl-case-workspace").hidden = false;
  $("sgl-breadcrumb").textContent = "SGL / кейсы / " + num(number);
  $("sgl-page-title").textContent = "Дело " + num(number);
  renderCase();
}
async function pollMessages() {
  if (!state.detail || !state.detail.case) return;
  const messages = state.detail.messages || [];
  const last = messages.length ? messages[messages.length - 1].id : "";
  try {
    const result = await api("/api/sgl/cases/" + state.detail.case.case_number + "/messages?after=" + encodeURIComponent(last));
    if (result.messages && result.messages.length) {
      state.detail.messages.push.apply(state.detail.messages, result.messages);
      if (state.caseTab === "conversation") renderCasePanel();
      toast("В Discord появились новые сообщения: " + result.messages.length + ".");
    }
  } catch (_) {}
}
function caseTab(tab) {
  state.caseTab = normalizeCaseTab(tab);
  history.replaceState({}, "", "/sgl/cases/" + state.caseNumber + "#" + state.caseTab);
  renderCase();
  focusActiveCaseTab({ revealPanel: true });
}
function focusActiveCaseTab(options) {
  const settings = options || {};
  const active = $("sgl-case-tabs") && $("sgl-case-tabs").querySelector(".is-active");
  if (active) {
    const reduced = globalThis.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    active.scrollIntoView({ block: "nearest", inline: "center", behavior: reduced ? "auto" : "smooth" });
  }
  if (!settings.revealPanel || !globalThis.matchMedia || !matchMedia("(max-width: 760px)").matches) return;
  requestAnimationFrame(() => {
    const panel = $("sgl-case-panel");
    if (panel) panel.scrollIntoView({ block: "start", behavior: "smooth" });
  });
}
function caseTabNodes() {
  const list = $("sgl-case-tabs");
  return list ? Array.from(list.querySelectorAll("[data-case-tab]")) : [];
}
function syncCaseTabAccessibility() {
  const list = $("sgl-case-tabs");
  const panel = $("sgl-case-panel");
  const tabs = caseTabNodes();
  if (!list || !panel || !tabs.length) return;
  list.setAttribute("role", "tablist");
  list.setAttribute("aria-label", "Разделы дела");
  list.setAttribute("aria-orientation", "horizontal");
  panel.setAttribute("role", "tabpanel");
  tabs.forEach((tab) => {
    const selected = tab.dataset.caseTab === state.caseTab;
    tab.id = "sgl-case-tab-" + tab.dataset.caseTab;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", panel.id);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected) panel.setAttribute("aria-labelledby", tab.id);
  });
}
function moveCaseTabByKeyboard(event) {
  const source = event.target;
  const tab = source instanceof Element && source.closest("#sgl-case-tabs [data-case-tab]");
  if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return false;
  const tabs = caseTabNodes();
  const index = tabs.indexOf(tab);
  if (index < 0) return false;
  let next = index;
  if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
  if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = tabs.length - 1;
  event.preventDefault();
  tabs[next].focus({ preventScroll: true });
  caseTab(tabs[next].dataset.caseTab);
  return true;
}
function openCaseRowByKeyboard(event) {
  const source = event.target;
  const row = source instanceof Element && source.closest("#sgl-case-list tr[data-open-case]");
  if (!row || !["Enter", " ", "Spacebar"].includes(event.key)) return false;
  if (source !== row && source.closest("button, a, input, select, textarea")) return false;
  event.preventDefault();
  goCase(row.dataset.openCase, row.dataset.openTab || "overview");
  return true;
}
function renderCase() {
  if (!state.detail || !state.detail.case) return;
  const item = state.detail.case;
  $("sgl-case-kicker").textContent = "SGL / ДЕЛО " + num(item.case_number);
  $("sgl-case-title").textContent = client(item);
  $("sgl-case-header-meta").innerHTML = pill(item.status) + '<span class="sgl-status-pill">' + esc(item.request_type || "Тип не указан") + '</span><span class="sgl-status-pill" title="' + esc(fullStamp(item.updated_at)) + '">обновлён ' + esc(stamp(item.updated_at)) + "</span>";
  const discord = $("sgl-case-discord");
  discord.href = item.discord_url || "#";
  discord.hidden = !item.discord_url;
  $("sgl-case-close").hidden = !state.detail.permissions || !state.detail.permissions.manage || ["closed", "archived"].includes(item.status);
  document.querySelectorAll("[data-case-tab]").forEach((node) => node.classList.toggle("is-active", node.dataset.caseTab === state.caseTab));
  syncCaseTabAccessibility();
  renderCaseHealth(item);
  renderIdentity();
  renderSidecar();
  renderCasePanel();
  $("sgl-case-workspace").removeAttribute("aria-busy");
  requestAnimationFrame(() => focusActiveCaseTab());
}
function renderIdentity() {
  const item = state.detail.case;
  const statusValue = item.status;
  const steps = [
    ["Создано", ["created", "awaiting_situation", "awaiting_link", "awaiting_close", "closed", "archived"].includes(statusValue)],
    ["Факты", Boolean(item.situation_text)],
    ["Иск", Boolean(item.claim_link)],
    ["Закрытие", ["closed", "archived"].includes(statusValue)],
  ];
  const active = steps.findIndex((row) => !row[1]);
  $("sgl-case-identity").innerHTML = '<section class="sgl-case-card"><span>СТОРОНЫ</span><h3>Участники дела</h3>' + [
    ["Клиент", client(item)], ["Ведущий адвокат", item.lead_lawyer_display || "Не назначен"],
    ["Секретарь", item.secretary_display || "Не назначен"], ["Discord", item.channel_id ? "канал " + item.channel_id : "канал не создан"],
  ].map((row) => '<div class="sgl-person-fact"><span>' + esc(row[0]) + "</span><b>" + esc(row[1]) + "</b></div>").join("") + '</section><section class="sgl-case-card"><span>ЭТАПЫ</span><h3>Маршрут дела</h3><div class="sgl-roadmap">' + steps.map((row, index) => '<div class="sgl-roadmap-step ' + (row[1] ? "is-done" : index === active ? "is-current" : "") + '"><i>' + (row[1] ? svg("check") : "") + "</i><b>" + esc(row[0]) + "</b></div>").join("") + "</div></section>";
}
function renderSidecar() {
  const item = state.detail.case;
  const action = nextAction(item);
  const tasks = (state.detail.tasks || []).filter((task) => task.status === "open");
  const notes = state.detail.ai_notes || [];
  const taskRows = tasks.length ? tasks.slice(0, 5).map((task) => '<div class="sgl-task-mini"><div><strong>' + esc(task.title) + '</strong><small>' + esc([task.owner_display, task.due_at ? fullStamp(task.due_at) : ""].filter(Boolean).join(" · ") || "без срока") + '</small></div><button data-complete-task="' + esc(task.id) + '" title="Отметить выполненной">' + svg("check") + "</button></div>").join("") : '<small>Нет открытых задач.</small>';
  const playbooks = state.detail.permissions && state.detail.permissions.manage ? '<div class="sgl-playbook"><small>Быстрый плейбук</small><div>' + Object.keys(PLAYBOOKS).map((key) => '<button data-apply-playbook="' + key + '">' + esc(PLAYBOOKS[key].label) + "</button>").join("") + "</div></div>" : "";
  const atlasCopy = notes.length ? esc(text(notes[0].answer).replace(/\s+/g, " ").slice(0, 180) || "Анализ сохранён в деле.") : "Соберите brief, риски или план — ответ останется в журнале дела.";
  $("sgl-case-sidecar").innerHTML = '<section class="sgl-case-card sgl-side-action"><span>СЛЕДУЩЕЕ ДЕЙСТВИЕ</span><strong>' + esc(action.title) + '</strong><p>' + esc(num(item.case_number) + " · " + (action.tab === "finance" ? "финансы" : action.tab === "forum" ? "форум" : "рабочий контур")) + '</p><button class="sgl-button sgl-button-primary" data-case-tab="' + esc(action.tab) + '">Открыть действие</button></section><section class="sgl-case-card"><span>ЗАДАЧИ</span><h3>Команда</h3>' + taskRows + '<button class="sgl-inline-action" data-add-task="1">Добавить задачу</button>' + playbooks + '</section><section class="sgl-case-card sgl-side-action"><span>ATLAS</span><strong>' + (notes.length ? "Последний вывод: " + esc(text(notes[0].kind || "analysis").toUpperCase()) : "Контекст ещё не разобран") + '</strong><p>' + atlasCopy + '</p><button class="sgl-button sgl-button-secondary" data-case-tab="atlas">' + (notes.length ? "Открыть Atlas" : "Запустить Atlas") + "</button></section>";
}
function panel(title, eyebrow, content) {
  return '<section class="sgl-panel"><header class="sgl-case-panel-header"><div><span>' + esc(eyebrow) + "</span><h2>" + esc(title) + "</h2></div></header>" + content + "</section>";
}
function renderCasePanel() {
  const renderers = {
    overview: overviewPanel, conversation: conversationPanel, timeline: timelinePanel,
    decisions: decisionsPanel, evidence: evidencePanel, finance: financePanel, forum: forumPanel, atlas: atlasPanel, archive: caseArchivePanel,
  };
  $("sgl-case-panel").innerHTML = (renderers[state.caseTab] || overviewPanel)();
}
function overviewPanel() {
  const item = state.detail.case;
  const fields = [
    ["Вид работы", item.request_type || "Не указан", false], ["Static ID", item.static_id || "Не указан", false],
    ["Телефон", item.phone || "Не указан", false], ["Ссылка на иск", item.claim_link || "Пока нет", false],
    ["Обстоятельства", item.situation_text || "Факты ещё не описаны", true],
  ];
  const facts = fields.map((row) => {
    const href = row[0] === "Ссылка на иск" ? safeUrl(row[1]) : "";
    return '<article class="sgl-fact-box' + (row[2] ? " is-wide" : "") + '"><span>' + esc(row[0]) + '</span>' + (href ? '<a href="' + esc(href) + '" target="_blank" rel="noreferrer">' + esc(row[1]) + "</a>" : "<b>" + esc(row[1]) + "</b>") + "</article>";
  }).join("");
  const checks = [
    ["Карточка клиента", Boolean(item.client_nick && item.static_id && item.phone), "сведения клиента"],
    ["Обстоятельства", Boolean(item.situation_text), "фактическая основа"],
    ["Ссылка на иск", Boolean(item.claim_link), "форум или документ"],
    ["Открытые задачи", !(state.detail.tasks || []).some((task) => task.status === "open"), "рабочая очередь"],
  ].map((row) => '<div class="sgl-checklist-row ' + (row[1] ? "is-done" : "is-missing") + '"><i>' + svg(row[1] ? "check" : "more") + "</i><b>" + esc(row[0]) + "</b><small>" + esc(row[1] ? "готово" : row[2]) + "</small></div>").join("");
  return panel("Краткая картина дела", "ОБЗОР", '<div class="sgl-overview-grid">' + facts + '</div><div class="sgl-checklist">' + checks + "</div>");
}
function messageDiscordId(item) {
  const value = item && item.discord_message_id;
  return value == null || text(value).trim() === "" ? "" : text(value);
}
function messageExcerpt(value, limit) {
  const compact = text(value).replace(/\s+/g, " ").trim() || "Вложение без текста";
  const max = limit || 118;
  return compact.length > max ? compact.slice(0, max - 1).trim() + "…" : compact;
}
function replyMessage(messageId) {
  const item = (state.detail && state.detail.messages || []).find((entry) => messageDiscordId(entry) === String(messageId));
  if (!item) return;
  state.messageReply = {
    discordId: messageDiscordId(item),
    author: item.author_display || "Участник",
    excerpt: messageExcerpt(item.content, 150),
  };
  renderCasePanel();
  requestAnimationFrame(() => {
    const composer = $("sgl-message-content");
    if (composer) composer.focus();
  });
}
function cancelMessageReply() {
  state.messageReply = null;
  renderCasePanel();
}
function renderMessageFiles(input) {
  const holder = input && input.closest("form") && input.closest("form").querySelector(".sgl-message-files");
  if (!holder) return;
  const files = Array.from(input.files || []).slice(0, 10);
  holder.innerHTML = files.map((file) => {
    const size = file.size >= 1024 * 1024 ? (file.size / (1024 * 1024)).toFixed(1) + " МБ" : Math.max(1, Math.round(file.size / 1024)) + " КБ";
    return '<span>' + svg("file") + '<b>' + esc(file.name || "Вложение") + "</b><small>" + esc(size) + "</small></span>";
  }).join("");
}
function jumpToMessage(messageId) {
  const target = Array.from(document.querySelectorAll(".sgl-message[data-message-id]")).find((node) => node.dataset.messageId === String(messageId));
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.remove("is-highlighted");
  requestAnimationFrame(() => target.classList.add("is-highlighted"));
  clearTimeout(jumpToMessage.timer);
  jumpToMessage.timer = setTimeout(() => target.classList.remove("is-highlighted"), 1750);
}
function conversationPanel() {
  const messages = state.detail.messages || [];
  const messageIndex = new Map(messages.map((item) => [messageDiscordId(item), item]).filter(([id]) => Boolean(id)));
  const list = messages.length ? messages.map((item) => {
    const attachments = Array.isArray(item.attachments) ? item.attachments : [];
    const discordId = messageDiscordId(item);
    const replyId = text(item.reply_to_discord_message_id || "");
    const replied = replyId ? messageIndex.get(replyId) : null;
    const links = attachments.map((file) => {
      const href = safeUrl(file.url || file.proxy_url);
      return href ? '<a href="' + esc(href) + '" target="_blank" rel="noreferrer">' + svg("file") + esc(file.name || file.filename || "Вложение") + "</a>" : "";
    }).join("");
    const quote = replyId ? '<button type="button" class="sgl-message-quote" data-jump-message="' + esc(replyId) + '" title="Перейти к исходному сообщению"><span>Ответ на ' + esc(replied ? replied.author_display || "участника" : "сообщение Discord") + '</span><b>' + esc(replied ? messageExcerpt(replied.content) : "Исходное сообщение пока не загружено") + "</b></button>" : "";
    const actions = discordId && !item.deleted_at ? '<div class="sgl-message-actions"><button type="button" data-reply-message="' + esc(discordId) + '" aria-label="Ответить ' + esc(item.author_display || "на сообщение") + '">Ответить</button></div>' : "";
    const messageTime = item.edited_at || item.created_at;
    return '<article class="sgl-message ' + (item.origin === "web" ? "is-web " : "") + (item.deleted_at ? "is-deleted" : "") + '" data-message-id="' + esc(discordId) + '"><div class="sgl-message-head"><b>' + esc(item.author_display || "Участник") + '</b><small title="' + esc(fullStamp(messageTime)) + '">' + esc(item.deleted_at ? "удалено" : stamp(messageTime)) + "</small></div>" + quote + "<p>" + esc(item.deleted_at ? "Сообщение удалено в Discord." : item.content || "Вложение без текста") + "</p>" + (links ? '<div class="sgl-attachments">' + links + "</div>" : "") + actions + "</article>";
  }).join("") : empty("Переписка появится после первого сообщения в Discord или из этого кабинета.");
  const reply = state.messageReply;
  const replyBanner = reply ? '<div class="sgl-message-replying"><div><span>ОТВЕТ В DISCORD</span><b>На ' + esc(reply.author) + '</b><small>' + esc(reply.excerpt) + '</small></div><input type="hidden" name="reply_to" value="' + esc(reply.discordId) + '"><button type="button" data-cancel-message-reply aria-label="Отменить ответ">' + svg("close") + "</button></div>" : "";
  const form = state.detail.permissions && state.detail.permissions.write ? '<form id="sgl-message-form" class="sgl-message-compose">' + replyBanner + '<textarea id="sgl-message-content" name="content" maxlength="2000" placeholder="Сообщение уйдёт в защищённый Discord-канал кейса…"></textarea><div class="sgl-message-compose-actions"><label class="sgl-message-attach"><input class="sgl-message-upload" type="file" name="files" multiple aria-label="Прикрепить файлы к сообщению"><svg><use href="#sgl-i-file"></use></svg><span>Файлы</span></label><button class="sgl-button sgl-button-primary" type="submit">Отправить</button></div><div class="sgl-message-files" aria-live="polite"></div></form>' : "";
  return panel("Живая переписка Discord", "ДИАЛОГ", '<div class="sgl-message-list">' + list + "</div>" + form);
}
function timelinePanel() {
  const events = state.detail.events || [];
  const list = events.length ? events.map((item) => {
    const kind = text(item.action).includes("atlas") ? "atlas" : /(confirmed|completed|published|created|saved)/.test(item.action || "") ? "done" : /(error|changed|failed)/.test(item.action || "") ? "forum" : "";
    const detail = item.action === "forum_topic_changed" ? "изменение зафиксировано" : item.action === "task_created" ? "появилось следующее действие" : item.action === "task_completed" ? "рабочий шаг закрыт" : item.action === "web_case_updated" ? "данные и доступы синхронизированы" : "";
    return '<article class="sgl-event" data-action="' + kind + '"><i></i><div><strong>' + esc(humanEvent(item)) + '</strong><small title="' + esc(fullStamp(item.created_at)) + '">' + esc([stamp(item.created_at), detail].filter(Boolean).join(" · ")) + "</small></div></article>";
  }).join("") : empty("В этом деле ещё нет аудиторских событий.");
  return panel("История и аудит", "ХРОНОЛОГИЯ", '<div class="sgl-event-list">' + list + "</div>");
}
function staffOptionMarkup(selected) {
  const members = new Map();
  const add = (id, display) => {
    if (id && !members.has(String(id))) members.set(String(id), display || "Сотрудник " + id);
  };
  (state.directory.lawyers || []).forEach((item) => add(item.discord_user_id, item.lawyer_nick || item.display_name));
  (state.tasks || []).forEach((item) => add(item.owner_id, item.owner_display));
  const current = state.detail && state.detail.case;
  if (current) {
    add(current.lead_lawyer_id, current.lead_lawyer_display);
    add(current.secretary_id, current.secretary_display);
  }
  add(selected, (state.detail && state.detail.decisions || []).find((item) => Number(item.target_user_id) === Number(selected))?.target_display);
  return '<option value="">Без адресата</option>' + Array.from(members.entries()).map((entry) => '<option value="' + esc(entry[0]) + '"' + (String(selected || "") === entry[0] ? " selected" : "") + ">" + esc(entry[1] + " · " + entry[0]) + "</option>").join("");
}
function decisionKind(value) {
  return { decision: "Решение", handoff: "Передача", risk: "Риск", note: "Заметка" }[value] || "Запись";
}
function decisionStatus(value) {
  return { open: "Нужно прочитать", read: "Прочитано", acknowledged: "Подтверждено", superseded: "Заменено" }[value] || status(value);
}
function decisionCard(item) {
  const action = item.status === "open" ? '<button class="sgl-inline-action" data-decision-status="read" data-decision-id="' + esc(item.id) + '">Прочитано</button>' : item.status === "read" ? '<button class="sgl-inline-action" data-decision-status="acknowledged" data-decision-id="' + esc(item.id) + '">Подтвердить</button>' : "";
  const target = item.target_display ? "→ " + item.target_display : "Без адресата";
  return '<article class="sgl-decision-card" data-kind="' + esc(item.kind || "note") + '" data-status="' + esc(item.status || "open") + '"><header><div><span>' + esc(decisionKind(item.kind)) + '</span><h3>' + esc(item.title) + '</h3></div><b>' + esc(decisionStatus(item.status)) + '</b></header><p>' + esc(item.body) + '</p><footer><span>' + esc([item.created_by_display || "SGL", target, stamp(item.created_at)].filter(Boolean).join(" · ")) + '</span><div>' + action + '</div></footer></article>';
}
function decisionsPanel() {
  const items = state.detail.decisions || [];
  const open = items.filter((item) => ["open", "read"].includes(item.status));
  const cards = items.length ? items.map(decisionCard).join("") : empty("Внутренних решений и передач пока нет. Они не публикуются в Discord и не видны участникам дела.");
  const composer = state.detail.permissions && state.detail.permissions.manage ? '<form id="sgl-decision-form" class="sgl-decision-composer"><header><span>НОВАЯ ВНУТРЕННЯЯ ЗАПИСЬ</span><h3>Зафиксировать решение</h3><p>Это приватный журнал бюро: запись остаётся неизменяемой и не уходит клиенту или в Discord.</p></header><div class="sgl-form-grid-inline"><label>Тип<select name="kind"><option value="decision">Решение</option><option value="risk">Риск</option><option value="handoff">Передача</option><option value="note">Заметка</option></select></label><label>Адресат<select name="target_user_id">' + staffOptionMarkup("") + '</select></label></div><label>Краткий заголовок<input name="title" maxlength="240" required placeholder="Например, срок подачи подтверждаем до 18:00"></label><label>Контекст и договорённость<textarea name="body" maxlength="8000" required placeholder="Что проверено, на чём остановились, что должен сделать адресат…"></textarea></label><button class="sgl-button sgl-button-primary" type="submit">Сохранить в журнал</button></form>' : "";
  const metrics = '<div class="sgl-decision-metrics"><article><span>Открытые</span><strong>' + esc(open.length) + '</strong><small>требуют прочтения или подтверждения</small></article><article><span>Передачи</span><strong>' + esc(items.filter((item) => item.kind === "handoff" && ["open", "read"].includes(item.status)).length) + '</strong><small>в работе у команды</small></article><article><span>Журнал</span><strong>private</strong><small>не виден клиенту и Discord</small></article></div>';
  return panel("Решения и передачи", "PRIVATE DECISION LOG", metrics + '<div class="sgl-decision-layout"><div class="sgl-decision-list">' + cards + "</div>" + composer + "</div>");
}
function evidenceInput(label, name, value, area) {
  return '<label>' + esc(label) + (area ? '<textarea name="' + esc(name) + '" maxlength="8000">' + esc(value || "") + '</textarea>' : '<input name="' + esc(name) + '" value="' + esc(value || "") + '">') + "</label>";
}
function evidenceType(name, mime) {
  const value = (text(name) + " " + text(mime)).toLocaleLowerCase("en-US");
  if (/\.(png|jpe?g|gif|webp|bmp|svg)(\?|$)/.test(value) || value.includes("image/")) return "image";
  if (/^https?:\/\//.test(text(name)) || value.includes("link")) return "link";
  return "file";
}
function evidenceSources() {
  const sources = [];
  const knownUrls = new Set();
  const add = (item) => {
    const href = safeUrl(item.href);
    const key = href || item.id;
    if (!key || knownUrls.has(key)) return;
    knownUrls.add(key);
    sources.push({ ...item, href, kind: item.kind || evidenceType(item.name, item.mime) });
  };
  const messages = state.detail && state.detail.messages || [];
  messages.forEach((message) => {
    const meta = [message.author_display || "Участник", stamp(message.created_at || message.edited_at)].filter(Boolean).join(" · ");
    (Array.isArray(message.attachments) ? message.attachments : []).forEach((file, index) => add({
      id: "attachment:" + message.id + ":" + index,
      name: file.name || file.filename || "Вложение из Discord",
      href: file.url || file.proxy_url || "",
      mime: file.content_type || file.mime_type || "",
      meta, origin: "Discord · вложение", messageId: message.id,
    }));
    const contentLinks = text(message.content).match(/https?:\/\/[^\s<>"']+/g) || [];
    contentLinks.forEach((href, index) => add({
      id: "message-link:" + message.id + ":" + index,
      name: href.replace(/^https?:\/\//, "").slice(0, 78), href,
      meta, origin: "Discord · ссылка", messageId: message.id, kind: "link",
    }));
  });
  const item = state.detail && state.detail.case || {};
  if (item.passport_url) add({ id: "case:passport", name: "Документ клиента", href: item.passport_url, meta: "Карточка дела", origin: "Карточка · документ", kind: "link" });
  if (item.claim_link) add({ id: "case:claim", name: "Опубликованный иск", href: item.claim_link, meta: "Карточка дела", origin: "Карточка · иск", kind: "link" });
  return sources;
}
function evidenceCard(source) {
  const open = source.href ? '<a class="sgl-evidence-open" href="' + esc(source.href) + '" target="_blank" rel="noreferrer">Открыть ' + svg("arrow") + '</a>' : '<button class="sgl-evidence-open" data-case-tab="conversation">В диалог ' + svg("arrow") + "</button>";
  const typeCopy = { image: "Изображение", link: "Ссылка", file: "Файл" }[source.kind] || "Материал";
  return '<article class="sgl-evidence-item" data-type="' + esc(source.kind) + '"><span class="sgl-evidence-icon">' + svg(source.kind === "image" ? "image" : source.kind === "link" ? "link" : "file") + '</span><div class="sgl-evidence-copy"><small>' + esc(typeCopy + " · " + source.origin) + '</small><strong title="' + esc(source.name) + '">' + esc(source.name) + '</strong><span>' + esc(source.meta || "Без метаданных") + '</span></div><div class="sgl-evidence-actions">' + open + '<button class="sgl-evidence-task" data-evidence-task="' + esc(source.id) + '" title="Создать задачу по материалу">' + svg("task") + '</button></div></article>';
}
function openEvidenceTask(id) {
  const source = evidenceSources().find((item) => item.id === id);
  if (!source) return;
  const description = [source.origin, source.meta, source.href].filter(Boolean).join("\n");
  openTask({ title: "Проверить материал: " + source.name.slice(0, 180), description, priority: "normal" });
}
function openForumAlertTask(id) {
  const alert = (state.forum.alerts || []).find((item) => Number(item.id) === Number(id));
  if (!alert) return;
  openTask({ title: "Разобрать Forum Watch: " + text(alert.title || "изменение темы").slice(0, 190), description: [alert.body, "Источник: Forum Watch"].filter(Boolean).join("\n"), priority: alert.severity === "critical" ? "critical" : "high" }, { caseNumber: alert.case_number });
}
function evidencePanel() {
  const item = state.detail.case;
  const fields = [
    ["Вид работы", "request_type", item.request_type], ["Discord ID клиента", "client_id", item.client_id],
    ["Discord ID ведущего адвоката", "lead_lawyer_id", item.lead_lawyer_id], ["Discord ID секретаря", "secretary_id", item.secretary_id],
    ["Ник клиента", "client_nick", item.client_nick], ["Static ID", "static_id", item.static_id],
    ["Телефон", "phone", item.phone], ["Банковский счёт", "bank_account", item.bank_account],
    ["Ссылка на документ клиента", "passport_url", item.passport_url], ["Ссылка на иск", "claim_link", item.claim_link],
  ].map((row) => evidenceInput(row[0], row[1], row[2], false)).join("");
  const options = ["created", "awaiting_situation", "awaiting_link", "awaiting_close", "error"].map((name) => '<option value="' + name + '"' + (item.status === name ? " selected" : "") + ">" + esc(status(name)) + "</option>").join("");
  const sources = evidenceSources();
  const filtered = sources.filter((source) => state.evidenceFilter === "all" || (state.evidenceFilter === "files" ? source.kind === "file" : state.evidenceFilter === "images" ? source.kind === "image" : source.kind === "link"));
  const counts = {
    all: sources.length,
    files: sources.filter((source) => source.kind === "file").length,
    images: sources.filter((source) => source.kind === "image").length,
    links: sources.filter((source) => source.kind === "link").length,
  };
  const filters = [["all", "Все"], ["files", "Файлы"], ["images", "Изображения"], ["links", "Ссылки"]].map((entry) => '<button class="' + (state.evidenceFilter === entry[0] ? "is-active" : "") + '" data-evidence-filter="' + entry[0] + '">' + esc(entry[1]) + '<b>' + esc(counts[entry[0]]) + "</b></button>").join("");
  const gateRows = [
    [Boolean(item.situation_text), "Фактическая основа", "Нужно описать обстоятельства"],
    [Boolean(item.passport_url || item.static_id), "Идентификация клиента", "Нужен документ или static ID"],
    [Boolean(sources.length), "Материалы в досье", "Нет файлов или внешних ссылок"],
    [Boolean(item.claim_link), "Ссылка на иск", "Можно добавить после подготовки"],
  ];
  const gates = gateRows.map((row) => '<div class="sgl-evidence-gate ' + (row[0] ? "is-ready" : "") + '"><i>' + svg(row[0] ? "check" : "more") + '</i><div><b>' + esc(row[1]) + '</b><small>' + esc(row[0] ? "проверяемый контур заполнен" : row[2]) + "</small></div></div>").join("");
  const vault = filtered.length ? filtered.map(evidenceCard).join("") : empty("Для этого фильтра материалов нет. Файлы из Discord появятся здесь автоматически.");
  return panel("Досье и материалы", "EVIDENCE VAULT", '<div class="sgl-evidence-overview"><section><span>Материалов</span><strong>' + esc(counts.all) + '</strong><small>из Discord и карточки дела</small></section><section><span>Факты</span><strong>' + esc(item.situation_text ? "есть" : "—") + '</strong><small>исходная основа дела</small></section><section><span>Контроль</span><strong>' + esc(gateRows.filter((row) => row[0]).length + " / 4") + '</strong><small>готовность досье</small></section></div><div class="sgl-evidence-layout"><section class="sgl-evidence-vault"><header><div><span>МАТЕРИАЛЫ</span><h3>Evidence Vault</h3></div><button class="sgl-inline-action" data-case-tab="conversation">Открыть диалог</button></header><div class="sgl-evidence-filters">' + filters + '</div><div class="sgl-evidence-list">' + vault + '</div></section><aside class="sgl-evidence-gates"><span>ПРОВЕРКА ГОТОВНОСТИ</span><h3>Контрольные точки</h3>' + gates + '</aside></div><details class="sgl-evidence-record"><summary><span><b>Карточка и доступы</b><small>Состав, контакты, обстоятельства и стадия</small></span>' + svg("arrow") + '</summary><form id="sgl-evidence-form" class="sgl-evidence-editor"><div class="sgl-form-grid-inline">' + fields + '</div>' + evidenceInput("Обстоятельства дела", "situation_text", item.situation_text, true) + '<label>Стадия<select name="status">' + options + '</select></label><div class="sgl-field-actions"><button class="sgl-button sgl-button-primary" type="submit">Сохранить и синхронизировать доступы</button></div></form></details>');
}
function receiptCard(item) {
  const actions = [];
  if (item.status === "issued") actions.push('<button class="sgl-inline-action" data-receipt-proofs="' + esc(item.id) + '">Добавить два подтверждения</button>');
  if (item.status === "proofs_submitted" && state.detail.permissions && state.detail.permissions.manage) actions.push('<button class="sgl-inline-action" data-confirm-receipt="' + esc(item.id) + '">Подтвердить оплату</button>');
  const services = safeUrl(item.proof_services_url);
  const duty = safeUrl(item.proof_duty_url);
  if (services) actions.push('<a class="sgl-inline-action" href="' + esc(services) + '" target="_blank" rel="noreferrer">Услуги</a>');
  if (duty) actions.push('<a class="sgl-inline-action" href="' + esc(duty) + '" target="_blank" rel="noreferrer">Пошлина</a>');
  return '<article class="sgl-receipt"><div class="sgl-receipt-head"><div><strong>Квитанция #' + esc(item.id) + " · " + esc(Number(item.total_amount || 0).toLocaleString("ru-RU")) + ' $</strong><p>' + esc((item.court_label || "Инстанция не указана") + " · адвокат " + Number(item.lawyer_amount || 0).toLocaleString("ru-RU") + " $ · пошлина " + Number(item.duty_amount || 0).toLocaleString("ru-RU") + " $") + "</p></div>" + pill(item.status) + "</div>" + (actions.length ? '<div class="sgl-inline-actions">' + actions.join("") + "</div>" : "") + "</article>";
}
function financePanel() {
  const receipts = state.detail.receipts || [];
  const ledger = receipts.length ? receipts.map(receiptCard).join("") : empty("Квитанций по делу пока нет.");
  const courtOptions = [
    ["d", "Окружная юрисдикция · 35 000"], ["s", "Верховная юрисдикция · 45 000"],
    ["k", "Апелляция / кассация · 45 000"], ["o", "Обращение · 25 000"], ["e", "Иное"],
  ].map((row) => '<option value="' + row[0] + '">' + row[1] + "</option>").join("");
  const form = '<form id="sgl-receipt-form" class="sgl-finance-form"><h3>Создать квитанцию</h3><div class="sgl-form-grid-inline"><label>Инстанция<select name="court">' + courtOptions + '</select></label><label>Сумма, $<input name="total_amount" inputmode="numeric" required placeholder="Например, 100000"></label></div><label>Пошлина (только «Иное»)<input name="duty_amount" inputmode="numeric" placeholder="Необязательно"></label><button class="sgl-button sgl-button-primary" type="submit">Создать и отправить счёт в Discord</button><button class="sgl-button sgl-button-secondary" type="button" data-open-contract="1">Проверить и создать договор</button></form>';
  return panel("Финансы и договор", "ПЛАТЕЖИ / ДОКУМЕНТЫ", '<div class="sgl-finance-layout"><div class="sgl-receipt-list">' + ledger + "</div>" + form + "</div>");
}
function selectedPublication() {
  const items = state.detail.forum_publications || [];
  const wanted = Number(state.detail.selectedPublicationId || 0);
  return items.find((item) => Number(item.id) === wanted) || items.find((item) => ["draft", "failed"].includes(item.status)) || null;
}
function inputField(label, name, value, type) {
  return '<label>' + esc(label) + (type === "textarea" ? '<textarea name="' + esc(name) + '" placeholder="' + (name === "body" ? "Обстоятельства и требования…" : "") + '">' + esc(value || "") + '</textarea>' : '<input name="' + esc(name) + '" value="' + esc(value || "") + '">') + "</label>";
}
function publicationCard(item) {
  const editable = ["draft", "failed"].includes(item.status);
  const actions = editable ? ['<button class="sgl-inline-action" data-select-publication="' + esc(item.id) + '">Редактировать</button>'] : [];
  const href = safeUrl(item.forum_url);
  if (href) actions.push('<a class="sgl-inline-action" href="' + esc(href) + '" target="_blank" rel="noreferrer">Открыть тему</a>');
  if (editable) actions.push('<button class="sgl-inline-action" data-publish-forum="' + esc(item.id) + '">Опубликовать</button>');
  if (item.status === "published") actions.push('<button class="sgl-inline-action" data-start-forum-revision="' + esc(item.id) + '">Новая редакция</button>');
  return '<article class="sgl-publication"><div class="sgl-publication-head"><div><strong>' + esc(item.title || "Черновик иска") + "</strong><p>" + esc(item.last_error || item.forum_url || item.target_url || "Ожидает данных") + "</p></div>" + pill(item.status) + '</div><div class="sgl-inline-actions">' + actions.join("") + "</div></article>";
}
function startForumRevision(id) {
  const source = (state.detail && state.detail.forum_publications || []).find((item) => Number(item.id) === Number(id));
  if (!source) return;
  state.detail.selectedPublicationId = 0;
  state.detail.revisionSource = source;
  renderCase();
}
function forumDiff() {
  const snapshots = (state.detail.forum_snapshots || []).slice().sort((left, right) => Number(right.id) - Number(left.id));
  if (!snapshots.length) return "";
  const current = snapshots[0];
  const before = snapshots[1];
  return '<section class="sgl-publication"><strong>Последнее изменение темы</strong><p>' + esc((current.snapshot_kind === "changed" ? "Forum Watch сохранил новую версию. " : "Сохранён снимок темы. ") + stamp(current.captured_at)) + '</p><div class="sgl-forum-diff"><article><small>До</small><p>' + esc(before && before.thread_excerpt || "Предыдущего снимка нет.") + "</p></article><article><small>После</small><p>" + esc(current.thread_excerpt || "Текст не получен.") + "</p></article></div></section>";
}
function forumPanel() {
  const selected = selectedPublication();
  const revision = !selected && state.detail.revisionSource || null;
  const editorValue = selected || revision;
  const header = selected ? "Редактор черновика" : revision ? "Новая редакция" : "Новый черновик";
  const publish = selected && ["draft", "failed"].includes(selected.status) ? '<button class="sgl-button sgl-button-secondary" type="button" data-publish-forum="' + esc(selected.id) + '">Опубликовать вручную</button>' : "";
  const hidden = selected ? '<input type="hidden" name="expected_updated_at" value="' + esc(selected.updated_at || "") + '">' : "";
  const editor = '<form id="sgl-forum-editor" class="sgl-forum-editor"><h3>' + header + "</h3>" + (revision ? '<div class="sgl-form-note">Основано на опубликованной теме. Это создаст новый черновик — исходная публикация останется неизменной.</div>' : "") + inputField("Раздел форума", "target_url", editorValue && editorValue.target_url, "") + inputField("Заголовок", "title", editorValue && editorValue.title, "") + inputField("BBCode / текст иска", "body", editorValue && editorValue.body, "textarea") + hidden + '<div class="sgl-inline-actions"><button class="sgl-button sgl-button-primary" type="submit">' + (selected ? "Сохранить черновик" : revision ? "Создать новую редакцию" : "Создать черновик") + "</button>" + publish + "</div></form>";
  const publications = state.detail.forum_publications || [];
  const list = publications.length ? publications.map(publicationCard).join("") : empty("Черновика ещё нет. Atlas может подготовить его на вкладке «Atlas».");
  return panel("Публикация и мониторинг", "FORUM WATCH", '<div class="sgl-forum-layout">' + editor + '<div class="sgl-publication-list">' + list + forumDiff() + "</div></div>");
}
function atlasCard(item) {
  const citations = Array.isArray(item.citations) && item.citations.length ? '<div class="sgl-citations">Источники: ' + item.citations.length + "</div>" : "";
  return '<article class="sgl-atlas-note"><div class="sgl-atlas-note-head"><b>' + esc(text(item.kind || "analysis").toUpperCase()) + "</b><small>" + esc((item.agent_id || "atlas") + " · " + stamp(item.created_at)) + "</small></div><p>" + esc(item.answer || "Atlas не вернул текст.") + "</p>" + citations + '<div class="sgl-inline-actions"><button class="sgl-inline-action" data-atlas-task="' + esc(item.id) + '">Создать задачу</button></div></article>';
}
function atlasContextCard(notes) {
  const sources = evidenceSources();
  const messages = state.detail && state.detail.messages || [];
  const facts = state.detail && state.detail.case && state.detail.case.situation_text ? "есть" : "нет";
  const cards = [
    [sources.length, "материалов", "Evidence Vault", "evidence"],
    [messages.length, "сообщений", "Диалог Discord", "conversation"],
    [facts, "факты", "Карточка дела", "evidence"],
  ].map((item) => '<button type="button" data-case-tab="' + item[3] + '"><strong>' + esc(item[0]) + '</strong><span>' + esc(item[1]) + '</span><small>' + esc(item[2]) + '</small></button>').join("");
  return '<section class="sgl-atlas-context"><header><div><span>КОНТЕКСТ ATLAS</span><b>Проверяемые источники</b></div><small>' + esc(notes.length ? "ответ хранится в журнале дела" : "начните с материалов дела") + '</small></header><div class="sgl-atlas-context-grid">' + cards + "</div></section>";
}
function atlasPanel() {
  const notes = state.detail.ai_notes || [];
  const list = notes.length ? notes.map(atlasCard).join("") : empty("Пока нет сохранённых выводов. Выберите один из режимов анализа справа.");
  const agents = [["atlas-claims", "Иски"], ["atlas-complaints", "Жалобы"], ["atlas-defense", "Защита"], ["atlas-documents", "Документы"]].map((row) => '<option value="' + row[0] + '">' + row[1] + "</option>").join("");
  const presets = [["summary", "Сводка"], ["risks", "Риски"], ["plan", "План"], ["claim", "Проект иска"]].map((row) => '<button type="button" data-atlas-preset="' + row[0] + '">' + row[1] + "</button>").join("");
  const composer = '<form id="sgl-atlas-form" class="sgl-atlas-composer"><h3>Новый запрос</h3><label>Профиль Atlas<select name="model">' + agents + '</select></label><label>Задача<textarea name="question" required maxlength="4000" placeholder="Что требуется разобрать в этом деле?"></textarea></label><div class="sgl-atlas-presets">' + presets + '</div><button class="sgl-button sgl-button-primary" type="submit">Спросить Atlas</button><div class="sgl-atlas-side"><b>Контролируемый контекст</b><p>Atlas получает карточку, последние сообщения и сохранённые выводы как непроверенные материалы. Ответ сохраняется в журнале дела.</p></div></form>';
  return panel("Atlas по материалам дела", "CASE INTELLIGENCE", '<div class="sgl-atlas-layout"><div class="sgl-atlas-notes">' + list + atlasContextCard(notes) + "</div>" + composer + "</div>");
}
function caseArchivePanel() {
  const item = state.detail.case;
  const archive = (state.bootstrap && state.bootstrap.archives ? state.bootstrap.archives.items : []).find((entry) => Number(entry.case_number) === Number(item.case_number));
  if (!archive) return panel("Состояние архива", "EVIDENCE VAULT", empty(item.status === "closed" ? "Кейс закрыт. Снимок Discord будет создан архивным процессом." : "Архив появляется после закрытия дела и создания снимка Discord."));
  const fields = [["Канал-источник", archive.original_channel_name || "—"], ["Статус снимка", archive.status || "—"], ["Сообщения", archive.message_count || 0], ["Вложения", archive.attachment_count || 0]].map((row) => '<article class="sgl-fact-box"><span>' + esc(row[0]) + "</span><b>" + esc(row[1]) + "</b></article>").join("");
  const action = archive.source_deleted_at ? '<button class="sgl-button sgl-button-secondary" data-restore-archive="' + esc(item.case_number) + '">Восстановить read-only копию в Discord</button>' : "";
  return panel("Состояние архива", "EVIDENCE VAULT", '<div class="sgl-overview-grid">' + fields + "</div>" + action);
}

function clientProfiles() {
  const select = $("sgl-create-client-profile");
  if (!select) return;
  const current = select.value;
  select.innerHTML = '<option value="">Новый или ручной ввод</option>' + (state.directory.clients || []).slice(0, 100).map((item) => '<option value="' + esc(item.id) + '">' + esc([item.client_nick || "Клиент", item.static_id && "static " + item.static_id, item.discord_user_id].filter(Boolean).join(" · ")) + "</option>").join("");
  select.value = current;
}
function fillProfile(id) {
  const profile = (state.directory.clients || []).find((item) => Number(item.id) === Number(id));
  if (!profile) return;
  const form = $("sgl-create-form");
  ["client_id", "client_nick", "static_id", "phone", "bank_account", "passport_url"].forEach((key) => {
    if (form.elements[key]) form.elements[key].value = profile[key === "client_id" ? "discord_user_id" : key] || "";
  });
}
function createStep() {
  document.querySelectorAll("[data-create-step]").forEach((node) => node.classList.toggle("is-active", Number(node.dataset.createStep) === state.createStep));
  document.querySelectorAll(".sgl-stepper li").forEach((node, index) => {
    node.classList.toggle("is-current", index + 1 === state.createStep);
    node.classList.toggle("is-done", index + 1 < state.createStep);
  });
  const labels = ["Клиент", "Команда", "Факты", "Проверка"];
  $("sgl-create-step-caption").textContent = "0" + state.createStep + " / 04 · " + labels[state.createStep - 1];
  $("sgl-create-back").hidden = state.createStep === 1;
  $("sgl-create-next").hidden = state.createStep === 4;
  $("sgl-create-submit").hidden = state.createStep !== 4;
  if (state.createStep === 4) createReview();
}
function validateStep() {
  const form = $("sgl-create-form");
  const required = state.createStep === 1 ? ["client_id"] : state.createStep === 2 ? ["lead_lawyer_id"] : [];
  for (const name of required) {
    if (!text(form.elements[name] && form.elements[name].value).trim()) {
      form.elements[name].focus();
      toast("Заполните обязательное поле, чтобы продолжить.", true);
      return false;
    }
  }
  return true;
}
function createReview() {
  const form = $("sgl-create-form");
  const rows = [
    ["Клиент", (form.elements.client_nick.value || "не указан") + " · Discord " + (form.elements.client_id.value || "—")],
    ["Команда", "адвокат " + (form.elements.lead_lawyer_id.value || "—") + (form.elements.secretary_id.value ? " · секретарь " + form.elements.secretary_id.value : "")],
    ["Вид работы", form.elements.request_type.value || "не указан"], ["Стартовая стадия", status(form.elements.status.value)],
    ["Обстоятельства", form.elements.situation_text.value ? "заполнены" : "добавятся позже"],
  ];
  $("sgl-create-review").innerHTML = rows.map((row) => '<div class="sgl-review-row"><span>' + esc(row[0]) + "</span><b>" + esc(row[1]) + "</b></div>").join("");
}
function openCreate() {
  state.createStep = 1;
  $("sgl-create-form").reset();
  clientProfiles();
  createStep();
  modal("sgl-create-dialog", true);
}
function taskDateInput(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return text(value).slice(0, 16);
  const pad = (number) => String(number).padStart(2, "0");
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + "T" + pad(date.getHours()) + ":" + pad(date.getMinutes());
}
function taskCaseOptions(selected) {
  const select = $("sgl-task-case-select");
  const cases = state.bootstrap && state.bootstrap.cases ? state.bootstrap.cases.items || [] : [];
  select.innerHTML = '<option value="">Выберите кейс</option>' + cases
    .filter((item) => !["closed", "archived"].includes(item.status) || Number(item.case_number) === Number(selected))
    .map((item) => '<option value="' + esc(item.case_number) + '">' + esc(num(item.case_number) + " · " + client(item)) + "</option>").join("");
  select.value = selected ? String(selected) : "";
}
function taskOwnerOptions(selected) {
  const select = $("sgl-task-owner-select");
  const members = new Map();
  const add = (id, label) => {
    if (id && !members.has(String(id))) members.set(String(id), label || "Участник " + id);
  };
  (state.directory.lawyers || []).forEach((item) => add(item.discord_user_id, item.lawyer_nick || item.display_name));
  (state.tasks || []).forEach((item) => add(item.owner_id, item.owner_display));
  const current = state.detail && state.detail.case;
  if (current) {
    add(current.lead_lawyer_id, current.lead_lawyer_display);
    add(current.secretary_id, current.secretary_display);
  }
  add(selected, (state.tasks || []).find((item) => Number(item.owner_id) === Number(selected))?.owner_display);
  select.innerHTML = '<option value="">Не назначен</option>' + Array.from(members.entries()).map((entry) => '<option value="' + esc(entry[0]) + '">' + esc(entry[1] + " · " + entry[0]) + "</option>").join("");
  select.value = selected ? String(selected) : "";
}
function openTask(prefill, options) {
  const source = prefill || {};
  const settings = options || {};
  const explicitCase = Object.prototype.hasOwnProperty.call(settings, "caseNumber");
  const caseNumber = explicitCase ? Number(settings.caseNumber || 0) : Number(source.case_number || (state.detail && state.detail.case && state.detail.case.case_number) || 0);
  const editId = Number(settings.editId || 0);
  const form = $("sgl-task-form");
  form.reset();
  state.taskDialog = { caseNumber: caseNumber || null, editId: editId || null };
  taskCaseOptions(caseNumber);
  taskOwnerOptions(source.owner_id);
  $("sgl-task-case-select-wrap").hidden = Boolean(caseNumber);
  $("sgl-task-status-wrap").hidden = !editId;
  $("sgl-task-kicker").textContent = editId ? "РАБОЧАЯ ЗАДАЧА" : caseNumber ? "ЗАДАЧА КЕЙСА" : "НОВАЯ ЗАДАЧА";
  $("sgl-task-title").textContent = editId ? "Изменить задачу" : "Следующее действие";
  $("sgl-task-submit").textContent = editId ? "Сохранить изменения" : "Создать задачу";
  const selectedCase = (state.bootstrap && state.bootstrap.cases ? state.bootstrap.cases.items || [] : []).find((item) => Number(item.case_number) === caseNumber);
  $("sgl-task-case-label").textContent = selectedCase ? num(caseNumber) + " · " + client(selectedCase) : "Выберите кейс, к которому относится работа.";
  form.elements.title.value = source.title || "";
  form.elements.description.value = source.description || "";
  form.elements.priority.value = source.priority || "normal";
  form.elements.due_at.value = taskDateInput(source.due_at);
  if (editId) form.elements.status.value = source.status || "open";
  modal("sgl-task-dialog", true);
}
function openContract() {
  const defaults = state.detail && state.detail.contract && state.detail.contract.defaults;
  if (!defaults) {
    toast("Данные для договора ещё не загружены.", true);
    return;
  }
  const labels = {
    contractNumber: "Номер договора", contractDate: "Дата договора", lawyerName: "Адвокат",
    lawyerStatic: "Static адвоката", lawyerContact: "Контакт адвоката", clientName: "Клиент",
    clientPassport: "Static / паспорт клиента", clientContact: "Контакт клиента",
    servicePrice: "Стоимость", contractEndDate: "Срок действия",
  };
  $("sgl-contract-fields").innerHTML = Object.keys(labels).map((key) => '<label>' + labels[key] + '<input name="' + key + '" maxlength="240" value="' + esc(defaults[key] || "") + '"></label>').join("");
  modal("sgl-contract-dialog", true);
}
function openClose() {
  if (!state.detail || !state.detail.case) return;
  const item = state.detail.case;
  const checks = [
    ["Обстоятельства сохранены", Boolean(item.situation_text)], ["Ссылка на иск добавлена", Boolean(item.claim_link)],
    ["Нет открытых задач", !(state.detail.tasks || []).some((task) => task.status === "open")],
    ["Квитанции подтверждены", !(state.detail.receipts || []).some((receipt) => receipt.status !== "confirmed")],
  ];
  $("sgl-close-preflight").innerHTML = checks.map((row) => '<div class="sgl-preflight-row ' + (row[1] ? "is-ok" : "is-warning") + '"><i>' + svg(row[1] ? "check" : "more") + "</i><span>" + esc(row[1] ? row[0] : row[0] + " — требует решения") + "</span></div>").join("");
  $("sgl-close-form").reset();
  modal("sgl-close-dialog", true);
}

async function refreshCase(message) {
  if (state.detail && state.detail.case) {
    try {
      state.detail = await api("/api/sgl/cases/" + state.detail.case.case_number);
      if (state.detail.permissions && state.detail.permissions.manage) {
        try {
          const journal = await api("/api/sgl/cases/" + state.detail.case.case_number + "/decisions?resolved=1");
          state.detail.decisions = journal.decisions || [];
        } catch (_) {
          state.detail.decisions = [];
        }
      } else {
        state.detail.decisions = [];
      }
      renderCase();
    } catch (error) {
      toast(error.message || "Не удалось обновить дело.", true);
    }
  }
  await refreshCore(true);
  if (message) toast(message);
}
async function submitCreate(form) {
  if (!validateStep()) return;
  const payload = Object.fromEntries(new FormData(form).entries());
  if (!payload.secretary_id) delete payload.secretary_id;
  const submit = $("sgl-create-submit");
  submit.disabled = true;
  submit.textContent = "Создаю закрытый канал…";
  try {
    const result = await api("/api/sgl/cases", { method: "POST", body: JSON.stringify(payload), timeout: 90000 });
    modal("sgl-create-dialog", false);
    await refreshCore(true);
    toast("Кейс " + num(result.case.case_number) + " создан. Открываю рабочее пространство…");
    goCase(result.case.case_number, "overview");
  } catch (error) {
    toast(error.message || "Не удалось создать кейс.", true);
  } finally {
    submit.disabled = false;
    submit.textContent = "Создать кейс";
  }
}
async function saveEvidence(form) {
  if (!state.detail || !state.detail.case) return;
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.expected_updated_at = state.detail.case.updated_at;
  ["client_id", "lead_lawyer_id", "secretary_id"].forEach((key) => { if (payload[key] === "") payload[key] = null; });
  try {
    await api("/api/sgl/cases/" + state.detail.case.case_number, { method: "PATCH", body: JSON.stringify(payload) });
    await refreshCase("Карточка дела сохранена.");
  } catch (error) {
    toast(error.message || "Не удалось сохранить карточку.", true);
  }
}
async function sendMessage(form) {
  if (!state.detail || !state.detail.case) return;
  const body = new FormData();
  const content = text(form.elements.content.value).trim();
  const replyTo = text(form.elements.reply_to && form.elements.reply_to.value).trim();
  if (content) body.set("content", content);
  if (replyTo) body.set("reply_to", replyTo);
  Array.from(form.elements.files.files || []).slice(0, 10).forEach((file) => body.append("files", file));
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    await api("/api/sgl/cases/" + state.detail.case.case_number + "/messages", { method: "POST", body });
    form.reset();
    state.messageReply = null;
    await refreshCase();
  } catch (error) {
    toast(error.message || "Не удалось отправить сообщение.", true);
  } finally {
    submit.disabled = false;
  }
}
async function createReceipt(form) {
  if (!state.detail || !state.detail.case) return;
  try {
    await api("/api/sgl/cases/" + state.detail.case.case_number + "/receipts", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
    form.reset();
    await refreshCase("Квитанция создана и отправлена в Discord.");
  } catch (error) {
    toast(error.message || "Не удалось создать квитанцию.", true);
  }
}
async function proofs(receiptId) {
  if (!state.detail || !state.detail.case) return;
  const services = prompt("Ссылка на подтверждение оплаты услуг:");
  if (!services) return;
  const duty = prompt("Ссылка на подтверждение пошлины:");
  if (!duty) return;
  try {
    await api("/api/sgl/cases/" + state.detail.case.case_number + "/receipts/" + receiptId + "/proofs", { method: "POST", body: JSON.stringify({ services_url: services, duty_url: duty }) });
    await refreshCase("Подтверждения оплаты сохранены, адвокату отправлен запрос.");
  } catch (error) {
    toast(error.message || "Не удалось сохранить подтверждения.", true);
  }
}
async function confirmReceipt(id) {
  if (!state.detail || !state.detail.case) return;
  try {
    await api("/api/sgl/cases/" + state.detail.case.case_number + "/receipts/" + id + "/confirm", { method: "POST", body: JSON.stringify({}) });
    await refreshCase("Оплата подтверждена.");
  } catch (error) {
    toast(error.message || "Не удалось подтвердить оплату.", true);
  }
}
async function saveForum(form) {
  if (!state.detail || !state.detail.case) return;
  const payload = Object.fromEntries(new FormData(form).entries());
  const selected = selectedPublication();
  try {
    if (selected) {
      const result = await api("/api/sgl/cases/" + state.detail.case.case_number + "/forum-publications/" + selected.id, { method: "PATCH", body: JSON.stringify(payload) });
      const index = state.detail.forum_publications.findIndex((item) => Number(item.id) === Number(result.publication.id));
      if (index >= 0) state.detail.forum_publications[index] = result.publication;
    } else {
      const result = await api("/api/sgl/cases/" + state.detail.case.case_number + "/forum-publications", { method: "POST", body: JSON.stringify(payload) });
      state.detail.forum_publications.unshift(result.publication);
      state.detail.selectedPublicationId = result.publication.id;
      state.detail.revisionSource = null;
    }
    renderCase();
    await refreshCore(true);
    toast(selected ? "Черновик сохранён." : "Черновик создан.");
  } catch (error) {
    toast(error.message || "Не удалось сохранить черновик.", true);
  }
}
async function publishForum(id) {
  if (!state.detail || !state.detail.case) return;
  const publication = (state.detail.forum_publications || []).find((item) => Number(item.id) === Number(id));
  if (!publication || !confirm("Опубликовать этот сохранённый черновик на форуме? Публикация выполняется через авторизованную сессию и может потребовать ручного действия.")) return;
  try {
    await api("/api/sgl/cases/" + state.detail.case.case_number + "/forum-publications/" + id + "/publish", { method: "POST", body: JSON.stringify({ expected_updated_at: publication.updated_at }), timeout: 90000 });
    await refreshCase("Иск опубликован. Forum Watch добавит тему в мониторинг.");
  } catch (error) {
    toast(error.message || "Публикация требует проверки.", true);
  }
}
function atlasKind(question) {
  const value = normalize(question);
  if (value.includes("риск")) return "risks";
  if (value.includes("план")) return "plan";
  if (value.includes("иск") || value.includes("черновик")) return "draft";
  return "analysis";
}
async function askAtlas(form) {
  if (!state.detail || !state.detail.case) return;
  const values = Object.fromEntries(new FormData(form).entries());
  if (!text(values.question).trim()) return;
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  submit.textContent = "Atlas анализирует…";
  try {
    const result = await api("/api/sgl/cases/" + state.detail.case.case_number + "/atlas", { method: "POST", body: JSON.stringify({ question: values.question, model: values.model, kind: atlasKind(values.question) }), timeout: 120000 });
    state.detail.ai_notes.unshift(result.note);
    form.reset();
    renderCase();
    await refreshCore(true);
  } catch (error) {
    toast(error.message || "Atlas временно недоступен.", true);
  } finally {
    submit.disabled = false;
  }
}
async function createTask(form) {
  const values = Object.fromEntries(new FormData(form).entries());
  const taskCaseNumber = Number(state.taskDialog.caseNumber || values.case_number || 0);
  delete values.case_number;
  if (!taskCaseNumber) {
    toast("Выберите кейс для новой задачи.", true);
    form.elements.case_number && form.elements.case_number.focus();
    return;
  }
  if (values.due_at) values.due_at = new Date(values.due_at).toISOString();
  try {
    const editing = Number(state.taskDialog.editId || 0);
    const result = editing
      ? await api("/api/sgl/tasks/" + editing, { method: "PATCH", body: JSON.stringify(values) })
      : await api("/api/sgl/cases/" + taskCaseNumber + "/tasks", { method: "POST", body: JSON.stringify(values) });
    const task = result.task;
    const index = (state.tasks || []).findIndex((item) => Number(item.id) === Number(task.id));
    if (index >= 0) state.tasks[index] = task;
    else state.tasks.unshift(task);
    if (state.detail && state.detail.case && Number(state.detail.case.case_number) === taskCaseNumber) {
      const detailIndex = (state.detail.tasks || []).findIndex((item) => Number(item.id) === Number(task.id));
      if (detailIndex >= 0) state.detail.tasks[detailIndex] = task;
      else state.detail.tasks.unshift(task);
      renderCase();
    }
    modal("sgl-task-dialog", false);
    await refreshCore(true);
    toast(editing ? "Задача обновлена." : "Задача добавлена в рабочую очередь.");
  } catch (error) {
    toast(error.message || "Не удалось сохранить задачу.", true);
  }
}
async function applyPlaybook(name) {
  const playbook = PLAYBOOKS[name];
  if (!playbook || !state.detail || !state.detail.case) return;
  const caseNumber = Number(state.detail.case.case_number);
  if (!confirm("Добавить плейбук «" + playbook.label + "»? В кейсе появится " + playbook.tasks.length + " задач")) return;
  try {
    for (const task of playbook.tasks) {
      await api("/api/sgl/cases/" + caseNumber + "/tasks", {
        method: "POST",
        body: JSON.stringify({ title: task[0], description: task[1], priority: task[2], source: "playbook:" + name }),
      });
    }
    await refreshCase();
    toast("Плейбук «" + playbook.label + "» добавлен в рабочую очередь.");
  } catch (error) {
    toast(error.message || "Не удалось добавить плейбук полностью.", true);
  }
}
async function createDecision(form) {
  if (!state.detail || !state.detail.case) return;
  const values = Object.fromEntries(new FormData(form).entries());
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  submit.textContent = "Сохраняю…";
  try {
    const result = await api("/api/sgl/cases/" + state.detail.case.case_number + "/decisions", { method: "POST", body: JSON.stringify(values) });
    state.detail.decisions = state.detail.decisions || [];
    state.detail.decisions.unshift(result.decision);
    form.reset();
    renderCase();
    toast("Решение сохранено в приватном журнале.");
  } catch (error) {
    toast(error.message || "Не удалось сохранить внутреннюю запись.", true);
  } finally {
    submit.disabled = false;
    submit.textContent = "Сохранить в журнал";
  }
}
async function updateDecisionStatus(id, value) {
  if (!state.detail || !state.detail.case) return;
  try {
    const result = await api("/api/sgl/cases/" + state.detail.case.case_number + "/decisions/" + id, { method: "PATCH", body: JSON.stringify({ status: value }) });
    const index = (state.detail.decisions || []).findIndex((item) => Number(item.id) === Number(id));
    if (index >= 0) state.detail.decisions[index] = result.decision;
    renderCase();
    toast(value === "acknowledged" ? "Запись подтверждена." : "Запись отмечена как прочитанная.");
  } catch (error) {
    toast(error.message || "Не удалось обновить внутреннюю запись.", true);
  }
}
async function changeTaskStatus(id, value) {
  try {
    const result = await api("/api/sgl/tasks/" + id, { method: "PATCH", body: JSON.stringify({ status: value }) });
    const index = (state.tasks || []).findIndex((item) => Number(item.id) === Number(id));
    if (index >= 0) state.tasks[index] = result.task;
    if (state.detail && state.detail.tasks) {
      const detailIndex = state.detail.tasks.findIndex((item) => Number(item.id) === Number(id));
      if (detailIndex >= 0) state.detail.tasks[detailIndex] = result.task;
      if (detailIndex >= 0) renderCase();
    }
    await refreshCore(true);
    toast(value === "done" ? "Задача отмечена выполненной." : value === "open" ? "Задача возвращена в работу." : "Статус задачи обновлён.");
  } catch (error) {
    toast(error.message || "Не удалось обновить задачу.", true);
  }
}
async function completeTask(id) { await changeTaskStatus(id, "done"); }
async function createContract(form) {
  if (!state.detail || !state.detail.case) return;
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  submit.textContent = "Формирую договор…";
  try {
    const result = await api("/api/sgl/cases/" + state.detail.case.case_number + "/contract", { method: "POST", body: JSON.stringify({ overrides: Object.fromEntries(new FormData(form).entries()) }), timeout: 180000 });
    modal("sgl-contract-dialog", false);
    await refreshCase();
    toast("Договор " + result.values.contractNumber + " отправлен в Discord (" + result.pages + " стр.).");
  } catch (error) {
    toast(error.message || "Не удалось сформировать договор.", true);
  } finally {
    submit.disabled = false;
  }
}
async function closeCase(form) {
  if (!state.detail || !state.detail.case) return;
  const values = Object.fromEntries(new FormData(form).entries());
  values.publish_portfolio = Boolean(form.elements.publish_portfolio.checked);
  try {
    await api("/api/sgl/cases/" + state.detail.case.case_number + "/close", { method: "POST", body: JSON.stringify(values) });
    modal("sgl-close-dialog", false);
    await refreshCase("Кейс закрыт и передан в архивную очередь.");
  } catch (error) {
    toast(error.message || "Не удалось закрыть кейс.", true);
  }
}
async function acknowledge(id) {
  try {
    const result = await api("/api/sgl/notifications/" + id + "/acknowledge", { method: "PATCH", body: JSON.stringify({}) });
    state.operations.notifications = (state.operations.notifications || []).filter((item) => Number(item.id) !== Number(id));
    const alertIndex = (state.forum.alerts || []).findIndex((item) => Number(item.id) === Number(id));
    if (alertIndex >= 0) state.forum.alerts[alertIndex] = result.notification || { ...state.forum.alerts[alertIndex], acknowledged_at: new Date().toISOString() };
    if (state.screen === "forum") renderForum();
    else renderToday();
  } catch (error) {
    toast(error.message || "Не удалось отметить уведомление.", true);
  }
}
async function acknowledgeVisible() {
  const items = (state.operations.notifications || []).slice();
  for (const item of items) await acknowledge(item.id);
  if (items.length) toast("Видимые уведомления отмечены как разобранные.");
}
async function restore(number) {
  try {
    const result = await api("/api/sgl/archives/" + number + "/restore", { method: "POST", body: JSON.stringify({}) });
    toast(result.created ? "Создана временная read-only копия в Discord." : "Временная read-only копия уже доступна.");
    if (result.discord_url) open(result.discord_url, "_blank", "noopener");
  } catch (error) {
    toast(error.message || "Не удалось восстановить архив.", true);
  }
}
async function checkForum() {
  const node = $("sgl-forum-check");
  node.disabled = true;
  try {
    const result = await api("/api/sgl/forum/watch", { method: "POST", body: JSON.stringify({}), timeout: 120000 });
    toast("Forum Watch: проверено " + result.summary.checked + ", изменений " + result.summary.changed + ", уведомлений " + result.summary.alerts + ".");
    await refreshCore(true);
  } catch (error) {
    toast(error.message || "Проверка Forum Watch недоступна.", true);
  } finally {
    node.disabled = false;
  }
}

function commandMatches(query, values) {
  const words = normalize(query).split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const haystack = values.map(text).join(" ").toLocaleLowerCase("ru-RU");
  return words.every((word) => haystack.includes(word));
}
function globalCaseResults(query) {
  const cases = state.bootstrap && state.bootstrap.cases ? state.bootstrap.cases.items || [] : [];
  return cases.filter((item) => commandMatches(query, [
    item.case_number, client(item), item.client_nick, item.static_id, item.request_type,
    item.lead_lawyer_display, item.secretary_display, status(item.status), nextAction(item).title,
  ])).slice(0, 6).map((item) => ({
    key: "case:" + item.case_number,
    kind: "case",
    caseNumber: item.case_number,
    icon: "case",
    title: num(item.case_number) + " · " + client(item),
    detail: [item.request_type || "Дело", status(item.status), nextAction(item).title].filter(Boolean).join(" · "),
  }));
}
function globalTaskResults(query) {
  return (state.tasks || []).filter((item) => commandMatches(query, [
    item.title, item.description, item.case_number, item.case_title, item.case_request_type,
    item.owner_display, item.source, taskPriority(item.priority), status(item.status),
  ])).slice(0, 5).map((item) => ({
    key: "task:" + item.id,
    kind: "task",
    taskId: item.id,
    icon: "task",
    title: item.title || "Задача без названия",
    detail: [num(item.case_number), item.owner_display || "не назначена", taskDueCopy(item)].join(" · "),
  }));
}
function globalPeopleResults(query) {
  const people = [];
  const seen = new Set();
  const add = (item, kind) => {
    const source = item || {};
    const id = text(source.discord_user_id || source.user_id || source.id);
    const name = text(source.client_display || source.client_nick || source.display_name || source.lawyer_nick || source.name).trim();
    const key = id || normalize(name);
    if (!name || seen.has(key) || !commandMatches(query, [name, id, source.static_id, source.phone, source.email, kind])) return;
    seen.add(key);
    const meta = kind === "Клиент"
      ? [source.static_id && "static " + source.static_id, id && "Discord " + id].filter(Boolean)
      : [source.static_id && "static " + source.static_id, id && "Discord " + id, source.email].filter(Boolean);
    people.push({
      key: "person:" + key,
      kind: "person",
      icon: "people",
      title: name,
      detail: [kind, meta.join(" · ") || "Открыть реестр бюро"].join(" · "),
    });
  };
  (state.directory.clients || []).forEach((item) => add(item, "Клиент"));
  (state.directory.lawyers || []).forEach((item) => add(item, "Адвокат"));
  const viewer = state.bootstrap && state.bootstrap.viewer;
  if (viewer && viewer.name) add({ id: viewer.id, display_name: viewer.name }, "Сотрудник");
  return people.slice(0, 5);
}
function globalQuickActions(query) {
  const actions = [
    { action: "new", icon: "plus", title: "Новый кейс", detail: "Открыть мастер создания защищённого дела", terms: ["новый кейс", "создать кейс", "создать дело", "добавить дело"] },
    { action: "task", icon: "task", title: "Новая задача", detail: "Добавить следующее действие в рабочую очередь", terms: ["новая задача", "создать задачу", "добавить задачу", "поручение"] },
    { action: "today", icon: "grid", title: "Рабочая очередь", detail: "Сигналы, задачи и платежи на сегодня", terms: ["очередь", "сегодня", "сигналы", "платежи"] },
    { action: "work", icon: "task", title: "Задачи команды", detail: "Сроки, исполнители и статусы всех дел", terms: ["задачи", "работа", "сроки", "исполнители"] },
    { action: "cases", icon: "case", title: "Реестр дел", detail: "Открыть все кейсы и их статусы", terms: ["кейсы", "дела", "реестр"] },
    { action: "atlas", icon: "atlas", title: "Atlas Intelligence", detail: "Перейти к анализам и приоритетным кейсам", terms: ["atlas", "ai", "анализ", "риски"] },
    { action: "forum", icon: "forum", title: "Forum Watch", detail: "Публикации, изменения и сигналы", terms: ["форум", "forum", "публикации", "иск"] },
    { action: "people", icon: "people", title: "Реестр бюро", detail: "Клиенты, адвокаты и рабочая команда", terms: ["люди", "команда", "сотрудники", "адвокаты", "клиенты", "бюро"] },
    { action: "archive", icon: "archive", title: "Архив дел", detail: "Снимки закрытых Discord-кейсов", terms: ["архив", "evidence vault", "снимки"] },
  ];
  return actions.filter((item) => commandMatches(query, [item.title, item.detail].concat(item.terms))).map((item) => ({
    key: "action:" + item.action,
    kind: "action",
    icon: item.icon,
    title: item.title,
    detail: item.detail,
    action: item.action,
  }));
}
function globalSearchResults(query) {
  const actions = globalQuickActions(query);
  if (!query) return actions.slice(0, 6);
  return globalCaseResults(query).concat(globalTaskResults(query), globalPeopleResults(query), actions).slice(0, 15);
}
function globalSearchResultMarkup(item, index, active, total) {
  const kind = { case: "Кейс", task: "Задача", person: "Человек", action: "Команда" }[item.kind] || "Результат";
  const id = "sgl-search-option-" + index;
  return '<button type="button" id="' + id + '" class="sgl-search-result' + (active ? " is-active" : "") + '" role="option" data-search-result="' + index + '" data-search-kind="' + esc(item.kind) + '" data-active="' + String(active) + '" aria-selected="' + String(active) + '" aria-posinset="' + (index + 1) + '" aria-setsize="' + total + '" aria-label="' + esc(kind + ": " + item.title + ". " + item.detail) + '" tabindex="-1"><span>' + svg(item.icon) + "</span><div><strong>" + esc(item.title) + "</strong><small>" + esc(item.detail) + "</small></div>" + svg("arrow") + "</button>";
}
function setGlobalSearchActive(index, reveal) {
  const target = $("sgl-search-results");
  const input = $("sgl-global-search");
  const total = state.palette.results.length;
  if (!target || !input || !total) {
    state.palette.activeIndex = -1;
    input && input.removeAttribute("aria-activedescendant");
    return;
  }
  const active = Math.max(0, Math.min(total - 1, Number(index) || 0));
  state.palette.activeIndex = active;
  const options = target.querySelectorAll("[data-search-result]");
  options.forEach((option, optionIndex) => {
    const selected = optionIndex === active;
    option.classList.toggle("is-active", selected);
    option.dataset.active = String(selected);
    option.setAttribute("aria-selected", String(selected));
  });
  const option = target.querySelector('[data-search-result="' + active + '"]');
  if (!option) return;
  input.setAttribute("aria-activedescendant", option.id);
  if (reveal && option.scrollIntoView) option.scrollIntoView({ block: "nearest" });
}
function closeGlobalSearch(options) {
  const settings = options || {};
  const target = $("sgl-search-results");
  const input = $("sgl-global-search");
  state.palette.results = [];
  state.palette.activeIndex = -1;
  if (target) {
    target.hidden = true;
    target.innerHTML = "";
  }
  if (input) {
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    if (settings.clear) input.value = "";
    if (settings.blur) input.blur();
  }
}
function openGlobalSearch() {
  const input = $("sgl-global-search");
  if (!input) return;
  globalSearch(input.value);
}
function moveGlobalSearch(delta) {
  const input = $("sgl-global-search");
  const target = $("sgl-search-results");
  if (!input || !target) return;
  if (target.hidden || !state.palette.results.length) globalSearch(input.value);
  const total = state.palette.results.length;
  if (!total) return;
  const current = state.palette.activeIndex;
  const next = current < 0 ? (delta < 0 ? total - 1 : 0) : (current + delta + total) % total;
  setGlobalSearchActive(next, true);
}
function executeGlobalSearch(index) {
  const item = state.palette.results[Number(index)];
  if (!item) return;
  closeGlobalSearch({ clear: true });
  if (item.kind === "case") {
    goCase(item.caseNumber, "overview");
    return;
  }
  if (item.kind === "task") {
    const task = (state.tasks || []).find((entry) => String(entry.id) === String(item.taskId));
    if (task) openTask(task, { caseNumber: task.case_number, editId: task.id });
    else toast("Эта задача уже не доступна в рабочей очереди.", true);
    return;
  }
  if (item.kind === "person") {
    goScreen("people");
    return;
  }
  if (item.action === "new") openCreate();
  if (item.action === "task") openTask({}, { caseNumber: null });
  if (["today", "work", "cases", "atlas", "forum", "people", "archive"].includes(item.action)) goScreen(item.action);
}
function globalSearch(value) {
  const target = $("sgl-search-results");
  const input = $("sgl-global-search");
  if (!target || !input) return;
  const query = normalize(value);
  const previous = state.palette.results[state.palette.activeIndex];
  const results = globalSearchResults(query);
  state.palette.query = query;
  state.palette.results = results;
  state.palette.activeIndex = results.length ? Math.max(0, results.findIndex((item) => previous && item.key === previous.key)) : -1;
  target.setAttribute("role", "listbox");
  target.setAttribute("aria-label", "Результаты глобального поиска SGL");
  target.setAttribute("aria-live", "polite");
  target.hidden = false;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", "sgl-search-results");
  input.setAttribute("aria-expanded", "true");
  input.setAttribute("aria-keyshortcuts", "Control+K Meta+K");
  if (!results.length) {
    target.innerHTML = '<div class="sgl-empty" role="status">Ничего не найдено. Попробуйте номер дела, имя, static ID или действие.</div>';
    input.removeAttribute("aria-activedescendant");
    return;
  }
  target.innerHTML = results.map((item, index) => globalSearchResultMarkup(item, index, index === state.palette.activeIndex, results.length)).join("");
  setGlobalSearchActive(state.palette.activeIndex, false);
}

function bindEvents() {
  document.querySelectorAll("dialog.sgl-modal").forEach((dialog) => {
    if (dialog.dataset.sglFocusBound) return;
    dialog.dataset.sglFocusBound = "true";
    dialog.addEventListener("close", () => restoreModalFocus(dialog.id));
  });
  const globalInput = $("sgl-global-search");
  const globalResults = $("sgl-search-results");
  if (globalInput) {
    globalInput.setAttribute("role", "combobox");
    globalInput.setAttribute("aria-autocomplete", "list");
    globalInput.setAttribute("aria-controls", "sgl-search-results");
    globalInput.setAttribute("aria-expanded", "false");
    globalInput.setAttribute("aria-keyshortcuts", "Control+K Meta+K");
  }
  if (globalResults) {
    globalResults.setAttribute("role", "listbox");
    globalResults.setAttribute("aria-label", "Результаты глобального поиска SGL");
  }
  app.addEventListener("click", (event) => {
    if (event.target.closest("#sgl-sidebar-toggle")) {
      setSidebarCompact(!app.classList.contains("is-sidebar-compact"));
      return;
    }
    const close = event.target.closest("[data-close-modal]");
    if (close) {
      modal(close.closest("dialog").id, false);
      return;
    }
    const searchResult = event.target.closest("[data-search-result]");
    if (searchResult) {
      event.preventDefault();
      executeGlobalSearch(searchResult.dataset.searchResult);
      return;
    }
    const memberPicker = event.target.closest("[data-open-member-picker]");
    if (memberPicker) {
      openMemberPicker(memberPicker);
      return;
    }
    const memberPickerFilter = event.target.closest("[data-member-picker-filter]");
    if (memberPickerFilter) {
      state.memberPicker.filter = memberPickerFilter.dataset.memberPickerFilter || "all";
      renderMemberPicker();
      return;
    }
    const memberPickerResult = event.target.closest("[data-select-member-picker]");
    if (memberPickerResult) {
      selectMemberPicker(memberPickerResult.dataset.selectMemberPicker);
      return;
    }
    if (event.target.closest("[data-member-picker-clear]")) {
      clearMemberPicker();
      return;
    }
    const nav = event.target.closest("[data-go]");
    if (nav) {
      event.preventDefault();
      goScreen(nav.dataset.go);
      return;
    }
    const openCaseButton = event.target.closest("[data-open-case]");
    if (openCaseButton && openCaseButton.dataset.openCase) {
      event.preventDefault();
      goCase(openCaseButton.dataset.openCase, openCaseButton.dataset.openTab || "overview");
      return;
    }
    const tab = event.target.closest("[data-case-tab]");
    if (tab) {
      caseTab(tab.dataset.caseTab);
      return;
    }
    const reply = event.target.closest("[data-reply-message]");
    if (reply) {
      replyMessage(reply.dataset.replyMessage);
      return;
    }
    if (event.target.closest("[data-cancel-message-reply]")) {
      cancelMessageReply();
      return;
    }
    const messageJump = event.target.closest("[data-jump-message]");
    if (messageJump) {
      jumpToMessage(messageJump.dataset.jumpMessage);
      return;
    }
    const evidenceFilter = event.target.closest("[data-evidence-filter]");
    if (evidenceFilter) {
      state.evidenceFilter = evidenceFilter.dataset.evidenceFilter || "all";
      renderCasePanel();
      return;
    }
    const evidenceTask = event.target.closest("[data-evidence-task]");
    if (evidenceTask) {
      openEvidenceTask(evidenceTask.dataset.evidenceTask);
      return;
    }
    const decisionStatus = event.target.closest("[data-decision-status]");
    if (decisionStatus) {
      void updateDecisionStatus(decisionStatus.dataset.decisionId, decisionStatus.dataset.decisionStatus);
      return;
    }
    const view = event.target.closest("[data-case-view]");
    if (view) {
      state.caseView = view.dataset.caseView;
      document.querySelectorAll("[data-case-view]").forEach((node) => node.classList.toggle("is-active", node.dataset.caseView === state.caseView));
      renderCases();
      return;
    }
    const workView = event.target.closest("[data-work-view]");
    if (workView) {
      state.workView = workView.dataset.workView;
      renderWork();
      return;
    }
    if (event.target.closest("#sgl-refresh")) { void refreshCore(false); return; }
    if (event.target.closest("#sgl-create-case") || event.target.closest("[data-create-case]")) { openCreate(); return; }
    if (event.target.closest("#sgl-inbox-button")) {
      goScreen("today");
      setTimeout(() => $("sgl-inbox-list").scrollIntoView({ behavior: "smooth", block: "center" }), 10);
      return;
    }
    if (event.target.closest("#sgl-ack-all-visible")) { void acknowledgeVisible(); return; }
    const ack = event.target.closest("[data-acknowledge]");
    if (ack) { void acknowledge(ack.dataset.acknowledge); return; }
    const forumAlertTask = event.target.closest("[data-forum-alert-task]");
    if (forumAlertTask) { openForumAlertTask(forumAlertTask.dataset.forumAlertTask); return; }
    if (event.target.closest("#sgl-forum-check")) { void checkForum(); return; }
    if (event.target.closest("#sgl-case-back")) { goScreen("cases"); return; }
    if (event.target.closest("#sgl-case-close")) { openClose(); return; }
    if (event.target.closest("[data-new-task]")) { openTask({}, { caseNumber: null }); return; }
    if (event.target.closest("[data-add-task]")) { openTask(); return; }
    const playbook = event.target.closest("[data-apply-playbook]");
    if (playbook) { void applyPlaybook(playbook.dataset.applyPlaybook); return; }
    const editTask = event.target.closest("[data-edit-task]");
    if (editTask) {
      const task = (state.tasks || []).find((item) => Number(item.id) === Number(editTask.dataset.editTask));
      if (task) openTask(task, { caseNumber: task.case_number, editId: task.id });
      return;
    }
    const requestedTaskStatus = event.target.closest("[data-task-status]");
    if (requestedTaskStatus) { void changeTaskStatus(requestedTaskStatus.dataset.taskId, requestedTaskStatus.dataset.taskStatus); return; }
    const completed = event.target.closest("[data-complete-task]");
    if (completed) { void completeTask(completed.dataset.completeTask); return; }
    const receiptProofs = event.target.closest("[data-receipt-proofs]");
    if (receiptProofs) { void proofs(receiptProofs.dataset.receiptProofs); return; }
    const receiptConfirm = event.target.closest("[data-confirm-receipt]");
    if (receiptConfirm) { void confirmReceipt(receiptConfirm.dataset.confirmReceipt); return; }
    if (event.target.closest("[data-open-contract]")) { openContract(); return; }
    const selection = event.target.closest("[data-select-publication]");
    if (selection) {
      state.detail.selectedPublicationId = Number(selection.dataset.selectPublication);
      state.detail.revisionSource = null;
      renderCase();
      return;
    }
    const revision = event.target.closest("[data-start-forum-revision]");
    if (revision) { startForumRevision(revision.dataset.startForumRevision); return; }
    const publish = event.target.closest("[data-publish-forum]");
    if (publish) { void publishForum(publish.dataset.publishForum); return; }
    const preset = event.target.closest("[data-atlas-preset]");
    if (preset) {
      const form = $("sgl-atlas-form");
      const copy = {
        summary: "Собери краткую сводку: подтверждённые факты, пробелы, текущая стадия и следующий шаг.",
        risks: "Проанализируй риски, противоречия и недостающие доказательства. Раздели факты и допущения.",
        plan: "Составь поэтапный план действий с зависимостями и приоритетами. Не выдумывай отсутствующие факты.",
        claim: "Подготовь структурированный проект иска на основе материалов. Отметь места, которые требуют ручной проверки.",
      };
      form.elements.question.value = copy[preset.dataset.atlasPreset] || "";
      form.elements.question.focus();
      return;
    }
    const atlasTask = event.target.closest("[data-atlas-task]");
    if (atlasTask) {
      const note = (state.detail && state.detail.ai_notes || []).find((item) => Number(item.id) === Number(atlasTask.dataset.atlasTask));
      openTask({ title: "Проверить вывод Atlas", description: note ? text(note.answer).slice(0, 1000) : "", priority: "normal" });
      return;
    }
    const restoreArchive = event.target.closest("[data-restore-archive]");
    if (restoreArchive) { void restore(restoreArchive.dataset.restoreArchive); return; }
    const quick = event.target.closest("[data-quick-action]");
    if (quick) {
      if (quick.dataset.quickAction === "new") openCreate();
      if (quick.dataset.quickAction === "today") goScreen("today");
      if (quick.dataset.quickAction === "work") goScreen("work");
      closeGlobalSearch();
    }
  });
  app.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const handlers = {
      "sgl-create-form": submitCreate, "sgl-task-form": createTask, "sgl-contract-form": createContract,
      "sgl-close-form": closeCase, "sgl-evidence-form": saveEvidence, "sgl-message-form": sendMessage,
      "sgl-receipt-form": createReceipt, "sgl-forum-editor": saveForum, "sgl-atlas-form": askAtlas,
      "sgl-decision-form": createDecision,
    };
    if (handlers[form.id]) {
      event.preventDefault();
      void handlers[form.id](form);
    }
  });
  app.addEventListener("input", (event) => {
    const target = event.target;
    if (target.id === "sgl-global-search") globalSearch(target.value);
    if (target.id === "sgl-member-picker-query") scheduleMemberPickerSearch(target.value);
    if (target.id === "sgl-case-search") {
      state.caseQuery = target.value;
      renderCases();
    }
    if (target.id === "sgl-work-search") {
      state.workQuery = target.value;
      renderWork();
    }
    if (target.id === "sgl-directory-search") {
      clearTimeout(state.directoryTimer);
      state.directoryTimer = setTimeout(() => { void loadDirectory(target.value); }, 240);
    }
  });
  app.addEventListener("focusin", (event) => {
    if (event.target && event.target.id === "sgl-global-search") openGlobalSearch();
  });
  app.addEventListener("change", (event) => {
    if (event.target.id === "sgl-create-client-profile") fillProfile(event.target.value);
    if (event.target.classList && event.target.classList.contains("sgl-message-upload")) renderMessageFiles(event.target);
    if (event.target.id === "sgl-task-case-select") {
      const selected = Number(event.target.value || 0);
      state.taskDialog.caseNumber = selected || null;
      const item = (state.bootstrap && state.bootstrap.cases ? state.bootstrap.cases.items || [] : []).find((entry) => Number(entry.case_number) === selected);
      $("sgl-task-case-label").textContent = item ? num(selected) + " · " + client(item) : "Выберите кейс, к которому относится работа.";
    }
  });
  $("sgl-create-next").addEventListener("click", () => {
    if (!validateStep()) return;
    state.createStep = Math.min(4, state.createStep + 1);
    createStep();
  });
  $("sgl-create-back").addEventListener("click", () => {
    state.createStep = Math.max(1, state.createStep - 1);
    createStep();
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".sgl-search-wrap")) closeGlobalSearch();
  });
  document.addEventListener("keydown", (event) => {
    if (moveCaseTabByKeyboard(event) || openCaseRowByKeyboard(event)) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("sgl-global-search").focus();
      openGlobalSearch();
      return;
    }
    if (event.target && event.target.id === "sgl-global-search") {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        moveGlobalSearch(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Enter" && !$("sgl-search-results").hidden && state.palette.activeIndex >= 0) {
        event.preventDefault();
        executeGlobalSearch(state.palette.activeIndex);
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        closeGlobalSearch({ blur: true });
        return;
      }
    }
    if (event.key === "Enter" && event.target && event.target.id === "sgl-member-picker-query") {
      const first = $("sgl-member-picker-results") && $("sgl-member-picker-results").querySelector("[data-select-member-picker]");
      if (first) {
        event.preventDefault();
        selectMemberPicker(first.dataset.selectMemberPicker);
      }
    }
    if (event.key === "Escape") closeGlobalSearch();
  });
}

async function initialize() {
  try {
    // The public landing is an explicit, shareable route. It must never be
    // replaced by the authenticated workspace merely because a staff session
    // happens to be present in this browser.
    if (new URLSearchParams(location.search).get("public") === "1") {
      showPublic({ public: {} });
      return;
    }
    const bootstrap = await api("/api/sgl/bootstrap");
    if (bootstrap.mode === "public") {
      showPublic(bootstrap);
      return;
    }
    state.bootstrap = bootstrap;
    document.body.classList.remove("public-mode");
    document.body.classList.add("sgl-admin-mode");
    removePublicAssets();
    $("public-site").hidden = true;
    app.hidden = false;
    setSidebarCompact(storedSidebarState());
    $("loading").hidden = true;
    bindEvents();
    await Promise.all([refreshCore(true), loadDirectory("")]);
    await route();
    state.refreshTimer = setInterval(() => {
      if (!document.hidden) void refreshCore(true);
    }, 60000);
  } catch (error) {
    $("loading").hidden = true;
    toast(error.message || "SGL не удалось открыть.", true);
  }
}

window.addEventListener("popstate", () => { void route(); });
window.addEventListener("hashchange", () => { void route(); });
window.addEventListener("beforeunload", stopPolling);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() - state.lastRefreshAt > 45000) void refreshCore(true);
});
let caseTabResizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(caseTabResizeTimer);
  caseTabResizeTimer = setTimeout(() => focusActiveCaseTab(), 120);
}, { passive: true });
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
else void initialize();
