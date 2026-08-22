"use strict";

const byId = (id) => document.getElementById(id);
function storedAtlasSpace() {
  try { return Number(localStorage.getItem("tmod-atlas-space")) || null; }
  catch (_error) { return null; }
}
function storedChatFocus() {
  try { return localStorage.getItem("tmod-atlas-chat-focus") === "1"; }
  catch (_error) { return false; }
}
const appState = {
  data: null,
  screen: "home",
  selectedTemplate: null,
  busy: false,
  scopeReady: false,
  serverCode: "phoenix-15",
  factionCode: "lspd",
  threadId: null,
  threadReady: false,
  responseMode: "balanced",
  modelId: "atlas-tvr-a",
  onboardingPrompted: false,
  organizationId: storedAtlasSpace(),
  chatFocus: storedChatFocus(),
  chatHistoryOpen: false,
  jobWatchers: new Set(),
  caseDetail: null,
  caseItemMode: "claim",
  documentDetail: null,
};
const screenMeta = {
  home: ["ATLAS", "Командный центр"],
  ai: ["УМНЫЙ ПОМОЩНИК", "Atlas AI"],
  memory: ["ATLAS CONTINUITY", "Память и события"],
  media: ["ATLAS MEDIA CORE", "Медиасеть"],
  cases: ["ATLAS CASE & EVIDENCE", "Дела и доказательства"],
  documents: ["РАБОТА С ДОКУМЕНТАМИ", "Документы"],
  knowledge: ["БИБЛИОТЕКА ATLAS", "База знаний"],
  forum: ["РАБОТА С ФОРУМОМ", "Форум и памятки"],
};
let toastTimer = null;

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.firstChild.remove();
}

function atlasAgent(agentId = appState.modelId) {
  return (appState.data?.ai?.models || []).find((item) => item.id === agentId)
    || { id: "atlas-tvr-a", name: "Генеральный Atlas", short_name: "Генеральный", glyph: "A", description: "Универсальный интеллектуальный помощник Atlas" };
}

function appendInlineMarkdown(parent, value) {
  const text = String(value || "");
  const token = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\)|\*[^*\n]+\*)/g;
  let cursor = 0;
  for (const match of text.matchAll(token)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const raw = match[0];
    if (raw.startsWith("**")) parent.append(element("strong", "", raw.slice(2, -2)));
    else if (raw.startsWith("`")) parent.append(element("code", "", raw.slice(1, -1)));
    else if (raw.startsWith("[")) {
      const parts = raw.match(/^\[([^\]]+)]\((https?:\/\/[^\s)]+)\)$/);
      if (parts) {
        const link = element("a", "", parts[1]);
        link.href = parts[2];
        link.target = "_blank";
        link.rel = "noreferrer noopener";
        parent.append(link);
      } else parent.append(document.createTextNode(raw));
    } else parent.append(element("em", "", raw.slice(1, -1)));
    cursor = match.index + raw.length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function tableCells(line) {
  return String(line || "").trim().replace(/^\||\|$/g, "").split("|").map((item) => item.trim());
}

function renderRichText(target, value) {
  clear(target);
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  let list = null;
  let inCode = false;
  let codeLines = [];
  const resetList = () => { list = null; };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^```/.test(line.trim())) {
      resetList();
      if (inCode) {
        const pre = element("pre");
        pre.append(element("code", "", codeLines.join("\n")));
        target.append(pre);
        codeLines = [];
      }
      inCode = !inCode;
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }
    if (!line.trim()) { resetList(); continue; }
    if (line.includes("|") && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])) {
      resetList();
      const table = element("table");
      const head = element("thead");
      const headRow = element("tr");
      tableCells(line).forEach((cell) => { const th = element("th"); appendInlineMarkdown(th, cell); headRow.append(th); });
      head.append(headRow); table.append(head);
      const body = element("tbody");
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        const row = element("tr");
        tableCells(lines[index]).forEach((cell) => { const td = element("td"); appendInlineMarkdown(td, cell); row.append(td); });
        body.append(row); index += 1;
      }
      index -= 1; table.append(body); target.append(table); continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      resetList();
      const node = element(`h${heading[1].length + 2}`);
      appendInlineMarkdown(node, heading[2]); target.append(node); continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (bullet || ordered) {
      const kind = ordered ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== kind) { list = element(kind); target.append(list); }
      const item = element("li"); appendInlineMarkdown(item, (bullet || ordered)[1]); list.append(item); continue;
    }
    resetList();
    if (/^>\s?/.test(line)) {
      const quote = element("blockquote"); appendInlineMarkdown(quote, line.replace(/^>\s?/, "")); target.append(quote); continue;
    }
    const paragraph = element("p"); appendInlineMarkdown(paragraph, line); target.append(paragraph);
  }
  if (inCode && codeLines.length) {
    const pre = element("pre"); pre.append(element("code", "", codeLines.join("\n"))); target.append(pre);
  }
}

function showToast(message, error = false) {
  const toast = byId("toast");
  clearTimeout(toastTimer);
  toast.textContent = String(message || "Готово");
  toast.className = `toast${error ? " error" : ""}`;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 5500);
}

function requestId() {
  return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (appState.organizationId) headers["X-Atlas-Space-ID"] = String(appState.organizationId);
  if (options.body) {
    if (!(options.body instanceof FormData) && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = appState.data?.viewer?.csrf_token || "";
    headers["X-Idempotency-Key"] = options.idempotencyKey || requestId();
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || 50000);
  try {
    const response = await fetch(url, {
      ...options,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    if (response.status === 401) {
      location.assign("/login?next=%2Fatlas");
      throw new Error("Требуется вход");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.message || `Atlas вернул HTTP ${response.status}`);
      error.payload = payload;
      throw error;
    }
    return payload;
  } finally {
    clearTimeout(timeout);
  }
}

function openDialog(dialog) {
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function closeDialog(dialog) {
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function bindPreviewMotion() {
  const preview = byId("atlas-preview");
  if (!preview || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  let frame = 0;
  preview.addEventListener("pointermove", (event) => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const bounds = preview.getBoundingClientRect();
      const x = ((event.clientX - bounds.left) / Math.max(bounds.width, 1) - .5) * 2;
      const y = ((event.clientY - bounds.top) / Math.max(bounds.height, 1) - .5) * 2;
      preview.style.setProperty("--pointer-x", x.toFixed(3));
      preview.style.setProperty("--pointer-y", y.toFixed(3));
    });
  }, { passive: true });
  preview.addEventListener("pointerleave", () => {
    preview.style.setProperty("--pointer-x", "0");
    preview.style.setProperty("--pointer-y", "0");
  }, { passive: true });
}

function setChatFocus(active, persist = true) {
  const enabled = Boolean(active);
  appState.chatFocus = enabled;
  if (!enabled) appState.chatHistoryOpen = false;
  const root = byId("atlas-app");
  root.classList.toggle("chat-focus-mode", enabled);
  root.classList.toggle("chat-focus-history", enabled && appState.chatHistoryOpen);
  const focusButton = byId("chat-focus-toggle");
  const historyButton = byId("chat-history-toggle");
  focusButton.setAttribute("aria-pressed", String(enabled));
  focusButton.classList.toggle("active", enabled);
  focusButton.querySelector("span").textContent = enabled ? "Свернуть" : "Фокус";
  historyButton.setAttribute("aria-pressed", String(enabled && appState.chatHistoryOpen));
  historyButton.classList.toggle("active", enabled && appState.chatHistoryOpen);
  if (persist) {
    try { localStorage.setItem("tmod-atlas-chat-focus", enabled ? "1" : "0"); }
    catch (_error) { /* focus mode still works for the current tab */ }
  }
  requestAnimationFrame(() => {
    const stream = byId("chat-stream");
    stream.scrollTop = stream.scrollHeight;
  });
}

function toggleChatHistory() {
  if (!appState.chatFocus) return;
  appState.chatHistoryOpen = !appState.chatHistoryOpen;
  setChatFocus(true, false);
}

function switchScreen(screen, updateHash = true) {
  const selected = screenMeta[screen] ? screen : "home";
  const changed = appState.screen !== selected;
  const applyScreen = () => {
    appState.screen = selected;
    if (selected !== "ai" && appState.chatFocus) setChatFocus(false);
    else if (selected === "ai" && appState.chatFocus) setChatFocus(true, false);
    document.querySelectorAll(".screen").forEach((node) => {
      node.classList.toggle("active", node.id === `screen-${selected}`);
    });
    document.querySelectorAll(".atlas-nav [data-screen]").forEach((button) => {
      const active = button.dataset.screen === selected;
      button.classList.toggle("active", active);
      button.setAttribute("aria-current", active ? "page" : "false");
    });
    byId("screen-kicker").textContent = screenMeta[selected][0];
    byId("screen-title").textContent = screenMeta[selected][1];
    if (updateHash) history.replaceState(null, "", `#/${selected}`);
  };
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (changed && !reducedMotion && typeof document.startViewTransition === "function") {
    document.startViewTransition(applyScreen);
  } else {
    applyScreen();
  }
  if (changed) window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
  if (selected === "media") void loadMediaLibrary().catch((error) => showToast(error.message, true));
  if (selected === "cases") void loadCases().catch((error) => showToast(error.message, true));
}

function bindWorkspaceMotion() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || !matchMedia("(pointer: fine)").matches) return;
  document.querySelectorAll(".atlas-hero, .module-card").forEach((surface) => {
    let frame = 0;
    surface.addEventListener("pointermove", (event) => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const bounds = surface.getBoundingClientRect();
        const x = ((event.clientX - bounds.left) / Math.max(bounds.width, 1)) * 100;
        const y = ((event.clientY - bounds.top) / Math.max(bounds.height, 1)) * 100;
        surface.style.setProperty("--mx", `${x.toFixed(1)}%`);
        surface.style.setProperty("--my", `${y.toFixed(1)}%`);
      });
    }, { passive: true });
    surface.addEventListener("pointerleave", () => {
      surface.style.removeProperty("--mx");
      surface.style.removeProperty("--my");
    }, { passive: true });
  });
}

function templateCard(template) {
  const card = element("article", "template-card");
  const icon = element("i", "", template.category === "Форум" ? "↗" : "▤");
  const copy = element("span");
  copy.append(element("b", "", template.name), element("small", "", template.description || template.category));
  const button = element("button", "", "Использовать");
  button.type = "button";
  button.addEventListener("click", () => openDocumentDialog(template));
  card.append(icon, copy, button);
  return card;
}

