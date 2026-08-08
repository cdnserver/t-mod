"use strict";

const byId = (id) => document.getElementById(id);
function storedAtlasSpace() {
  try { return Number(localStorage.getItem("tmod-atlas-space")) || null; }
  catch (_error) { return null; }
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
};
const screenMeta = {
  home: ["ATLAS", "Командный центр"],
  ai: ["УМНЫЙ ПОМОЩНИК", "Atlas AI"],
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
    if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
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

function switchScreen(screen, updateHash = true) {
  const selected = screenMeta[screen] ? screen : "home";
  const changed = appState.screen !== selected;
  const applyScreen = () => {
    appState.screen = selected;
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
  return card;
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
  copy.append(
    element("b", "", thread.title || "Новый диалог"),
    element("small", "", thread.preview || "Диалог без сообщений"),
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
  message.textContent = status.last_error
    ? String(status.last_error)
    : "Последняя подтверждённая версия всегда остаётся доступной Atlas AI.";
  const button = byId("forum-sync-now");
  button.disabled = state === "running" || state === "disabled";
  button.firstChild.textContent = state === "running" ? "Проверка уже выполняется " : "Проверить обновления сейчас ";
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

  const knowledgeEditor = byId("knowledge-editor");
  knowledgeEditor.classList.toggle("locked", !viewer.administrator);
  if (!viewer.administrator) {
    knowledgeEditor.querySelector("h3").textContent = "Добавление источников доступно администратору";
  }
}

function messageNode(role, text, citations = []) {
  const row = element("div", role === "user" ? "user-message" : "assistant-message");
  if (role !== "user") row.append(element("span", "", "A"));
  const copy = element("div");
  if (role !== "user") copy.append(element("small", "", "ATLAS · ОТВЕТ С КОНТЕКСТОМ"));
  const richText = element("div", "rich-text");
  renderRichText(richText, text);
  copy.append(richText);
  appendCitations(copy, citations);
  row.append(copy);
  return row;
}

function updateResearchProgress(copy, event) {
  let panel = copy.querySelector(".research-progress");
  if (!panel) {
    panel = element("section", "research-progress");
    panel.append(element("header", "", "АРИСТОТЕЛЬ · ПЛАН ИССЛЕДОВАНИЯ"), element("ol", "research-steps"), element("footer", "", "Формируем задачи…"));
    copy.insertBefore(panel, copy.querySelector(".rich-text"));
  }
  if (event.phase === "plan") {
    const list = panel.querySelector("ol");
    clear(list);
    (event.steps || []).forEach((step) => {
      const item = element("li", "pending");
      item.dataset.stepId = step.id;
      item.append(element("i", "", "○"), element("span", "", `${step.agent} · ${step.title}`));
      list.append(item);
    });
    panel.querySelector("footer").textContent = "Задачи распределены по исследовательским контурам";
  } else if (event.phase === "stage") {
    const item = [...panel.querySelectorAll("[data-step-id]")].find(
      (candidate) => candidate.dataset.stepId === String(event.step_id || ""),
    );
    if (item) {
      item.className = event.status || "pending";
      item.querySelector("i").textContent = event.status === "complete" ? "✓" : "●";
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
      const node = citation.url ? element("a", "", `[${citation.index}] ${citation.title}`) : element("span", "", `[${citation.index}] ${citation.title}`);
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
  openDialog(byId("document-dialog"));
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
  byId("current-thread-title").textContent = result.thread.title || "Диалог";
  const stream = byId("chat-stream");
  clear(stream);
  (result.messages || []).forEach((message) => {
    stream.append(messageNode(message.role, message.content_text, message.citations || []));
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

async function loadForumSync() {
  const result = await api("/api/atlas/forum-sync");
  appState.data.forum_sync = result.status || {};
  renderForumSync(appState.data.forum_sync);
  return appState.data.forum_sync;
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
    appState.modelId = event.currentTarget.value || "atlas-tvr-a";
    showToast(`Модель: ${appState.modelId}`);
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
  [byId("onboarding-dialog"), byId("document-dialog")].forEach((dialog) => {
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
  byId("document-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    try {
      await api("/api/atlas/documents", {
        method: "POST",
        body: JSON.stringify({ title: values.title, template_id: values.template_id || null, fields: { content: values.content || "" }, rendered_text: values.content || "" }),
      });
      closeDialog(byId("document-dialog"));
      form.reset();
      await reload();
      showToast("Черновик документа создан.");
    } catch (error) { showToast(error.message, true); }
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
    button.textContent = "Читаем тему…";
    try {
      const result = await api("/api/atlas/knowledge/import-forum", {
        method: "POST",
        body: JSON.stringify({ ...values, server_code: appState.serverCode, faction_code: appState.factionCode }),
        timeout: 90000,
      });
      form.reset();
      await loadKnowledgeSources();
      showToast(result.message || "Тема добавлена в библиотеку.");
    } catch (error) { showToast(error.message, true); }
    finally { button.disabled = false; button.textContent = "Прочитать и добавить →"; }
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
  byId("global-search").addEventListener("click", () => {
    switchScreen("ai");
    byId("atlas-question").focus();
    showToast("Глобальный поиск будет расширен; сейчас используйте Atlas AI.");
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