function documentCard(document) {
  const card = element("article", "document-card");
  const icon = element("i", "", "◇");
  const copy = element("span");
  copy.append(
    element("b", "", document.title),
    element("small", "", `${document.template_name || "Без шаблона"} · ${document.status || "draft"} · r${document.revision || 1}`),
  );
  const state = element("small", "", new Date(document.updated_at).toLocaleDateString("ru-RU"));
  card.append(icon, copy, state);
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.addEventListener("click", () => void openDocumentDetail(document.id));
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); void openDocumentDetail(document.id); }
  });
  return card;
}

const documentStatusLabels = { draft: "Черновик", review: "На проверке", approved: "Одобрено", published: "Опубликовано", archived: "Архив" };

function documentProcessItem(kind, title, note, status = "") {
  const item = element("article", `document-process-item ${kind} ${status}`);
  const mark = element("i", "", kind === "revision" ? "r" : kind === "comment" ? "“" : "✓");
  const copy = element("span");
  copy.append(element("b", "", title), element("small", "", note));
  item.append(mark, copy);
  return item;
}

function documentTemplateById(templateId) {
  return (appState.data?.templates || []).find((item) => Number(item.id) === Number(templateId)) || null;
}

function documentSchemaFields(template) {
  return Array.isArray(template?.schema?.fields) ? template.schema.fields : [];
}

function renderStructuredDocumentFields(container, template, values = {}, locked = false) {
  clear(container);
  documentSchemaFields(template).forEach((field) => {
    const label = element("label");
    const caption = element("span", "document-field-caption", field.label || field.key);
    if (field.required) caption.append(element("i", "", "обязательно"));
    let control;
    if (field.type === "textarea") {
      control = document.createElement("textarea");
      control.rows = 4;
    } else if (field.type === "select") {
      control = document.createElement("select");
      control.append(new Option("Выберите значение", ""));
      (field.options || []).forEach((option) => control.append(new Option(option, option)));
    } else {
      control = document.createElement("input");
      control.type = ["date", "number"].includes(field.type) ? field.type : "text";
    }
    control.name = `field__${field.key}`;
    control.value = values?.[field.key] || "";
    control.placeholder = field.placeholder || "";
    control.required = Boolean(field.required);
    control.disabled = locked;
    label.append(caption, control);
    container.append(label);
  });
  container.hidden = !container.childElementCount;
}

function collectDocumentFields(form) {
  const fields = {};
  for (const [name, value] of new FormData(form)) {
    if (name.startsWith("field__")) fields[name.slice(7)] = value;
  }
  return fields;
}

function approvalRouteRow(item = {}, position = 1) {
  const row = element("div", "document-route-row");
  row.dataset.routeStep = "1";
  row.append(element("i", "", String(position).padStart(2, "0")));
  const inputs = element("div", "document-route-inputs");
  const title = document.createElement("input");
  title.name = "route_title";
  title.maxLength = 120;
  title.required = true;
  title.placeholder = "Название этапа";
  title.value = item.title || "Проверка ответственным";
  const target = document.createElement("input");
  target.name = "route_user";
  target.inputMode = "numeric";
  target.pattern = "[0-9]+";
  target.placeholder = "Discord ID — либо выберите роль";
  target.value = item.assigned_user_id || "";
  const role = document.createElement("select");
  role.name = "route_role";
  [["", "Без роли"], ["member", "Любой участник"], ["editor", "Редактор"], ["administrator", "Администратор"], ["owner", "Владелец"]].forEach(([value, label]) => role.append(new Option(label, value)));
  role.value = item.required_role || "";
  inputs.append(title, target, role);
  const remove = element("button", "document-route-remove", "×");
  remove.type = "button";
  remove.title = "Удалить этап";
  remove.addEventListener("click", () => { row.remove(); renumberApprovalRoute(); });
  row.append(inputs, remove);
  return row;
}

function renumberApprovalRoute() {
  [...byId("document-route-builder").children].forEach((row, index) => { row.querySelector("i").textContent = String(index + 1).padStart(2, "0"); });
}

function renderApprovalRouteBuilder(items = []) {
  const builder = byId("document-route-builder");
  clear(builder);
  (items.length ? items : [{}]).forEach((item, index) => builder.append(approvalRouteRow(item, index + 1)));
}

function renderDocumentDetail(detail) {
  appState.documentDetail = detail;
  const selected = detail.document || {};
  const header = byId("document-detail-header");
  header.querySelector("span").textContent = `ATLAS DOCUMENT · ${String(selected.status || "draft").toUpperCase()}`;
  header.querySelector("h2").textContent = selected.title || "Документ Atlas";
  header.querySelector("p").textContent = `Редакция ${selected.revision || 1} · открытых комментариев: ${detail.open_comments || 0}`;
  header.querySelector(".document-state-seal strong").textContent = documentStatusLabels[selected.status] || selected.status;
  header.querySelector(".document-state-seal").className = `document-state-seal ${selected.status || "draft"}`;
  const form = byId("document-edit-form");
  form.elements.title.value = selected.title || "";
  form.elements.rendered_text.value = selected.rendered_text || "";
  renderStructuredDocumentFields(byId("document-edit-fields"), detail.template, selected.fields || {}, ["published", "archived"].includes(selected.status));
  form.elements.rendered_text.readOnly = Boolean(detail.template);
  byId("document-rendered-label").firstChild.textContent = detail.template ? "Собранный текст · обновится после сохранения" : "Текст документа";
  form.elements.change_summary.value = "";
  [...form.elements].forEach((control) => { if (control.name) control.disabled = ["published", "archived"].includes(selected.status); });

  const revisions = byId("document-revision-list");
  clear(revisions);
  (detail.revisions || []).forEach((item) => revisions.append(documentProcessItem("revision", `Редакция ${item.revision}`, `${item.change_summary || "Изменение"} · ${readableTime(item.created_at)}`)));
  if (!detail.revisions?.length) revisions.append(element("div", "empty", "История появится после сохранения."));

  const comments = byId("document-comment-list");
  clear(comments);
  (detail.comments || []).forEach((item) => {
    const row = documentProcessItem("comment", item.author_name || `Участник ${item.author_user_id}`, `к редакции ${item.revision} · ${item.body}`, item.status);
    if (item.status === "open") {
      const resolve = element("button", "", "Закрыть");
      resolve.type = "button";
      resolve.addEventListener("click", async () => {
        resolve.disabled = true;
        try {
          await api(`/api/atlas/documents/${selected.id}/comments/${item.id}`, { method: "PATCH", body: "{}" });
          await refreshOpenDocument();
        } catch (error) { showToast(error.message || "Комментарий не закрыт.", true); }
        finally { resolve.disabled = false; }
      });
      row.append(resolve);
    }
    comments.append(row);
  });
  if (!detail.comments?.length) comments.append(element("div", "empty", "Комментариев к документу нет."));

  const approvals = byId("document-approval-list");
  clear(approvals);
  (detail.approvals || []).forEach((item) => {
    const row = documentProcessItem("approval", `${item.step_order}. ${item.title}`, `${item.assigned_name || item.required_role || `Участник ${item.assigned_user_id}`} · ${({ pending: "ожидает", approved: "одобрено", rejected: "отклонено", skipped: "пропущено" })[item.status] || item.status}`, item.status);
    if (item.status === "pending") {
      const actions = element("div", "document-approval-actions");
      const decide = async (decision, button) => {
        button.disabled = true;
        try {
          await api(`/api/atlas/documents/${selected.id}/approvals/${item.id}`, { method: "PATCH", body: JSON.stringify({ decision }) });
          await refreshOpenDocument();
        } catch (error) { showToast(error.message || "Решение не сохранено.", true); }
        finally { button.disabled = false; }
      };
      const approve = element("button", "", "Одобрить"); approve.type = "button";
      const reject = element("button", "danger", "Отклонить"); reject.type = "button";
      approve.addEventListener("click", () => void decide("approved", approve));
      reject.addEventListener("click", () => void decide("rejected", reject));
      actions.append(approve, reject); row.append(actions);
    }
    approvals.append(row);
  });
  if (!detail.approvals?.length) approvals.append(element("div", "empty", "Маршрут ещё не назначен."));
  renderApprovalRouteBuilder(detail.approvals || []);

  const next = byId("document-next-state");
  const transition = { draft: ["review", "Передать на проверку"], review: ["approved", "Подтвердить документ"], approved: ["published", "Опубликовать"] }[selected.status];
  next.hidden = !transition;
  next.dataset.status = transition?.[0] || "";
  next.textContent = transition?.[1] || "Готово";
}

async function refreshOpenDocument() {
  const documentId = Number(appState.documentDetail?.document?.id || 0);
  if (!documentId) return;
  renderDocumentDetail(await api(`/api/atlas/documents/${documentId}`, { timeout: 15000 }));
}

async function openDocumentDetail(documentId) {
  const dialog = byId("document-detail-dialog");
  openDialog(dialog);
  byId("document-detail-header").querySelector("h2").textContent = "Загрузка документа…";
  try { renderDocumentDetail(await api(`/api/atlas/documents/${Number(documentId)}`, { timeout: 15000 })); }
  catch (error) { closeDialog(dialog); showToast(error.message || "Документ не открылся.", true); }
}

function renderOnboarding(membership) {
  const step = Number(membership?.onboarding_step || 0);
  const card = byId("onboarding-card");
  card.hidden = step >= 4;
  document.querySelectorAll("#onboarding-steps [data-step]").forEach((button) => {
    button.classList.toggle("done", Number(button.dataset.step) <= step);
  });
}

function threadCard(thread) {
  const button = element("button", "atlas-thread-card");
  button.type = "button";
  button.dataset.threadId = String(thread.id);
  button.classList.toggle("active", Number(thread.id) === Number(appState.threadId));
  const copy = element("span");
  const agent = atlasAgent(thread.agent_id);
  copy.append(
    element("b", "", thread.title || "Новый диалог"),
    element("small", "", `${agent.short_name || agent.name} · ${thread.preview || "Диалог без сообщений"}`),
  );
  const date = element("time", "", readableTime(thread.updated_at, ""));
  button.append(copy, date);
  button.addEventListener("click", async () => {
    if (appState.busy) {
      showToast("Дождитесь ответа Atlas перед переключением диалога.");
      return;
    }
    try { await loadThread(Number(thread.id)); }
    catch (error) { showToast(error.message || "Не удалось открыть диалог.", true); }
  });
  return button;
}

function renderThreads(items) {
  const threads = Array.isArray(items) ? items : [];
  const list = byId("atlas-thread-list");
  clear(list);
  threads.forEach((thread) => list.append(threadCard(thread)));
  if (!threads.length) list.append(element("div", "empty", "Диалогов пока нет."));
}

function resetChatStream() {
  const stream = byId("chat-stream");
  clear(stream);
  const welcome = messageNode(
    "assistant",
    "Я могу проверить норму, продолжить прошлую мысль или создать речь и документ на основе правил. Факты останутся проверяемыми, а творческая часть — свободной.",
  );
  welcome.classList.add("welcome");
  stream.append(welcome);
}

function selectedServer() {
  return (appState.data?.catalog?.servers || []).find((item) => item.code === appState.serverCode) || { label: "Phoenix (15)" };
}

function selectedFaction() {
  return (appState.data?.catalog?.factions || []).find((item) => item.code === appState.factionCode) || { label: "LSPD" };
}

function scopeLabel() {
  return `${selectedServer().label} · ${selectedFaction().label}`;
}

function knowledgeScopeLabel(scope) {
  return {
    global: "Весь Atlas",
    server: "Весь сервер",
    faction: "Организация",
    workspace: "Рабочая группа",
  }[scope] || "Рабочая группа";
}

function sourceVisibleHere(item) {
  const scope = item.visibility_scope || "workspace";
  if (scope === "global") return true;
  if (scope === "server") return item.server_code === appState.serverCode;
  if (scope === "faction") {
    return item.server_code === appState.serverCode && item.faction_code === appState.factionCode;
  }
  return item.server_code === appState.serverCode && item.faction_code === appState.factionCode;
}

function fillSelect(select, items, selected) {
  clear(select);
  items.filter((item) => item.enabled !== false).forEach((item) => {
    select.append(new Option(item.label || item.name, item.code, false, item.code === selected));
  });
}

function renderCatalog(data) {
  const catalog = data.catalog || { servers: [], factions: [], defaults: {} };
  const profile = data.membership?.profile || {};
  if (!appState.scopeReady) {
    appState.serverCode = profile.server_code || catalog.defaults?.server_code || "phoenix-15";
    appState.factionCode = profile.faction_code || catalog.defaults?.faction_code || "lspd";
    appState.scopeReady = true;
  }
  fillSelect(byId("atlas-server"), catalog.servers || [], appState.serverCode);
  fillSelect(byId("atlas-faction"), catalog.factions || [], appState.factionCode);
  fillSelect(byId("onboarding-server"), catalog.servers || [], appState.serverCode);
  fillSelect(byId("onboarding-faction"), catalog.factions || [], appState.factionCode);
  byId("onboarding-nickname").value = profile.nickname || "";
  byId("onboarding-rank").value = profile.rank || profile.role || "";
  byId("onboarding-direction").value = profile.direction || "";
  byId("vault-scope").textContent = scopeLabel();
  byId("source-library-title").textContent = scopeLabel();
}

function sourceStatus(item) {
  if (item.status === "indexed") return ["ready", "Готов"];
  if (item.status === "failed") return ["failed", "Нужна проверка"];
  return ["pending", "Подготавливается"];
}

function knowledgeSourceCard(item) {
  const card = element("article", "knowledge-source-card");
  const icon = element("i", "", item.original_filename ? "⇧" : "≡");
  const copy = element("span");
  const taxonomy = item.metadata?.taxonomy || {};
  const details = [
    knowledgeScopeLabel(item.visibility_scope),
    taxonomy.domain ? String(taxonomy.domain).toUpperCase() : null,
    taxonomy.corpus_kind || item.source_kind || "материал",
    item.original_filename || "текст",
  ].filter(Boolean);
  if (item.updated_at) details.push(new Date(item.updated_at).toLocaleDateString("ru-RU"));
  copy.append(element("b", "", item.title), element("small", "", details.join(" · ")));
  const [statusClass, statusText] = sourceStatus(item);
  const status = element("em", `source-status ${statusClass}`, statusText);
  if (item.status === "failed") status.title = "Откройте журнал Atlas в Ядерном Реакторе или загрузите материал повторно.";
  card.append(icon, copy, status);
  return card;
}

function renderKnowledgeSources(items) {
  const selected = (Array.isArray(items) ? items : []).filter(
    sourceVisibleHere,
  );
  const list = byId("knowledge-source-list");
  clear(list);
  selected.forEach((item) => list.append(knowledgeSourceCard(item)));
  if (!selected.length) list.append(element("div", "empty", "В этом разделе пока нет материалов."));
  const ready = selected.filter((item) => item.status === "indexed").length;
  byId("vault-count").textContent = String(ready);
  byId("metric-knowledge").textContent = String(ready);
}

function readableTime(value, fallback) {
  if (!value) return fallback;
  const selected = new Date(value);
  if (Number.isNaN(selected.getTime())) return fallback;
  return selected.toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
}

function renderForumSync(value) {
  const status = value && typeof value === "object" ? value : { status: "waiting" };
  const labels = {
    waiting: "Ожидает первого запуска",
    pending: "Готовится к первой сверке",
    running: "Проверяет форум сейчас",
    ok: "База подтверждена",
    attention: "Нужно внимание администратора",
    error: "Сверка будет повторена",
    disabled: "Автоматическая сверка отключена",
  };
  const state = status.status || "waiting";
  byId("forum-sync-status").textContent = labels[state] || "Состояние уточняется";
  byId("forum-sync-light").className = state;
  byId("forum-sync-last").textContent = readableTime(status.last_success_at, "Ещё не выполнялась");
  byId("forum-sync-next").textContent = readableTime(status.next_sync_at, "После первого запуска");
  const stats = status.last_stats || {};
  byId("forum-sync-pages").textContent = String(stats.pages || 0);
  byId("forum-sync-changed").textContent = String(stats.changed || 0);
  const message = byId("forum-sync-message");
  message.classList.toggle("warning", state === "attention" || state === "error");
  const rawError = status.last_error ? String(status.last_error) : "";
  if (rawError && stats.phase === "forum_read") {
    message.textContent = "Не удалось прочитать форум. Откройте локальный Chromium: окно оставлено активным для авторизации и проверки.";
    message.title = rawError;
  } else if (rawError && stats.phase === "knowledge_index") {
    message.textContent = "Форум прочитан, но часть материалов пока не попала в интеллектуальный поиск. Atlas повторит индексирование автоматически.";
    message.title = rawError;
  } else {
    message.textContent = rawError || "Последняя подтверждённая версия всегда остаётся доступной Atlas AI.";
    message.removeAttribute("title");
  }
  const button = byId("forum-sync-now");
  button.disabled = state === "running" || state === "disabled";
  button.firstChild.textContent = state === "running" ? "Проверка уже выполняется " : "Проверить обновления сейчас ";
}

const timelineKindLabels = {
  incident: "Инцидент",
  activity: "Действие",
  decision: "Решение",
  document: "Документ",
  communication: "Коммуникация",
  note: "Заметка",
  system: "Система",
};
const timelineStatusLabels = { open: "Открыто", active: "В работе", resolved: "Завершено", archived: "Архив" };

function timelineCard(item, index) {
  const card = element("article", `memory-event ${item.importance || "routine"}`);
  card.style.setProperty("--event-delay", `${Math.min(index, 10) * 35}ms`);
  const rail = element("span", "memory-event-rail");
  rail.append(element("i"), element("b", "", String(index + 1).padStart(2, "0")));
  const copy = element("div", "memory-event-copy");
  const meta = element("div", "memory-event-meta");
  meta.append(
    element("span", `kind ${item.event_kind || "activity"}`, timelineKindLabels[item.event_kind] || "Событие"),
    element("span", `status ${item.status || "open"}`, timelineStatusLabels[item.status] || item.status || "Открыто"),
    element("time", "", readableTime(item.occurred_at, "Время не указано")),
  );
  const title = element("h3", "", item.title || "Событие Atlas");
  const summary = element("p", "", item.summary || "Контекст будет дополнен позднее.");
  const footer = element("footer");
  const action = element(
    "button",
    "memory-event-action",
    item.status === "resolved" ? "Вернуть в работу" : "Завершить",
  );
  action.type = "button";
  action.addEventListener("click", async () => {
    action.disabled = true;
    try {
      await api(`/api/atlas/timeline/${encodeURIComponent(item.id)}`, {
        method: "PATCH",
        body: JSON.stringify({
          status: item.status === "resolved" ? "active" : "resolved",
          expected_version: item.version || 1,
        }),
      });
      await reload();
      switchScreen("memory");
      showToast(item.status === "resolved" ? "Событие возвращено в работу." : "Событие завершено.");
    } catch (error) {
      action.disabled = false;
      showToast(error.message || "Состояние события не изменено.", true);
    }
  });
  footer.append(
    element("span", "", item.actor_display_name || `Участник ${item.actor_user_id || "Atlas"}`),
    element("small", "", item.source_type ? `Источник: ${item.source_type} · ${item.source_id}` : "Сохранено напрямую в Atlas Memory"),
    action,
  );
  copy.append(meta, title, summary, footer);
  card.append(rail, copy);
  return card;
}

function renderTimeline(items, summary = {}) {
  const selected = Array.isArray(items) ? items : [];
  const root = byId("memory-timeline");
  clear(root);
  selected.forEach((item, index) => root.append(timelineCard(item, index)));
  if (!selected.length) root.append(element("div", "empty memory-empty", "История начнётся с первого события."));
  byId("memory-total").textContent = String(summary.total || 0);
  byId("memory-active").textContent = String(summary.active || 0);
  byId("memory-attention").textContent = String(summary.attention || 0);
  byId("memory-resolved").textContent = String(summary.resolved || 0);
}

function render(data) {
  appState.data = data;
  renderCatalog(data);
  const viewer = data.viewer || {};
  const organization = data.organization || {};
  const membership = data.membership || {};
  byId("viewer-name").textContent = viewer.name || "Участник";
  byId("viewer-avatar").textContent = String(viewer.name || "A").charAt(0).toUpperCase();
  const spaceSelect = byId("atlas-space");
  clear(spaceSelect);
  (data.spaces || [organization]).forEach((space) => {
    spaceSelect.append(new Option(space.name || "Личное пространство", String(space.id)));
  });
  appState.organizationId = Number(organization.id) || null;
  if (appState.organizationId) {
    spaceSelect.value = String(appState.organizationId);
    try { localStorage.setItem("tmod-atlas-space", String(appState.organizationId)); }
    catch (_error) { /* private mode can disable local storage */ }
  }
  byId("space-role").textContent = `${membership.role || "member"} · ${organization.kind || "project"}`;
  byId("atlas-admin-link").hidden = !viewer.administrator;
  const counts = data.counts || {};
  byId("metric-documents").textContent = String(counts.documents || 0);
  byId("metric-knowledge").textContent = String(counts.knowledge || 0);
  byId("metric-members").textContent = String(counts.members || 0);
  byId("metric-threads").textContent = String(counts.threads || 0);
  byId("metric-events").textContent = String(counts.events || 0);
  renderTimeline(data.timeline || [], data.timeline_summary || {});
  renderKnowledgeSources(data.knowledge_sources || []);
  renderForumSync(data.forum_sync);
  renderThreads(data.threads || []);
  renderOnboarding(membership);

  const ai = data.ai || {};
  const modelSelect = byId("atlas-model");
  clear(modelSelect);
  (ai.models || [{ id: "atlas-tvr-a", name: "atlas-tvr-a" }]).forEach((model) => {
    modelSelect.append(new Option(model.name || model.id, model.id, false, model.id === appState.modelId));
  });
  if (![...modelSelect.options].some((option) => option.value === appState.modelId)) appState.modelId = "atlas-tvr-a";
  modelSelect.value = appState.modelId;
  const aiState = byId("ai-state");
  aiState.classList.toggle("warning", !ai.configured || ai.qdrant !== "ok");
  aiState.querySelector("small").textContent = ai.configured && ai.qdrant === "ok" ? "готов" : "настройка";
  byId("context-model").textContent = ai.configured ? "Ответы по проверенным материалам" : "Помощник ещё настраивается";
  byId("context-collection").textContent = ai.qdrant === "ok" ? "библиотека подключена" : "библиотека временно недоступна";

  const templates = Array.isArray(data.templates) ? data.templates : [];
  const templateList = byId("template-list");
  clear(templateList);
  templates.forEach((item) => templateList.append(templateCard(item)));
  if (!templates.length) templateList.append(element("div", "empty", "Шаблоны ещё не опубликованы."));
  byId("template-count").textContent = String(templates.length);
  const select = byId("document-template");
  clear(select);
  select.append(new Option("Без шаблона", ""));
  templates.forEach((item) => select.append(new Option(`${item.category} · ${item.name}`, String(item.id))));

  const documents = Array.isArray(data.documents) ? data.documents : [];
  const documentList = byId("document-list");
  clear(documentList);
  documents.forEach((item) => documentList.append(documentCard(item)));
  if (!documents.length) documentList.append(element("div", "empty", "Создайте первый документ из шаблона."));
  byId("document-count").textContent = String(documents.length);
  byId("document-total").textContent = String(documents.length);
  byId("document-draft-count").textContent = String(documents.filter((item) => item.status === "draft").length);
  byId("document-review-count").textContent = String(documents.filter((item) => item.status === "review").length);
  byId("document-approved-count").textContent = String(documents.filter((item) => ["approved", "published"].includes(item.status)).length);

  const knowledgeEditor = byId("knowledge-editor");
  knowledgeEditor.classList.toggle("locked", !viewer.administrator);
  if (!viewer.administrator) {
    knowledgeEditor.querySelector("h3").textContent = "Добавление источников доступно администратору";
  }
}

function messageNode(role, text, citations = [], options = {}) {
  const row = element("div", role === "user" ? "user-message" : "assistant-message");
  const agent = atlasAgent(options.agentId);
  if (role !== "user") row.append(element("span", "", agent.glyph || "A"));
  const copy = element("div");
  if (role !== "user") copy.append(element("small", "", `${String(agent.short_name || "ATLAS").toUpperCase()} · АГЕНТ ATLAS`));
  const richText = element("div", "rich-text");
  renderRichText(richText, text);
  copy.append(richText);
  appendCitations(copy, citations);
  if (role !== "user") appendAnswerFeedback(copy, options.messageId, options.feedback);
  row.append(copy);
  return row;
}

function appendAnswerFeedback(copy, messageId, current = "") {
  if (!Number(messageId) || copy.querySelector(".answer-feedback")) return;
  const panel = element("div", "answer-feedback");
  panel.append(element("small", "", "Ответ был полезен?"));
  const actions = element("div", "answer-feedback-actions");
  const good = element("button", "feedback-good", "✓ Хороший ответ");
  const bad = element("button", "feedback-bad", "× Плохой ответ");
  [good, bad].forEach((button) => { button.type = "button"; });
  const paint = (rating) => {
    good.classList.toggle("selected", rating === "good");
    bad.classList.toggle("selected", rating === "bad");
  };
  const submit = async (rating) => {
    good.disabled = true;
    bad.disabled = true;
    try {
      await api(`/api/atlas/messages/${encodeURIComponent(messageId)}/feedback`, {
        method: "POST",
        body: JSON.stringify({ rating }),
      });
      paint(rating);
      panel.querySelector("small").textContent = rating === "good"
        ? "Спасибо — удачный ответ отмечен"
        : "Спасибо — этот ответ не попадёт в память Atlas";
    } catch (error) {
      showToast(error.message || "Оценка не сохранена", true);
    } finally {
      good.disabled = false;
      bad.disabled = false;
    }
  };
  good.addEventListener("click", () => submit("good"));
  bad.addEventListener("click", () => submit("bad"));
  paint(current);
  actions.append(good, bad);
  panel.append(actions);
  copy.append(panel);
}

function updateResearchProgress(copy, event) {
  let panel = copy.querySelector(".research-progress");
  if (!panel) {
    panel = element("section", "research-progress");
    panel.append(element("header", "", "АРИСТОТЕЛЬ · ИССЛЕДОВАТЕЛЬСКАЯ КОМАНДА"), element("ol", "research-steps"), element("footer", "", "Проектируем персональный план…"));
    copy.insertBefore(panel, copy.querySelector(".rich-text"));
  }
  if (event.phase === "planning") {
    panel.querySelector("footer").textContent = "Аристотель определяет нужные роли и задачи";
  } else if (event.phase === "plan") {
    const list = panel.querySelector("ol");
    clear(list);
    (event.steps || []).forEach((step) => {
      const item = element("li", "pending");
      item.dataset.stepId = step.id;
      const description = element("span");
      description.append(element("b", "", step.agent), element("small", "agent-assignment", `${step.role || "Исследователь"} · ${step.title}`));
      item.append(element("i", "", "○"), description);
      list.append(item);
    });
    panel.querySelector("footer").textContent = event.source === "fallback"
      ? "Резервный план активирован — исследование продолжится"
      : "Уникальный план создан специально для этого запроса";
  } else if (event.phase === "stage") {
    const item = [...panel.querySelectorAll("[data-step-id]")].find(
      (candidate) => candidate.dataset.stepId === String(event.step_id || ""),
    );
    if (item) {
      item.className = event.status || "pending";
      item.querySelector("i").textContent = event.status === "complete" ? "✓" : "●";
    }
  } else if (event.phase === "agent_result") {
    const item = [...panel.querySelectorAll("[data-step-id]")].find(
      (candidate) => candidate.dataset.stepId === String(event.step_id || ""),
    );
    if (item) {
      item.className = event.status || "complete";
      item.querySelector("i").textContent = event.status === "complete" ? "✓" : "!";
      const detail = item.querySelector("small");
      if (detail && event.detail) detail.textContent = event.detail;
      if (event.report) {
        let report = item.querySelector("details");
        if (!report) {
          report = element("details", "agent-work-report");
          report.append(element("summary", "", "Открыть рабочий отчёт"), element("p"));
          item.querySelector("span").append(report);
        }
        report.querySelector("p").textContent = event.report;
        const seconds = Math.max(.1, Number(event.elapsed_ms || 0) / 1000).toFixed(1);
        report.querySelector("summary").textContent = `Рабочий отчёт · ${seconds} с`;
      }
    }
  } else if (event.phase === "evidence") {
    const domains = Object.entries(event.domains || {}).map(([key, count]) => `${key}: ${count}`).join(" · ");
    panel.querySelector("footer").textContent = `Найдено источников: ${event.source_count || 0}${domains ? ` · ${domains}` : ""}`;
  } else if (event.phase === "complete") {
    panel.classList.add("complete");
    panel.querySelector("footer").textContent = `Исследование завершено · источников: ${event.source_count || 0}`;
  }
}

function appendCitations(copy, citations = []) {
  if (citations.length) {
    const list = element("div", "citation-list");
    citations.forEach((citation) => {
      const pinpoints = Array.isArray(citation.pinpoints) && citation.pinpoints.length
        ? ` · ${citation.pinpoints.slice(0, 4).join(", ")}`
        : "";
      const label = `[${citation.index}] ${citation.title}${pinpoints}`;
      const node = citation.url ? element("a", "", label) : element("span", "", label);
      if (citation.url) {
        node.href = citation.url;
        node.target = "_blank";
        node.rel = "noreferrer";
      }
      list.append(node);
    });
    copy.append(list);
  }
}

async function sendQuestion(question) {
  if (appState.busy) return;
  appState.busy = true;
  const stream = byId("chat-stream");
  stream.append(messageNode("user", question));
  stream.scrollTop = stream.scrollHeight;
  const button = byId("atlas-chat-form").querySelector("button[type='submit']");
  button.disabled = true;
  button.textContent = "Atlas отвечает…";
  const assistant = messageNode("assistant", "");
  assistant.classList.add("streaming");
  const answerNode = assistant.querySelector(".rich-text");
  const answerCopy = answerNode.parentElement;
  stream.append(assistant);
  let responseTimeout = null;
  try {
    const controller = new AbortController();
    responseTimeout = setTimeout(() => controller.abort(), 90000);
    const response = await fetch("/api/atlas/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": appState.data?.viewer?.csrf_token || "",
        "X-Idempotency-Key": requestId(),
        ...(appState.organizationId ? { "X-Atlas-Space-ID": String(appState.organizationId) } : {}),
      },
      body: JSON.stringify({
        question,
        thread_id: appState.threadId,
        response_mode: appState.responseMode,
        model: appState.modelId,
        server_code: appState.serverCode,
        faction_code: appState.factionCode,
      }),
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    if (response.status === 401) {
      location.assign("/login?next=%2Fatlas");
      throw new Error("Требуется вход");
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.message || `Atlas вернул HTTP ${response.status}`);
    }
    if (!response.body?.getReader) throw new Error("Браузер не поддерживает потоковые ответы Atlas.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let result = null;
    let streamError = null;
    let streamedText = "";
    const acceptEvent = (event) => {
      if (event.type === "delta" && event.text) {
        streamedText += String(event.text);
        answerNode.textContent = streamedText;
        assistant.classList.add("has-content");
        stream.scrollTop = stream.scrollHeight;
      } else if (event.type === "progress") {
        updateResearchProgress(answerCopy, event);
        stream.scrollTop = stream.scrollHeight;
      } else if (event.type === "done") {
        result = event;
      } else if (event.type === "error") {
        streamError = new Error(event.message || "Ответ Atlas прерван.");
      }
    };
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        lines.filter(Boolean).forEach((line) => {
          try {
            const payloadLine = line.startsWith("data:") ? line.slice(5).trim() : line;
            if (payloadLine) acceptEvent(JSON.parse(payloadLine));
          }
          catch (_error) { /* incomplete/foreign event is safely ignored */ }
        });
        if (done) break;
      }
      if (buffer.trim()) {
        const payloadLine = buffer.startsWith("data:") ? buffer.slice(5).trim() : buffer;
        if (payloadLine) acceptEvent(JSON.parse(payloadLine));
      }
    } finally {
      clearTimeout(responseTimeout);
      responseTimeout = null;
      reader.releaseLock();
    }
    if (streamError) throw streamError;
    if (!result) throw new Error("Atlas не подтвердил завершение ответа.");
    renderRichText(answerNode, result.answer || streamedText || "Ответ готов.");
    appendCitations(answerCopy, result.citations || []);
    appendAnswerFeedback(answerCopy, result.message_id, "");
    assistant.classList.remove("streaming");
    appState.threadId = Number(result.thread_id);
    byId("current-thread-title").textContent = result.thread?.title || question.slice(0, 100);
    try { await loadThreads(); }
    catch (_error) { showToast("Ответ готов, но список диалогов обновится позже."); }
    byId("atlas-question").value = "";
  } catch (error) {
    if (!answerNode.textContent) answerNode.textContent = error.message || "Ответ временно недоступен.";
    else answerCopy.append(element("small", "stream-interrupted", "Связь прервалась — ответ сохранится только после полного завершения."));
    assistant.classList.remove("streaming");
    assistant.classList.add("stream-error");
    showToast(error.message, true);
  } finally {
    clearTimeout(responseTimeout);
    appState.busy = false;
    button.disabled = false;
    button.textContent = "Отправить ↑";
    stream.scrollTop = stream.scrollHeight;
  }
}

function openDocumentDialog(template = null) {
  appState.selectedTemplate = template;
  byId("document-template").value = template ? String(template.id) : "";
  renderStructuredDocumentFields(byId("document-template-fields"), template);
  byId("document-freeform-label").hidden = Boolean(template);
  openDialog(byId("document-dialog"));
}

function openMemoryDialog() {
  const field = byId("memory-event-form").elements.occurred_at;
  if (!field.value) {
    const localNow = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
    field.value = localNow.toISOString().slice(0, 16);
  }
  openDialog(byId("memory-event-dialog"));
}

async function reload() {
  const data = await api("/api/atlas/bootstrap");
  if (data.preview) appState.data = data;
  else {
    render(data);
    await loadKnowledgeSources();
    if (!appState.threadReady) {
      appState.threadReady = true;
      const latest = (data.threads || [])[0];
      if (latest) await loadThread(Number(latest.id));
      else newChat();
    }
  }
}

async function loadThreads() {
  const result = await api("/api/atlas/threads");
  appState.data.threads = result.items || [];
  renderThreads(appState.data.threads);
}

async function loadThread(threadId) {
  const result = await api(`/api/atlas/threads/${encodeURIComponent(threadId)}`);
  appState.threadId = Number(result.thread.id);
  appState.modelId = result.thread.agent_id || "atlas-tvr-a";
  byId("atlas-model").value = appState.modelId;
  const selectedAgent = atlasAgent(appState.modelId);
  byId("context-model").textContent = selectedAgent.name;
  byId("context-collection").textContent = selectedAgent.description;
  byId("current-thread-title").textContent = result.thread.title || "Диалог";
  const stream = byId("chat-stream");
  clear(stream);
  (result.messages || []).forEach((message) => {
    stream.append(messageNode(message.role, message.content_text, message.citations || [], {
      messageId: message.id,
      feedback: message.feedback_rating || "",
      agentId: message.model || result.thread.agent_id,
    }));
  });
  if (!(result.messages || []).length) resetChatStream();
  renderThreads(appState.data?.threads || []);
  stream.scrollTop = stream.scrollHeight;
}

function newChat() {
  if (appState.busy) {
    showToast("Дождитесь ответа Atlas перед созданием нового диалога.");
    return;
  }
  appState.threadId = null;
  byId("current-thread-title").textContent = "Новый диалог";
  resetChatStream();
  renderThreads(appState.data?.threads || []);
  byId("atlas-question").focus();
}

async function loadKnowledgeSources() {
  const query = new URLSearchParams({ server_code: appState.serverCode, faction_code: appState.factionCode });
  const result = await api(`/api/atlas/knowledge?${query}`);
  appState.data.knowledge_sources = result.items || [];
  renderKnowledgeSources(appState.data.knowledge_sources);
}

function formatBytes(value) {
  let size = Math.max(0, Number(value || 0));
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size >= 10 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

function mediaStatusLabel(status) {
  return ({ ready: "Готов", uploading: "Загрузка", processing: "Обработка", failed: "Нужна проверка", archived: "Архив" })[status] || status || "Подготовка";
}

function mediaAssetCard(item) {
  const card = element("article", `media-asset-card ${item.status || "uploading"}`);
  const visual = element("div", "media-asset-visual");
  const symbol = item.media_kind === "video" ? "▶" : item.media_kind === "audio" ? "♪" : item.media_kind === "image" ? "▧" : "◇";
  visual.append(element("i", "", symbol), element("small", "", String(item.media_kind || "file").toUpperCase()));
  const copy = element("div", "media-asset-copy");
  const meta = element("div", "media-asset-meta");
  meta.append(
    element("span", `media-state ${item.status || ""}`, mediaStatusLabel(item.status)),
    element("span", "", item.visibility_scope === "workspace" ? "Пространство" : "Личный"),
    element("span", "", formatBytes(item.size_bytes)),
  );
  copy.append(meta, element("h3", "", item.title || item.original_filename), element("p", "", item.original_filename || "Материал Atlas"));
  const footer = element("footer");
  footer.append(element("time", "", readableTime(item.created_at, "только что")));
  if (item.content_url) {
    const open = element("a", "", item.media_kind === "file" ? "Скачать ↗" : "Открыть ↗");
    open.href = item.content_url;
    open.target = "_blank";
    open.rel = "noreferrer noopener";
    footer.append(open);
  } else footer.append(element("span", "", item.error || "Atlas продолжит обработку в фоне"));
  copy.append(footer);
  card.append(visual, copy);
  return card;
}

function renderMediaLibrary(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const quota = payload?.quota || {};
  const list = byId("media-library-list");
  clear(list);
  items.forEach((item) => list.append(mediaAssetCard(item)));
  if (!items.length) list.append(element("div", "empty", "Первый материал появится здесь после загрузки."));
  byId("media-count").textContent = String(items.length);
  byId("media-ready").textContent = String(items.filter((item) => item.status === "ready").length);
  byId("media-processing").textContent = String(items.filter((item) => ["uploading", "processing"].includes(item.status)).length);
  byId("media-quota").textContent = formatBytes(quota.used_bytes || 0);
  byId("media-quota-note").textContent = `из ${formatBytes(quota.limit_bytes || 0)}`;
  appState.data.media = payload;
}

async function loadMediaLibrary() {
  const result = await api("/api/atlas/media", { timeout: 15000 });
  renderMediaLibrary(result);
  return result;
}

function paintMediaProgress(percent, title = "Загрузка материала") {
  const panel = byId("media-upload-progress");
  panel.hidden = false;
  const bounded = Math.max(0, Math.min(100, Math.round(Number(percent || 0))));
  panel.style.setProperty("--upload-progress", `${bounded}%`);
  panel.querySelector("b").textContent = title;
  panel.querySelector("small").textContent = `${bounded}%`;
}

async function followMediaJob(job) {
  if (!Number(job?.id)) return;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, attempt < 10 ? 500 : 1500));
    const result = await api(`/api/atlas/jobs/${Number(job.id)}`, { timeout: 10000 });
    const current = result.job || {};
    if (["pending", "running", "retry"].includes(current.status)) {
      paintMediaProgress(Math.max(94, Number(current.progress?.percent || 0)), "Atlas проверяет и сохраняет файл");
      continue;
    }
    await loadMediaLibrary();
    if (current.status === "succeeded") {
      paintMediaProgress(100, "Материал готов");
      showToast("Материал сохранён в медиасети Atlas.");
    } else {
      showToast("Материал сохранён, но не прошёл обработку. Состояние можно повторить из журнала задач.", true);
    }
    setTimeout(() => { byId("media-upload-progress").hidden = true; }, 1800);
    return;
  }
}

async function uploadMediaFile(file, values) {
  const started = await api("/api/atlas/media/uploads", {
    method: "POST",
    body: JSON.stringify({
      title: String(values.title || "") || file.name,
      filename: file.name,
      size_bytes: file.size,
      mime_type: file.type || "application/octet-stream",
      visibility_scope: values.visibility_scope || "private",
      retention_policy: values.retention_policy || "manual",
      media_kind: file.type.startsWith("video/") ? "video" : file.type.startsWith("audio/") ? "audio" : file.type.startsWith("image/") ? "image" : "file",
    }),
    timeout: 15000,
  });
  const chunkSize = Math.max(256 * 1024, Number(started.chunk_bytes || 4 * 1024 * 1024));
  let offset = Number(started.upload?.received_size || 0);
  let finalResult = null;
  while (offset < file.size) {
    const chunk = await file.slice(offset, Math.min(file.size, offset + chunkSize)).arrayBuffer();
    finalResult = await api(started.upload_url, {
      method: "PUT",
      body: chunk,
      headers: { "Content-Type": "application/octet-stream", "Upload-Offset": String(offset) },
      timeout: 45000,
    });
    offset = Number(finalResult.upload?.received_size || offset + chunk.byteLength);
    paintMediaProgress((offset / file.size) * 92, "Передаём материал в Atlas");
  }
  return finalResult;
}

const caseStatusLabels = {
  intake: "Приём", investigation: "Расследование", review: "Проверка",
  ready: "Готово", closed: "Закрыто", archived: "Архив",
};
const caseKindLabels = { incident: "Инцидент", investigation: "Расследование", legal: "Правовой материал", request: "Обращение" };
const casePriorityLabels = { routine: "Обычный", high: "Высокий", critical: "Критический" };
const evidenceTypeLabels = { note: "Заметка", timeline_event: "Событие", media_asset: "Материал", media_segment: "Фрагмент", document: "Документ", knowledge_source: "Источник", url: "Ссылка" };

function caseReadiness(item) {
  return item?.readiness || { score: 0, state: "not_ready", unsupported_claim_ids: [], critical_gap_ids: [], pending_evidence_ids: [] };
}

function caseCard(item) {
  const readiness = caseReadiness(item);
  const card = element("button", `case-card ${item.priority || "routine"}`);
  card.type = "button";
  card.dataset.caseId = String(item.id);
  const index = element("div", "case-card-index");
  index.append(element("small", "", `ДЕЛО ${String(item.case_number || item.id).padStart(3, "0")}`), element("strong", "", `${Number(readiness.score || 0)}%`));
  const copy = element("div", "case-card-copy");
  const meta = element("div", "case-card-meta");
  meta.append(
    element("span", `case-status ${item.status || "intake"}`, caseStatusLabels[item.status] || item.status),
    element("span", "", caseKindLabels[item.case_kind] || item.case_kind),
    element("span", "", item.visibility_scope === "private" ? "Личное" : "Пространство"),
  );
  copy.append(meta, element("h3", "", item.title || "Без названия"), element("p", "", item.objective || item.executive_summary || "Цель проверки пока не описана."));
  const footer = element("footer");
  footer.append(
    element("span", "", `${Number(item.claim_count || readiness.claims || 0)} утвержд. · ${Number(item.evidence_count || readiness.evidence || 0)} доказ.`),
    element("time", "", readableTime(item.updated_at, "только что")),
  );
  copy.append(footer);
  const gauge = element("div", `case-card-gauge ${readiness.state || "not_ready"}`);
  gauge.style.setProperty("--case-score", `${Math.max(0, Math.min(100, Number(readiness.score || 0)))}%`);
  gauge.append(element("i"), element("small", "", readiness.state === "ready" ? "готово" : "проверка"));
  card.append(index, copy, gauge);
  card.addEventListener("click", () => void openCaseDetail(item.id));
  return card;
}

function renderCases(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const list = byId("case-list");
  clear(list);
  items.forEach((item) => list.append(caseCard(item)));
  if (!items.length) list.append(element("div", "empty", "Откройте первое дело, чтобы начать проверку фактов."));
  byId("case-open-count").textContent = String(items.filter((item) => !["closed", "archived"].includes(item.status)).length);
  byId("case-ready-count").textContent = String(items.filter((item) => caseReadiness(item).state === "ready").length);
  byId("case-claim-count").textContent = String(items.reduce((sum, item) => sum + Number(item.claim_count || item.readiness?.claims || 0), 0));
  byId("case-evidence-count").textContent = String(items.reduce((sum, item) => sum + Number(item.evidence_count || item.readiness?.evidence || 0), 0));
  appState.data.cases = payload;
}

async function loadCases() {
  const status = byId("case-status-filter")?.value || "";
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  const result = await api(`/api/atlas/cases${query}`, { timeout: 15000 });
  renderCases(result);
  return result;
}

function readinessGap(text, tone = "") {
  const node = element("span", tone);
  node.append(element("i"), document.createTextNode(text));
  return node;
}

function claimRow(item) {
  const row = element("article", `case-detail-item claim ${item.claim_status || "unverified"}`);
  const header = element("header");
  header.append(element("span", "", ({ context: "Контекст", material: "Существенное", critical: "Критическое" })[item.importance] || item.importance), element("small", "", ({ unverified: "Не проверено", supported: "Подтверждено", contradicted: "Есть противоречие", accepted: "Принято", rejected: "Отклонено" })[item.claim_status] || item.claim_status));
  row.append(header, element("p", "", item.statement));
  if (item.rationale) row.append(element("small", "case-item-note", item.rationale));
  if (item.claim_status === "unverified") {
    const footer = element("footer");
    footer.append(element("span", "", "Решение проверяющего"));
    const actions = element("div", "case-item-actions");
    const review = async (claimStatus, button) => {
      button.disabled = true;
      try {
        await api(`/api/atlas/cases/${appState.caseDetail.case.id}/claims/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify({ claim_status: claimStatus, expected_version: item.version }),
        });
        await refreshOpenCase();
        await loadCases();
      } catch (error) { showToast(error.message || "Оценка утверждения не сохранена.", true); }
      finally { button.disabled = false; }
    };
    const supported = element("button", "", "Подтверждено");
    const contradicted = element("button", "danger", "Противоречие");
    [supported, contradicted].forEach((button) => { button.type = "button"; });
    supported.addEventListener("click", () => void review("supported", supported));
    contradicted.addEventListener("click", () => void review("contradicted", contradicted));
    actions.append(supported, contradicted);
    footer.append(actions);
    row.append(footer);
  }
  return row;
}

function evidenceRow(item) {
  const row = element("article", `case-detail-item evidence ${item.verification_status || "pending"}`);
  const header = element("header");
  header.append(element("span", "", evidenceTypeLabels[item.source_type] || item.source_type), element("small", "", item.verification_status === "verified" ? "Проверено" : item.verification_status === "rejected" ? "Отклонено" : "Ждёт проверки"));
  row.append(header, element("h4", "", item.title), element("p", "", item.relevance || item.summary || "Описание связи пока не добавлено."));
  const meta = element("footer");
  meta.append(element("span", "", item.source_id ? `источник #${item.source_id}` : "зафиксировано вручную"), element("span", "", item.admissibility === "admissible" ? "допустимо" : item.admissibility === "excluded" ? "исключено" : "оценка не завершена"));
  if (item.verification_status === "pending") {
    const approve = element("button", "", "Подтвердить");
    approve.type = "button";
    approve.addEventListener("click", async () => {
      approve.disabled = true;
      try {
        await api(`/api/atlas/cases/${appState.caseDetail.case.id}/evidence/${item.id}`, { method: "PATCH", body: JSON.stringify({ verification_status: "verified", admissibility: "admissible", expected_version: item.version }) });
        await refreshOpenCase();
        await loadCases();
      } catch (error) { showToast(error.message || "Проверка не сохранена.", true); }
      finally { approve.disabled = false; }
    });
    meta.append(approve);
  }
  row.append(meta);
  return row;
}

function renderCaseDetail(detail) {
  appState.caseDetail = detail;
  const selected = detail.case || {};
  const readiness = caseReadiness(detail);
  const header = byId("case-detail-header");
  header.querySelector("span").textContent = `ДЕЛО ${String(selected.case_number || selected.id).padStart(3, "0")} · ${casePriorityLabels[selected.priority] || selected.priority}`;
  header.querySelector("h2").textContent = selected.title || "Дело Atlas";
  header.querySelector("p").textContent = selected.objective || "Цель проверки пока не описана.";
  header.querySelector(".case-readiness-orb strong").textContent = String(Number(readiness.score || 0));
  header.querySelector(".case-readiness-orb").style.setProperty("--case-score", `${Number(readiness.score || 0)}%`);
  const gaps = byId("case-readiness-gaps");
  clear(gaps);
  gaps.append(readinessGap(`${caseStatusLabels[selected.status] || selected.status} · версия ${selected.version}`, "identity"));
  if (!readiness.claims) gaps.append(readinessGap("Нет проверяемых утверждений", "warning"));
  if (readiness.critical_gap_ids?.length) gaps.append(readinessGap(`Критических пробелов: ${readiness.critical_gap_ids.length}`, "danger"));
  if (readiness.unsupported_claim_ids?.length) gaps.append(readinessGap(`Без подтверждения: ${readiness.unsupported_claim_ids.length}`, "warning"));
  if (readiness.pending_evidence_ids?.length) gaps.append(readinessGap(`Ждут проверки: ${readiness.pending_evidence_ids.length}`, "warning"));
  if (readiness.state === "ready") gaps.append(readinessGap("Материалы достаточны для следующего этапа", "success"));
  const claims = byId("case-claims");
  clear(claims); (detail.claims || []).forEach((item) => claims.append(claimRow(item)));
  if (!detail.claims?.length) claims.append(element("div", "empty", "Сформулируйте первое проверяемое утверждение."));
  const evidence = byId("case-evidence");
  clear(evidence); (detail.evidence || []).forEach((item) => evidence.append(evidenceRow(item)));
  if (!detail.evidence?.length) evidence.append(element("div", "empty", "Привяжите первоисточник или зафиксируйте свидетельство."));
  const claimSelect = byId("case-evidence-claim");
  clear(claimSelect); claimSelect.append(new Option("К делу в целом", ""));
  (detail.claims || []).forEach((item) => claimSelect.append(new Option(item.statement.slice(0, 80), String(item.id))));
}

async function refreshOpenCase() {
  const caseId = Number(appState.caseDetail?.case?.id || 0);
  if (!caseId) return;
  renderCaseDetail(await api(`/api/atlas/cases/${caseId}`, { timeout: 15000 }));
}

async function openCaseDetail(caseId) {
  const dialog = byId("case-detail-dialog");
  openDialog(dialog);
  byId("case-detail-header").querySelector("h2").textContent = "Загрузка дела…";
  try { renderCaseDetail(await api(`/api/atlas/cases/${Number(caseId)}`, { timeout: 15000 })); }
  catch (error) { closeDialog(dialog); showToast(error.message || "Дело не открылось.", true); }
}

function openCaseItem(mode) {
  appState.caseItemMode = mode;
  const claimMode = mode === "claim";
  const form = byId("case-item-form");
  form.reset();
  byId("case-claim-fields").hidden = !claimMode;
  byId("case-evidence-fields").hidden = claimMode;
  form.elements.statement.required = claimMode;
  form.elements.evidence_title.required = !claimMode;
  byId("case-item-kicker").textContent = claimMode ? "ATLAS CASE · НОВОЕ ПОЛОЖЕНИЕ" : "ATLAS EVIDENCE · ПЕРВОИСТОЧНИК";
  byId("case-item-title").textContent = claimMode ? "Добавить утверждение" : "Привязать доказательство";
  byId("case-item-description").textContent = claimMode ? "Сформулируйте одно положение, которое можно подтвердить или опровергнуть." : "Укажите существующий объект Atlas, HTTPS-ссылку или сохраните самостоятельную заметку.";
  openDialog(byId("case-item-dialog"));
}

async function followAtlasJob(job) {
  const jobId = Number(job?.id || 0);
  if (!jobId || appState.jobWatchers.has(jobId)) return;
  appState.jobWatchers.add(jobId);
  try {
    for (let attempt = 0; attempt < 90; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, attempt < 8 ? 750 : 2000));
      const result = await api(`/api/atlas/jobs/${jobId}`, { timeout: 10000 });
      const current = result.job || {};
      if (["pending", "running", "retry"].includes(current.status)) continue;
      await loadKnowledgeSources();
      if (current.status === "failed") {
        showToast("Atlas сохранил материал, но поиск пока недоступен. Задача будет видна администраторам.", true);
      }
      return;
    }
  } catch (_error) {
    // The source remains durable on the server; a temporary browser outage is harmless.
  } finally {
    appState.jobWatchers.delete(jobId);
  }
}

async function loadForumSync() {
  const result = await api("/api/atlas/forum-sync");
  appState.data.forum_sync = result.status || {};
  renderForumSync(appState.data.forum_sync);
  return appState.data.forum_sync;
}

const searchKindLabels = { case: "ДЕЛО", document: "ДОКУМЕНТ", timeline_event: "СОБЫТИЕ", media_asset: "МЕДИА", knowledge_source: "ЗНАНИЯ" };

function openGlobalSearch() {
  openDialog(byId("atlas-search-dialog"));
  requestAnimationFrame(() => byId("atlas-search-query").focus());
}

function renderGlobalSearch(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const root = byId("atlas-search-results");
  clear(root);
  items.forEach((item) => {
    const result = element("button", "atlas-search-result");
    result.type = "button";
    const mark = element("i", "", ({ case: "▣", document: "◇", timeline_event: "◌", media_asset: "▶", knowledge_source: "§" })[item.kind] || "·");
    const copy = element("span");
    copy.append(element("small", "", `${searchKindLabels[item.kind] || item.kind} · ${item.eyebrow || "ATLAS"}`), element("b", "", item.title), element("p", "", item.snippet || "Открыть объект Atlas"));
    result.append(mark, copy, element("em", "", "→"));
    result.addEventListener("click", async () => {
      closeDialog(byId("atlas-search-dialog"));
      switchScreen(item.screen || "home");
      if (item.kind === "case") await openCaseDetail(item.id);
      else if (item.kind === "document") await openDocumentDetail(item.id);
    });
    root.append(result);
  });
  if (!items.length) root.append(element("div", "empty", payload?.query ? "Совпадений в доступном пространстве нет." : "Введите запрос для поиска."));
  const groups = Object.entries(payload?.counts || {}).map(([kind, count]) => `${searchKindLabels[kind] || kind}: ${count}`);
  byId("atlas-search-summary").textContent = items.length ? `Найдено ${payload.total || items.length} · ${groups.join(" · ")}` : "Atlas проверил все доступные разделы.";
}

async function runGlobalSearch(query) {
  const value = String(query || "").trim();
  if (value.length < 2) { renderGlobalSearch({ items: [], query: "" }); return; }
  byId("atlas-search-summary").textContent = "Собираем результаты из модулей Atlas…";
  const params = new URLSearchParams({ q: value, server_code: appState.serverCode, faction_code: appState.factionCode });
  renderGlobalSearch(await api(`/api/atlas/search?${params}`, { timeout: 15000 }));
}

async function updateScope() {
  appState.serverCode = byId("atlas-server").value || "phoenix-15";
  appState.factionCode = byId("atlas-faction").value || "lspd";
  byId("onboarding-server").value = appState.serverCode;
  byId("onboarding-faction").value = appState.factionCode;
  byId("vault-scope").textContent = scopeLabel();
  byId("source-library-title").textContent = scopeLabel();
  renderKnowledgeSources(appState.data?.knowledge_sources || []);
  try {
    const membership = appState.data?.membership || {};
    await Promise.all([
      loadKnowledgeSources(),
      api("/api/atlas/onboarding", {
        method: "POST",
        body: JSON.stringify({
          step: Number(membership.onboarding_step || 0),
          profile: { ...(membership.profile || {}), server_code: appState.serverCode, faction_code: appState.factionCode },
        }),
      }),
    ]);
  } catch (error) {
    showToast(error.message || "Не удалось сменить раздел.", true);
  }
}

function bind() {
  byId("atlas-space").addEventListener("change", async (event) => {
    appState.organizationId = Number(event.currentTarget.value) || null;
    appState.threadId = null;
    appState.threadReady = false;
    try {
      if (appState.organizationId) localStorage.setItem("tmod-atlas-space", String(appState.organizationId));
    } catch (_error) { /* selection still works for the current tab */ }
    try { await reload(); showToast("Рабочее пространство переключено."); }
    catch (error) { showToast(error.message || "Не удалось открыть пространство.", true); }
  });
  bindWorkspaceMotion();
  byId("atlas-server").addEventListener("change", () => void updateScope());
  byId("atlas-faction").addEventListener("change", () => void updateScope());
  byId("atlas-model").addEventListener("change", (event) => {
    if (appState.busy) {
      event.currentTarget.value = appState.modelId;
      showToast("Дождитесь ответа Atlas перед сменой агента.");
      return;
    }
    appState.modelId = event.currentTarget.value || "atlas-tvr-a";
    const label = event.currentTarget.selectedOptions[0]?.textContent || appState.modelId;
    const agent = atlasAgent(appState.modelId);
    byId("context-model").textContent = agent.name || label;
    byId("context-collection").textContent = agent.description || "Отдельный рабочий контур";
    if (appState.threadId) newChat();
    showToast(`Агент: ${label}. Создан отдельный диалог и контур памяти.`);
  });
  document.querySelectorAll(".atlas-nav [data-screen]").forEach((button) => {
    button.addEventListener("click", () => switchScreen(button.dataset.screen));
  });
  document.querySelectorAll("[data-jump]").forEach((node) => {
    node.addEventListener("click", () => switchScreen(node.dataset.jump));
  });
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.addEventListener("click", () => closeDialog(button.closest("dialog")));
  });
  [byId("onboarding-dialog"), byId("document-dialog"), byId("document-detail-dialog"), byId("memory-event-dialog"), byId("case-dialog"), byId("case-detail-dialog"), byId("case-item-dialog"), byId("atlas-search-dialog")].forEach((dialog) => {
    dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(dialog); });
  });
  byId("continue-onboarding").addEventListener("click", () => openDialog(byId("onboarding-dialog")));
  document.querySelectorAll("#onboarding-steps [data-step]").forEach((button) => {
    button.addEventListener("click", () => openDialog(byId("onboarding-dialog")));
  });
  byId("onboarding-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    try {
      appState.serverCode = values.server_code || appState.serverCode;
      appState.factionCode = values.faction_code || appState.factionCode;
      byId("atlas-server").value = appState.serverCode;
      byId("atlas-faction").value = appState.factionCode;
      await api("/api/atlas/onboarding", { method: "POST", body: JSON.stringify({ step: 4, profile: values }) });
      closeDialog(byId("onboarding-dialog"));
      await reload();
      showToast("Рабочий профиль Atlas настроен.");
    } catch (error) { showToast(error.message, true); }
  });
  byId("atlas-chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const question = byId("atlas-question").value.trim();
    if (question) void sendQuestion(question);
  });
  const composer = byId("atlas-question");
  const resizeComposer = () => {
    composer.style.height = "auto";
    composer.style.height = `${Math.min(composer.scrollHeight, 190)}px`;
  };
  composer.addEventListener("input", resizeComposer);
  composer.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      byId("atlas-chat-form").requestSubmit();
    }
  });
  byId("new-atlas-chat").addEventListener("click", newChat);
  byId("chat-focus-toggle").addEventListener("click", () => {
    setChatFocus(!appState.chatFocus);
  });
  byId("chat-history-toggle").addEventListener("click", toggleChatHistory);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && appState.chatFocus && !document.querySelector("dialog[open]")) {
      setChatFocus(false);
    }
  });
  document.querySelectorAll("[data-response-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      if (appState.busy) {
        showToast("Режим можно сменить после завершения текущего ответа.");
        return;
      }
      appState.responseMode = button.dataset.responseMode || "balanced";
      document.querySelectorAll("[data-response-mode]").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
    });
  });
  byId("new-document").addEventListener("click", () => openDocumentDialog());
  byId("new-memory-event").addEventListener("click", openMemoryDialog);
  document.querySelectorAll("[data-create-memory]").forEach((node) => node.addEventListener("click", openMemoryDialog));
  byId("memory-event-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const selectedTime = values.occurred_at ? new Date(String(values.occurred_at)) : null;
    try {
      await api("/api/atlas/timeline", {
        method: "POST",
        body: JSON.stringify({
          ...values,
          occurred_at: selectedTime && !Number.isNaN(selectedTime.getTime()) ? selectedTime.toISOString() : null,
        }),
      });
      closeDialog(byId("memory-event-dialog"));
      form.reset();
      await reload();
      switchScreen("memory");
      showToast("Событие сохранено в Atlas Memory.");
    } catch (error) { showToast(error.message || "Событие не сохранено.", true); }
  });
  byId("media-upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const file = byId("media-file").files?.[0];
    if (!file) { showToast("Выберите файл для медиасети.", true); return; }
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    paintMediaProgress(1, "Подготавливаем защищённую загрузку");
    try {
      const result = await uploadMediaFile(file, values);
      form.reset();
      await loadMediaLibrary();
      if (result?.job) void followMediaJob(result.job);
      else showToast("Файл передан Atlas. Обработка продолжится в фоне.");
    } catch (error) {
      byId("media-upload-progress").hidden = true;
      showToast(error.message || "Материал не загружен.", true);
    } finally {
      button.disabled = false;
    }
  });
  byId("refresh-media").addEventListener("click", async () => {
    try { await loadMediaLibrary(); showToast("Медиатека обновлена."); }
    catch (error) { showToast(error.message || "Не удалось обновить медиатеку.", true); }
  });
  byId("new-case").addEventListener("click", () => openDialog(byId("case-dialog")));
  byId("case-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    try {
      const result = await api("/api/atlas/cases", { method: "POST", body: JSON.stringify(values) });
      closeDialog(byId("case-dialog"));
      form.reset();
      await loadCases();
      await openCaseDetail(result.case.id);
      showToast("Дело открыто. Теперь добавьте проверяемые утверждения.");
    } catch (error) { showToast(error.message || "Дело не создано.", true); }
    finally { button.disabled = false; }
  });
  byId("refresh-cases").addEventListener("click", async () => {
    try { await loadCases(); showToast("Реестр дел обновлён."); }
    catch (error) { showToast(error.message || "Реестр не обновился.", true); }
  });
  byId("case-status-filter").addEventListener("change", () => void loadCases().catch((error) => showToast(error.message, true)));
  byId("case-add-claim").addEventListener("click", () => openCaseItem("claim"));
  byId("case-add-evidence").addEventListener("click", () => openCaseItem("evidence"));
  byId("case-item-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const caseId = Number(appState.caseDetail?.case?.id || 0);
    if (!caseId) { showToast("Сначала откройте дело.", true); return; }
    const button = byId("case-item-submit");
    button.disabled = true;
    try {
      if (appState.caseItemMode === "claim") {
        await api(`/api/atlas/cases/${caseId}/claims`, { method: "POST", body: JSON.stringify({ statement: values.statement, importance: values.importance }) });
        showToast("Утверждение добавлено в проверку.");
      } else {
        await api(`/api/atlas/cases/${caseId}/evidence`, {
          method: "POST",
          body: JSON.stringify({
            source_type: values.source_type,
            source_id: values.source_type === "note" ? null : values.source_id,
            title: values.evidence_title,
            claim_id: values.claim_id || null,
            relevance: values.relevance || "",
          }),
        });
        showToast("Доказательство привязано с сохранением происхождения.");
      }
      closeDialog(byId("case-item-dialog"));
      await refreshOpenCase();
      await loadCases();
    } catch (error) { showToast(error.message || "Изменение не сохранено.", true); }
    finally { button.disabled = false; }
  });
  byId("document-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    try {
      const result = await api("/api/atlas/documents", {
        method: "POST",
        body: JSON.stringify({ title: values.title, template_id: values.template_id || null, fields: values.template_id ? collectDocumentFields(form) : { content: values.content || "" }, rendered_text: values.content || "" }),
      });
      closeDialog(byId("document-dialog"));
      form.reset();
      await reload();
      await openDocumentDetail(result.document.id);
      showToast("Черновик документа создан.");
    } catch (error) { showToast(error.message, true); }
  });
  byId("document-edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const selected = appState.documentDetail?.document;
    if (!selected) return;
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    try {
      await api(`/api/atlas/documents/${selected.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          operation: "revise",
          title: values.title,
          fields: appState.documentDetail?.template ? collectDocumentFields(form) : { ...(selected.fields || {}), content: values.rendered_text || "" },
          rendered_text: values.rendered_text || "",
          change_summary: values.change_summary || "Обновлён документ",
          expected_revision: selected.revision,
        }),
      });
      await refreshOpenDocument();
      await reload();
      showToast("Новая редакция сохранена; предыдущая осталась в истории.");
    } catch (error) { showToast(error.message || "Редакция не сохранена.", true); }
    finally { button.disabled = false; }
  });
  byId("document-next-state").addEventListener("click", async (event) => {
    const selected = appState.documentDetail?.document;
    const status = event.currentTarget.dataset.status;
    if (!selected || !status) return;
    event.currentTarget.disabled = true;
    try {
      await api(`/api/atlas/documents/${selected.id}`, { method: "PATCH", body: JSON.stringify({ operation: "transition", status, expected_revision: selected.revision }) });
      await refreshOpenDocument();
      await reload();
      showToast(`Документ: ${documentStatusLabels[status] || status}.`);
    } catch (error) { showToast(error.message || "Переход не выполнен.", true); }
    finally { event.currentTarget.disabled = false; }
  });
  byId("document-comment-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const selected = appState.documentDetail?.document;
    if (!selected) return;
    const values = Object.fromEntries(new FormData(form));
    try {
      await api(`/api/atlas/documents/${selected.id}/comments`, { method: "POST", body: JSON.stringify({ body: values.body, revision: selected.revision }) });
      form.reset();
      await refreshOpenDocument();
      showToast("Комментарий привязан к текущей редакции.");
    } catch (error) { showToast(error.message || "Комментарий не сохранён.", true); }
  });
  byId("document-approval-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const selected = appState.documentDetail?.document;
    if (!selected) return;
    const steps = [...byId("document-route-builder").querySelectorAll("[data-route-step]")].map((row) => ({
      title: row.querySelector("[name='route_title']").value,
      assigned_user_id: row.querySelector("[name='route_user']").value || null,
      required_role: row.querySelector("[name='route_role']").value || null,
    }));
    try {
      await api(`/api/atlas/documents/${selected.id}/approvals`, { method: "PUT", body: JSON.stringify({ steps }) });
      await refreshOpenDocument();
      showToast("Маршрут согласования назначен.");
    } catch (error) { showToast(error.message || "Маршрут не сохранён.", true); }
  });
  byId("document-route-add").addEventListener("click", () => {
    const builder = byId("document-route-builder");
    if (builder.children.length >= 12) return showToast("В маршруте может быть не больше 12 этапов.", true);
    builder.append(approvalRouteRow({}, builder.children.length + 1));
  });
  byId("document-template").addEventListener("change", (event) => {
    const template = documentTemplateById(event.currentTarget.value);
    appState.selectedTemplate = template;
    renderStructuredDocumentFields(byId("document-template-fields"), template);
    byId("document-freeform-label").hidden = Boolean(template);
  });
  document.querySelectorAll("[data-document-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      const tab = button.dataset.documentTab;
      document.querySelectorAll("[data-document-tab]").forEach((item) => item.classList.toggle("active", item === button));
      byId("document-approval-pane").hidden = tab !== "approval";
      byId("document-comments-pane").hidden = tab !== "comments";
      byId("document-revisions-pane").hidden = tab !== "revisions";
    });
  });
  byId("knowledge-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "Индексируем…";
    try {
      const result = await api("/api/atlas/knowledge", {
        method: "POST",
        body: JSON.stringify({ ...values, server_code: appState.serverCode, faction_code: appState.factionCode }),
        timeout: 15000,
      });
      form.reset();
      await loadKnowledgeSources();
      void followAtlasJob(result.job);
      showToast(result.message || "Источник принят и индексируется в фоне.");
    } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; button.textContent = "Добавить в библиотеку →"; }
  });
  byId("knowledge-upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    values.set("server_code", appState.serverCode);
    values.set("faction_code", appState.factionCode);
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "Загружаем…";
    try {
      const result = await api("/api/atlas/knowledge/upload", { method: "POST", body: values, timeout: 30000 });
      form.reset();
      await loadKnowledgeSources();
      void followAtlasJob(result.job);
      showToast(result.message || "Файл принят в библиотеку.");
    } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; button.textContent = "Загрузить в библиотеку →"; }
  });
  byId("knowledge-forum-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    button.textContent = "Читаем форум…";
    try {
      const result = await api("/api/atlas/knowledge/import-forum", {
        method: "POST",
        body: JSON.stringify({ ...values, server_code: appState.serverCode, faction_code: appState.factionCode }),
        timeout: 90000,
      });
      form.reset();
      if (result.browser_url) byId("forum-browser-link").href = result.browser_url;
      await loadKnowledgeSources();
      void followAtlasJob(result.job);
      showToast(result.message || "Форум принят на индексирование.");
    } catch (error) {
      if (error.payload?.browser_url) byId("forum-browser-link").href = error.payload.browser_url;
      showToast(error.message, true);
    }
    finally { button.disabled = false; button.textContent = "Прочитать и проиндексировать →"; }
  });
  document.querySelectorAll("[data-knowledge-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.knowledgeMode;
      document.querySelectorAll("[data-knowledge-mode]").forEach((item) => item.classList.toggle("active", item === button));
      byId("knowledge-upload-form").hidden = mode !== "file";
      byId("knowledge-forum-form").hidden = mode !== "forum";
      byId("knowledge-form").hidden = mode !== "text";
    });
  });
  byId("refresh-sources").addEventListener("click", async () => {
    try { await loadKnowledgeSources(); showToast("Состояние библиотеки обновлено."); }
    catch (error) { showToast(error.message, true); }
  });
  byId("forum-sync-now").addEventListener("click", async () => {
    const button = byId("forum-sync-now");
    button.disabled = true;
    try {
      await api("/api/atlas/forum-sync", { method: "POST", body: "{}" });
      renderForumSync({ ...(appState.data?.forum_sync || {}), status: "running" });
      showToast("Atlas начал безопасную сверку форума.");
      setTimeout(() => void loadForumSync().catch(() => {}), 1800);
    } catch (error) {
      showToast(error.message || "Не удалось запустить сверку.", true);
      renderForumSync(appState.data?.forum_sync || {});
    }
  });
  byId("global-search").addEventListener("click", openGlobalSearch);
  byId("atlas-search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void runGlobalSearch(byId("atlas-search-query").value).catch((error) => showToast(error.message || "Поиск временно недоступен.", true));
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      openGlobalSearch();
    }
  });
  window.addEventListener("hashchange", () => switchScreen(location.hash.replace(/^#\//, ""), false));
}

async function bootstrap() {
  bind();
  try {
    await reload();
    byId("loading").hidden = true;
    if (appState.data?.preview) {
      if (appState.data?.viewer?.account_tier === "zero") {
        const link = byId("preview-return");
        link.href = "/logout";
        link.querySelector("span").textContent = "T-Mod Account";
        link.querySelector("b").textContent = "Сменить аккаунт";
      }
      byId("atlas-preview").hidden = false;
      bindPreviewMotion();
      return;
    }
    byId("atlas-app").hidden = false;
    requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add("atlas-ready")));
    switchScreen(location.hash.replace(/^#\//, "") || "home", false);
    if (Number(appState.data?.membership?.onboarding_step || 0) < 4 && !appState.onboardingPrompted) {
      appState.onboardingPrompted = true;
      setTimeout(() => openDialog(byId("onboarding-dialog")), 380);
    }
    setInterval(() => {
      if (!document.hidden && appState.screen === "forum") void loadForumSync().catch(() => {});
    }, 30000);
  } catch (error) {
    if (!String(error.message).includes("Требуется вход")) {
      byId("loading").querySelector("span").textContent = "Atlas временно недоступен";
      byId("loading").querySelector("small").textContent = error.message || "проверьте контейнер T-Mod";
    }
  }
}

void bootstrap();
