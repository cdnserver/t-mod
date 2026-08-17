"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = {
    csrf: "",
    cases: [],
    detail: null,
    filter: "active",
    activeTab: "overview",
    busy: false,
    confirmAction: null,
  };

  const labels = {
    status: {
      new: "Входящий сигнал",
      screening: "Сбор сведений",
      needs_info: "Ожидаются сведения",
      analysis: "Аналитика",
      decision: "Подготовка решения",
      approved: "Допуск одобрен",
      denied: "В допуске отказано",
      archived: "Архив",
    },
    risk: {
      unrated: "Риск не определён",
      low: "Низкий риск",
      medium: "Средний риск",
      high: "Высокий риск",
      critical: "Критический риск",
    },
    priority: { normal: "Обычный", important: "Важный", urgent: "Срочный", critical: "Критический" },
    classification: { restricted: "Ограничено", secret: "Секретно", top_secret: "Особой важности" },
    kind: {
      admission: "Проверка при вступлении",
      background: "Фоновая проверка",
      incident: "Инцидент",
      internal: "Внутренняя проверка",
      other: "Иное расследование",
    },
    materialKind: { document: "Документ", link: "Ссылка", testimony: "Свидетельство", observation: "Наблюдение", media: "Медиа", other: "Иное" },
    reliability: { unrated: "Не оценено", low: "Низкая", medium: "Средняя", high: "Высокая", confirmed: "Подтверждено" },
    materialStatus: { new: "Новый", verified: "Проверен", rejected: "Отклонён" },
    taskStatus: { todo: "К выполнению", doing: "В работе", blocked: "Заблокирована", done: "Выполнена" },
    event: {
      created: "Расследование зарегистрировано",
      claim: "Расследование принято в работу",
      note: "Добавлена служебная запись",
      update: "Аналитическая карточка обновлена",
      needs_info: "Запрошены дополнительные сведения",
      analysis: "Начат аналитический этап",
      decision: "Материалы подготовлены к решению",
      approve: "Допуск одобрен",
      deny: "В допуске отказано",
      reopen: "Расследование открыто повторно",
      archive: "Расследование перенесено в архив",
      material_added: "Добавлен материал",
      material_status: "Изменён статус материала",
      relation_added: "Добавлена связь",
      task_added: "Поставлена задача",
      task_status: "Изменён статус задачи",
    },
  };

  const lanes = [
    ["incoming", "ВХОДЯЩИЕ", "Новые сигналы"],
    ["collection", "СБОР СВЕДЕНИЙ", "Проверка и дополнение"],
    ["analysis", "АНАЛИТИКА", "Версии и выводы"],
    ["decision", "РЕШЕНИЕ", "Готово к вердикту"],
    ["closed", "ЗАВЕРШЕНО", "Решения и архив"],
  ];

  const closedStatuses = new Set(["approved", "denied", "archived"]);
  const node = (tag, className = "", text = "") => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== "") element.textContent = String(text);
    return element;
  };
  const textOr = (value, fallback = "Не указано") => String(value || "").trim() || fallback;
  const caseNumber = (item) => `ОВР-${String(item?.case_number || 0).padStart(3, "0")}`;
  const fullName = (item) => `${item?.first_name || ""} ${item?.last_name || ""}`.trim() || "Без имени";

  function timeoutSignal(ms) {
    if (globalThis.AbortSignal?.timeout) return AbortSignal.timeout(ms);
    const controller = new AbortController();
    window.setTimeout(() => controller.abort(), ms);
    return controller.signal;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      signal: options.signal || timeoutSignal(options.method === "POST" ? 30000 : 15000),
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json", "X-CSRF-Token": state.csrf } : {}),
        ...(options.headers || {}),
      },
    });
    let data = {};
    try { data = await response.json(); } catch { data = {}; }
    if (!response.ok) {
      const error = new Error(data.message || data.error || `Ошибка HTTP ${response.status}`);
      error.status = response.status;
      error.code = data.error || "request_failed";
      error.payload = data;
      throw error;
    }
    return data;
  }

  function toast(message, kind = "success") {
    const item = byId("ovr-toast");
    item.textContent = String(message);
    item.dataset.kind = kind;
    item.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => { item.hidden = true; }, 4800);
  }

  function setBusy(active, copy = "Сохраняем изменения") {
    state.busy = active;
    const overlay = byId("ovr-busy");
    overlay.hidden = !active;
    const title = overlay.querySelector("strong");
    if (title) title.textContent = copy;
  }

  function openDialog(id) {
    const dialog = byId(id);
    if (!dialog.open) dialog.showModal();
  }

  function closeDialog(id) {
    const dialog = byId(id);
    if (dialog?.open) dialog.close();
  }

  function formatMoment(value, withYear = false) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("ru-RU", {
      day: "2-digit",
      month: "short",
      ...(withYear ? { year: "numeric" } : {}),
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function deadlineMeta(item) {
    if (closedStatuses.has(item.status)) return { label: `Закрыто ${formatMoment(item.decided_at || item.updated_at)}`, tone: "closed" };
    const time = new Date(item.due_at).getTime();
    if (!Number.isFinite(time)) return { label: "Срок не указан", tone: "" };
    const hours = (time - Date.now()) / 3600000;
    if (hours < 0) return { label: `Просрочено на ${Math.max(1, Math.ceil(-hours))} ч`, tone: "overdue" };
    if (hours < 12) return { label: `Осталось ${Math.max(1, Math.ceil(hours))} ч`, tone: "soon" };
    return { label: `До ${formatMoment(item.due_at)}`, tone: "" };
  }

  function laneFor(item) {
    if (item.status === "new") return "incoming";
    if (["screening", "needs_info"].includes(item.status)) return "collection";
    if (item.status === "analysis") return "analysis";
    if (item.status === "decision") return "decision";
    return "closed";
  }

  function mergeCase(item) {
    if (!item?.id) return;
    const index = state.cases.findIndex((entry) => Number(entry.id) === Number(item.id));
    if (index >= 0) state.cases[index] = { ...state.cases[index], ...item };
    else state.cases.unshift(item);
  }

  function applyDetail(detail) {
    if (!detail?.case) return;
    state.detail = detail;
    mergeCase(detail.case);
    renderBoard();
    renderCase();
    stampSync();
  }

  function stampSync() {
    byId("ovr-sync").textContent = `Актуально на ${new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
  }

  function visibleCases() {
    const query = byId("ovr-search").value.trim().toLowerCase();
    return state.cases.filter((item) => {
      const isClosed = closedStatuses.has(item.status);
      if (state.filter === "active" && isClosed) return false;
      if (state.filter === "closed" && !isClosed) return false;
      if (!query) return true;
      return [item.case_number, item.first_name, item.last_name, item.static_id, item.discord_text, item.assigned_to_display, item.case_kind]
        .some((value) => String(value || "").toLowerCase().includes(query));
    });
  }

  function renderMetrics() {
    const active = state.cases.filter((item) => !closedStatuses.has(item.status));
    byId("metric-active").textContent = String(active.length);
    byId("metric-analysis").textContent = String(active.filter((item) => ["analysis", "decision"].includes(item.status)).length);
    byId("metric-due").textContent = String(active.filter((item) => {
      const hours = (new Date(item.due_at).getTime() - Date.now()) / 3600000;
      return Number.isFinite(hours) && hours < 12;
    }).length);
    byId("metric-closed").textContent = String(state.cases.length - active.length);
  }

  function pill(copy, tone = "") {
    const item = node("span", `intel-pill ${tone}`.trim(), copy);
    if (tone) item.dataset.tone = tone;
    return item;
  }

  function caseCard(item) {
    const card = node("button", "ovr-card");
    card.type = "button";
    card.dataset.risk = item.risk_level || "unrated";
    card.dataset.priority = item.priority || "normal";

    const top = node("div", "card-top");
    top.append(node("span", "case-code", caseNumber(item)), pill(labels.status[item.status] || item.status, item.status));
    const identity = node("div", "card-identity");
    const monogram = `${String(item.first_name || "?")[0]}${String(item.last_name || "?")[0]}`.toUpperCase();
    identity.append(node("span", "card-monogram", monogram));
    const identityCopy = node("div");
    identityCopy.append(node("h3", "", fullName(item)), node("p", "", `${labels.kind[item.case_kind] || "Расследование"} · статик ${item.static_id}`));
    identity.append(identityCopy);

    const counters = node("div", "card-counters");
    counters.append(
      node("span", "", `Материалы ${item.material_count || 0}`),
      node("span", "", `Связи ${item.relation_count || 0}`),
      node("span", "", `Задачи ${item.task_done_count || 0}/${item.task_count || 0}`),
    );
    const progress = node("div", "card-progress");
    const bar = node("i");
    bar.style.width = `${Math.max(0, Math.min(100, Number(item.progress || 0)))}%`;
    progress.append(bar);
    const footer = node("footer");
    const deadline = deadlineMeta(item);
    footer.append(node("span", "", item.assigned_to_display || "Сотрудник не назначен"), node("span", `deadline ${deadline.tone}`, deadline.label));
    card.append(top, identity, counters, progress, footer);
    card.addEventListener("click", () => void openCase(item.id));
    return card;
  }

  function renderBoard() {
    const items = visibleCases();
    byId("ovr-board").replaceChildren(...lanes.map(([laneId, title, subtitle]) => {
      const lane = node("section", "case-lane");
      lane.dataset.lane = laneId;
      const selected = items.filter((item) => laneFor(item) === laneId);
      const heading = node("header");
      const copy = node("div");
      copy.append(node("strong", "", title), node("small", "", subtitle));
      heading.append(copy, node("b", "", selected.length));
      const list = node("div", "case-list");
      if (selected.length) list.append(...selected.map(caseCard));
      else list.append(node("div", "lane-empty", "В этой группе пока нет расследований."));
      lane.append(heading, list);
      return lane;
    }));
    renderMetrics();
  }

  function fact(label, value, href = "") {
    const item = node("div", "fact");
    item.append(node("small", "", label));
    if (href) {
      const link = node("a", "", value || "Открыть источник");
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.append(link);
    } else item.append(node("strong", "", textOr(value, "—")));
    return item;
  }

  function emptyState(title, copy) {
    const item = node("div", "intel-empty");
    item.append(node("i", "", "＋"), node("strong", "", title), node("p", "", copy));
    return item;
  }

  function actionButton(label, action, tone = "secondary", noteRequired = false) {
    const button = node("button", `button button-${tone}`, label);
    button.type = "button";
    button.dataset.action = action;
    button.addEventListener("click", () => {
      if (noteRequired) showConfirmation(action);
      else void mutateCase(action, {}, successMessage(action));
    });
    return button;
  }

  function successMessage(action) {
    return {
      claim: "Расследование принято в работу.",
      note: "Служебная запись добавлена.",
      update: "Аналитическая карточка сохранена.",
      needs_info: "Запрос дополнительных сведений зафиксирован.",
      analysis: "Расследование переведено на аналитический этап.",
      decision: "Материалы переданы на решение.",
      approve: "Решение о допуске сохранено.",
      deny: "Решение об отказе сохранено.",
      reopen: "Расследование открыто повторно.",
      archive: "Расследование перенесено в архив.",
    }[action] || "Изменения сохранены.";
  }

  function showConfirmation(action) {
    const copy = {
      needs_info: ["ЗАПРОС СВЕДЕНИЙ", "Вернуть расследование на дополнение", "Укажите, каких сведений не хватает и почему без них невозможно продолжить."],
      approve: ["ФИНАЛЬНОЕ РЕШЕНИЕ", "Одобрить допуск", "Решение завершит проверку. Укажите мотивированное основание допуска."],
      deny: ["ФИНАЛЬНОЕ РЕШЕНИЕ", "Отказать в допуске", "Решение завершит проверку. Укажите факты и оценку рисков, на которых основан отказ."],
      reopen: ["ПОВТОРНОЕ ОТКРЫТИЕ", "Возобновить расследование", "Опишите новые обстоятельства, из-за которых завершённое дело необходимо вернуть в работу."],
    }[action];
    if (!copy) return;
    state.confirmAction = action;
    byId("confirm-kicker").textContent = copy[0];
    byId("confirm-title").textContent = copy[1];
    byId("confirm-copy").textContent = copy[2];
    byId("confirm-note").value = "";
    byId("confirm-submit").className = `button button-${action === "deny" ? "danger" : "primary"}`;
    openDialog("confirm-dialog");
  }

  function renderStage(item) {
    const stages = ["new", "screening", "analysis", "decision", "approved"];
    let active = stages.indexOf(item.status);
    if (item.status === "needs_info") active = 1;
    if (["denied", "archived"].includes(item.status)) active = 4;
    const stage = byId("case-stage");
    stage.replaceChildren(...stages.map((status, index) => {
      const row = node("div", "stage-row");
      if (index < active) row.dataset.state = "complete";
      else if (index === active) row.dataset.state = "active";
      const marker = node("i", "", index < active ? "✓" : String(index + 1));
      const copy = node("div");
      copy.append(node("strong", "", index === 4 ? "Завершение" : labels.status[status]), node("small", "", ["Сигнал зарегистрирован", "Факты и источники", "Версии и риски", "Мотивированное решение", labels.status[item.status]][index]));
      row.append(marker, copy);
      return row;
    }));
  }

  function renderActions(item) {
    const actions = byId("case-actions");
    const controls = [];
    if (item.status === "new") controls.push(actionButton("Принять в работу", "claim", "primary"));
    if (["screening", "needs_info"].includes(item.status)) {
      controls.push(actionButton("Перейти к аналитике", "analysis", "primary"));
      controls.push(actionButton("Запросить сведения", "needs_info", "warning", true));
    }
    if (item.status === "analysis") {
      controls.push(actionButton("Подготовить решение", "decision", "primary"));
      controls.push(actionButton("Нужны сведения", "needs_info", "warning", true));
    }
    if (item.status === "decision") {
      controls.push(actionButton("Одобрить допуск", "approve", "primary", true));
      controls.push(actionButton("Отказать", "deny", "danger", true));
      controls.push(actionButton("Вернуть в анализ", "analysis", "secondary"));
    }
    if (["approved", "denied"].includes(item.status)) {
      controls.push(actionButton("Открыть повторно", "reopen", "secondary", true));
      controls.push(actionButton("В архив", "archive", "secondary"));
    }
    if (item.status === "archived") controls.push(actionButton("Открыть повторно", "reopen", "secondary", true));
    actions.replaceChildren(...controls);
  }

  function renderSnapshot(item, detail) {
    const deadline = deadlineMeta(item);
    const rows = [
      ["Ответственный", item.assigned_to_display || "Не назначен"],
      ["Контрольный срок", deadline.label],
      ["Материалы", `${detail.materials.length} · проверено ${detail.materials.filter((entry) => entry.status === "verified").length}`],
      ["Связи", String(detail.relations.length)],
      ["Задачи", `${detail.tasks.filter((entry) => entry.status === "done").length} из ${detail.tasks.length}`],
      ["Последнее изменение", formatMoment(item.updated_at)],
    ];
    const target = byId("case-snapshot");
    target.replaceChildren(...rows.map(([key, value]) => {
      const row = node("div", "snapshot-row");
      row.append(node("span", "", key), node("strong", "", value));
      return row;
    }));
  }

  function renderMaterials(materials) {
    const target = byId("case-materials");
    if (!materials.length) {
      target.replaceChildren(emptyState("Доказательная база пуста", "Добавьте документ, ссылку, свидетельство или наблюдение и отдельно оцените его надёжность."));
      return;
    }
    target.replaceChildren(...materials.map((item) => {
      const card = node("article", "material-card");
      card.dataset.status = item.status;
      const header = node("header");
      const icon = node("span", "material-icon", { document: "▤", link: "↗", testimony: "◌", observation: "⌾", media: "▣", other: "◇" }[item.kind] || "◇");
      const copy = node("div");
      copy.append(node("small", "", `${labels.materialKind[item.kind] || item.kind} · ${labels.reliability[item.reliability] || item.reliability}`), node("h3", "", item.title));
      header.append(icon, copy, pill(labels.materialStatus[item.status] || item.status, item.status));
      card.append(header);
      if (item.content) card.append(node("p", "material-copy", item.content));
      if (item.source_url) {
        const link = node("a", "material-source", "Открыть первоисточник ↗");
        link.href = item.source_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.append(link);
      }
      const footer = node("footer");
      footer.append(node("span", "", `${item.created_by_display || "Сотрудник"} · ${formatMoment(item.created_at)}`));
      if (!closedStatuses.has(state.detail.case.status) && item.status !== "verified") {
        const verify = node("button", "mini-button positive", "Подтвердить");
        verify.type = "button";
        verify.addEventListener("click", () => void relatedMutation({ action: "material_status", material_id: item.id, status: "verified" }, "Материал подтверждён."));
        footer.append(verify);
      }
      if (!closedStatuses.has(state.detail.case.status) && item.status !== "rejected") {
        const reject = node("button", "mini-button danger", "Отклонить");
        reject.type = "button";
        reject.addEventListener("click", () => void relatedMutation({ action: "material_status", material_id: item.id, status: "rejected" }, "Материал отмечен как отклонённый."));
        footer.append(reject);
      }
      card.append(footer);
      return card;
    }));
  }

  function renderRelations(relations) {
    const target = byId("case-relations");
    if (!relations.length) {
      target.replaceChildren(emptyState("Карта связей не построена", "Добавляйте людей, организации и характер их связи с объектом расследования."));
      return;
    }
    const center = node("article", "relation-node subject-node");
    center.append(node("small", "", "ОБЪЕКТ"), node("strong", "", fullName(state.detail.case)), node("span", "", `Статик ${state.detail.case.static_id}`));
    target.replaceChildren(center, ...relations.map((item, index) => {
      const card = node("article", "relation-node");
      card.style.setProperty("--delay", `${index * 45}ms`);
      card.append(node("small", "", labels.reliability[item.confidence] || item.confidence), node("strong", "", item.person_name), node("b", "", item.relation_type));
      if (item.static_id || item.discord_text) card.append(node("span", "", [item.static_id ? `Статик ${item.static_id}` : "", item.discord_text].filter(Boolean).join(" · ")));
      if (item.details) card.append(node("p", "", item.details));
      return card;
    }));
  }

  function nextTaskStatus(status) {
    return { todo: "doing", doing: "done", blocked: "doing", done: "todo" }[status] || "doing";
  }

  function renderTasks(tasks) {
    const target = byId("case-tasks");
    if (!tasks.length) {
      target.replaceChildren(emptyState("Оперативный план пуст", "Разложите расследование на проверяемые задачи с ответственными и сроками."));
      return;
    }
    const groups = ["todo", "doing", "blocked", "done"];
    target.replaceChildren(...groups.map((status) => {
      const column = node("section", "task-column");
      const items = tasks.filter((item) => item.status === status);
      const heading = node("header");
      heading.append(node("strong", "", labels.taskStatus[status]), node("b", "", items.length));
      column.append(heading);
      if (!items.length) column.append(node("div", "task-empty", "Нет задач"));
      else column.append(...items.map((item) => {
        const card = node("article", "task-card");
        card.dataset.priority = item.priority;
        card.append(pill(labels.priority[item.priority] || item.priority, item.priority), node("h3", "", item.title));
        if (item.description) card.append(node("p", "", item.description));
        const meta = node("div", "task-meta");
        meta.append(node("span", "", item.assignee_display || "Без ответственного"), node("span", "", item.due_at ? formatMoment(item.due_at) : "Без срока"));
        card.append(meta);
        if (!closedStatuses.has(state.detail.case.status)) {
          const controls = node("footer");
          const advance = node("button", "mini-button", item.status === "done" ? "Вернуть" : item.status === "doing" ? "Завершить" : "В работу");
          advance.type = "button";
          advance.addEventListener("click", () => void relatedMutation({ action: "task_status", task_id: item.id, status: nextTaskStatus(item.status) }, "Статус задачи обновлён."));
          controls.append(advance);
          if (!["blocked", "done"].includes(item.status)) {
            const block = node("button", "mini-button danger", "Блокер");
            block.type = "button";
            block.addEventListener("click", () => void relatedMutation({ action: "task_status", task_id: item.id, status: "blocked" }, "Задача отмечена как заблокированная."));
            controls.append(block);
          }
          card.append(controls);
        }
        return card;
      }));
      return column;
    }));
  }

  function renderTimeline(events) {
    const target = byId("case-timeline");
    const ordered = [...events].reverse();
    if (!ordered.length) {
      target.replaceChildren(emptyState("Журнал пуст", "Первое событие появится после регистрации расследования."));
      return;
    }
    target.replaceChildren(...ordered.map((item) => {
      const row = node("article", "timeline-item");
      const marker = node("i");
      const body = node("div");
      body.append(node("strong", "", labels.event[item.action] || item.action));
      if (item.note) body.append(node("p", "", item.note));
      body.append(node("small", "", `${item.actor_display || "Система"} · ${formatMoment(item.created_at, true)}`));
      row.append(marker, body);
      return row;
    }));
  }

  function renderCase() {
    const detail = state.detail;
    if (!detail?.case) return;
    const item = detail.case;
    byId("case-breadcrumb").textContent = caseNumber(item);
    byId("case-command-name").textContent = fullName(item);
    byId("case-monogram").textContent = `${String(item.first_name || "?")[0]}${String(item.last_name || "?")[0]}`.toUpperCase();
    byId("case-kicker").lastChild.textContent = ` ${labels.kind[item.case_kind] || "РАССЛЕДОВАНИЕ ОВР"}`.toUpperCase();
    byId("case-title").textContent = fullName(item);
    byId("case-subtitle").textContent = `${caseNumber(item)} · статик ${item.static_id} · ${textOr(item.discord_text, "Discord не указан")}`;
    byId("case-pills").replaceChildren(
      pill(labels.status[item.status] || item.status, item.status),
      pill(labels.risk[item.risk_level] || item.risk_level, item.risk_level),
      pill(`Приоритет: ${labels.priority[item.priority] || item.priority}`, item.priority),
    );
    const progress = Math.max(0, Math.min(100, Number(item.progress || 0)));
    byId("case-progress-ring").style.setProperty("--progress", progress);
    byId("case-progress-value").textContent = `${progress}%`;
    byId("tab-material-count").textContent = String(detail.materials.length);
    byId("tab-relation-count").textContent = String(detail.relations.length);
    byId("tab-task-count").textContent = String(detail.tasks.length);
    byId("case-classification-mark").textContent = labels.classification[item.classification] || item.classification;
    byId("case-facts").replaceChildren(
      fact("Полное имя", fullName(item)),
      fact("Статик", item.static_id),
      fact("Discord", item.discord_text),
      fact("Тип расследования", labels.kind[item.case_kind] || item.case_kind),
      fact("Создатель", item.created_by_display),
      fact("Форум", item.forum_url ? "Открыть профиль ↗" : "Не указан", item.forum_url || ""),
    );
    byId("case-aliases-view").textContent = textOr(item.aliases, "Не установлены");
    byId("case-affiliations-view").textContent = textOr(item.affiliations, "Не установлены");
    byId("case-additional-view").textContent = textOr(item.additional_info, "Исходные сведения не указаны");

    const fields = {
      "case-objective": item.objective,
      "case-hypothesis": item.hypothesis,
      "case-summary": item.executive_summary,
      "case-findings": item.findings,
      "case-nowa": item.nowa_links,
      "case-risk": item.risk_level || "unrated",
      "case-priority": item.priority || "normal",
      "case-classification": item.classification || "restricted",
      "case-aliases": item.aliases,
      "case-affiliations": item.affiliations,
    };
    Object.entries(fields).forEach(([id, value]) => { byId(id).value = value || ""; });
    byId("case-note").value = "";
    const closed = closedStatuses.has(item.status);
    ["case-objective", "case-hypothesis", "case-summary", "case-findings", "case-nowa", "case-risk", "case-priority", "case-classification", "case-aliases", "case-affiliations", "case-save", "material-new", "relation-new", "task-new"].forEach((id) => { byId(id).disabled = closed; });
    byId("case-add-note").disabled = false;
    renderStage(item);
    renderActions(item);
    renderSnapshot(item, detail);
    renderMaterials(detail.materials);
    renderRelations(detail.relations);
    renderTasks(detail.tasks);
    renderTimeline(detail.events);
    selectTab(state.activeTab);
  }

  function selectTab(tab) {
    state.activeTab = tab;
    byId("case-tabs").querySelectorAll("button[data-tab]").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
    document.querySelectorAll(".case-panel[data-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === tab));
  }

  function dossierPayload() {
    return {
      objective: byId("case-objective").value,
      hypothesis: byId("case-hypothesis").value,
      executive_summary: byId("case-summary").value,
      findings: byId("case-findings").value,
      nowa_links: byId("case-nowa").value,
      risk_level: byId("case-risk").value,
      priority: byId("case-priority").value,
      classification: byId("case-classification").value,
      aliases: byId("case-aliases").value,
      affiliations: byId("case-affiliations").value,
    };
  }

  async function mutateCase(action, extra = {}, message = "Изменения сохранены.") {
    const item = state.detail?.case;
    if (!item || state.busy) return false;
    setBusy(true);
    try {
      const data = await request("/api/ovr", {
        method: "POST",
        body: JSON.stringify({ action, case_id: item.id, expected_revision: item.revision, ...extra }),
      });
      if (data.detail) applyDetail(data.detail);
      else if (data.case) { mergeCase(data.case); renderBoard(); }
      toast(message);
      return true;
    } catch (error) {
      toast(error.message, "error");
      if (error.code === "ovr_case_revision_conflict") void refreshCase(true);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function relatedMutation(payload, message) {
    const item = state.detail?.case;
    if (!item || state.busy) return false;
    const { action, ...extra } = payload;
    return mutateCase(action, extra, message);
  }

  async function loadRegistry({ silent = false } = {}) {
    try {
      const data = await request("/api/ovr");
      state.csrf = data.viewer?.csrf_token || state.csrf;
      state.cases = Array.isArray(data.cases) ? data.cases : [];
      renderBoard();
      stampSync();
      byId("ovr-gate").hidden = true;
      byId("ovr-shell").hidden = false;
      requestAnimationFrame(() => byId("ovr-shell").classList.add("ready"));
      return true;
    } catch (error) {
      if (error.status === 401) {
        byId("ovr-gate-message").textContent = "Сессия не найдена. Войдите с логином и восьмизначным PIN.";
        byId("ovr-login").hidden = false;
      } else if (error.status === 403) {
        byId("ovr-gate-message").textContent = "Этот контур закрыт. Запросите у администратора отдельный доступ к ОВР.";
      } else if (!silent) {
        byId("ovr-gate-message").textContent = error.message;
        toast(error.message, "error");
      }
      return false;
    }
  }

  async function fetchCase(id, { silent = false } = {}) {
    try {
      const data = await request(`/api/ovr?case_id=${encodeURIComponent(id)}`);
      state.csrf = data.viewer?.csrf_token || state.csrf;
      applyDetail(data.detail);
      return true;
    } catch (error) {
      if (!silent) toast(error.message, "error");
      return false;
    }
  }

  async function openCase(id, push = true) {
    const caseId = Number(id);
    if (!Number.isFinite(caseId)) return;
    byId("ovr-registry-page").hidden = true;
    byId("ovr-case-page").hidden = false;
    if (push && location.hash !== `#/case/${caseId}`) history.pushState({ caseId }, "", `#/case/${caseId}`);
    const cached = state.cases.find((item) => Number(item.id) === caseId);
    if (cached) {
      byId("case-breadcrumb").textContent = caseNumber(cached);
      byId("case-command-name").textContent = fullName(cached);
      byId("case-title").textContent = fullName(cached);
      byId("case-subtitle").textContent = "Получаем материалы, связи и журнал…";
    }
    setBusy(true, "Открываем расследование");
    await fetchCase(caseId);
    setBusy(false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function refreshCase(silent = false) {
    const id = state.detail?.case?.id;
    if (!id) return;
    if (!silent) setBusy(true, "Получаем свежую карточку");
    const ok = await fetchCase(id, { silent });
    if (!silent) {
      setBusy(false);
      if (ok) toast("Расследование синхронизировано.");
    }
  }

  function showRegistry(push = true) {
    state.detail = null;
    byId("ovr-case-page").hidden = true;
    byId("ovr-registry-page").hidden = false;
    if (push && location.hash) history.pushState({}, "", location.pathname + location.search);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function route() {
    const match = location.hash.match(/^#\/case\/(\d+)$/);
    if (match) void openCase(Number(match[1]), false);
    else showRegistry(false);
  }

  async function createCase(event) {
    event.preventDefault();
    if (state.busy) return;
    setBusy(true, "Регистрируем расследование");
    try {
      const data = await request("/api/ovr", {
        method: "POST",
        body: JSON.stringify({
          action: "create",
          first_name: byId("ovr-first-name").value.trim(),
          last_name: byId("ovr-last-name").value.trim(),
          static_id: byId("ovr-static").value.trim(),
          discord_text: byId("ovr-discord").value.trim(),
          case_kind: byId("ovr-kind").value,
          priority: byId("ovr-priority").value,
          forum_url: byId("ovr-forum").value.trim(),
          objective: byId("ovr-objective").value.trim(),
          additional_info: byId("ovr-additional").value.trim(),
        }),
      });
      mergeCase(data.case);
      renderBoard();
      closeDialog("ovr-create-dialog");
      event.currentTarget.reset();
      toast(`${caseNumber(data.case)} зарегистрировано. Контрольный срок — 48 часов.`);
      setBusy(false);
      await openCase(data.case.id);
    } catch (error) {
      setBusy(false);
      toast(error.message, "error");
    }
  }

  async function submitMaterial(event) {
    event.preventDefault();
    const ok = await relatedMutation({
      action: "material_add",
      kind: byId("material-kind").value,
      reliability: byId("material-reliability").value,
      title: byId("material-title").value.trim(),
      source_url: byId("material-url").value.trim(),
      content: byId("material-content").value.trim(),
    }, "Материал добавлен в доказательную базу.");
    if (ok) { closeDialog("material-dialog"); event.currentTarget.reset(); }
  }

  async function submitRelation(event) {
    event.preventDefault();
    const ok = await relatedMutation({
      action: "relation_add",
      person_name: byId("relation-name").value.trim(),
      relation_type: byId("relation-type").value.trim(),
      static_id: byId("relation-static").value.trim(),
      discord_text: byId("relation-discord").value.trim(),
      confidence: byId("relation-confidence").value,
      details: byId("relation-details").value.trim(),
    }, "Связь добавлена в карту окружения.");
    if (ok) { closeDialog("relation-dialog"); event.currentTarget.reset(); }
  }

  async function submitTask(event) {
    event.preventDefault();
    const dueValue = byId("task-due").value;
    const ok = await relatedMutation({
      action: "task_add",
      title: byId("task-title").value.trim(),
      description: byId("task-description").value.trim(),
      priority: byId("task-priority").value,
      due_at: dueValue ? new Date(dueValue).toISOString() : "",
      assignee_display: byId("task-assignee").value.trim(),
    }, "Оперативная задача поставлена.");
    if (ok) { closeDialog("task-dialog"); event.currentTarget.reset(); }
  }

  async function downloadProtectedReport(event) {
    event.preventDefault();
    if (!state.detail?.case?.id) {
      toast("Сначала откройте расследование.", "error");
      return;
    }
    const password = byId("report-password").value;
    const confirmation = byId("report-password-confirm").value;
    if (password.length < 8 || password.length > 128) {
      toast("Пароль должен содержать от 8 до 128 символов.", "error");
      return;
    }
    if (password !== confirmation) {
      toast("Пароли отчёта не совпадают.", "error");
      return;
    }

    setBusy(true, "Шифруем архивное досье");
    try {
      const response = await fetch(`/api/ovr/${encodeURIComponent(state.detail.case.id)}/report.pdf`, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        signal: timeoutSignal(60000),
        headers: {
          Accept: "application/pdf, application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": state.csrf,
        },
        body: JSON.stringify({ password, password_confirmation: confirmation }),
      });
      if (!response.ok) {
        let message = `Ошибка HTTP ${response.status}`;
        try {
          const payload = await response.json();
          message = payload.message || payload.error || message;
        } catch { /* Ответ без JSON. */ }
        throw new Error(message);
      }
      const report = await response.blob();
      if (!report.size) throw new Error("Сервер вернул пустой отчёт.");
      const filename = `ovr-${String(state.detail.case.case_number || 0).padStart(3, "0")}-dossier.pdf`;
      const url = URL.createObjectURL(report);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      link.hidden = true;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30000);
      event.currentTarget.reset();
      closeDialog("report-dialog");
      toast("Защищённое досье сформировано и загружено.");
    } catch (error) {
      toast(error.name === "AbortError" ? "Формирование отчёта заняло слишком много времени." : error.message, "error");
    } finally {
      byId("report-password").value = "";
      byId("report-password-confirm").value = "";
      setBusy(false);
    }
  }

  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => closeDialog(button.dataset.close)));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  }));
  byId("ovr-home").addEventListener("click", () => showRegistry());
  byId("case-back").addEventListener("click", () => showRegistry());
  byId("ovr-new").addEventListener("click", () => openDialog("ovr-create-dialog"));
  byId("ovr-refresh").addEventListener("click", async () => {
    setBusy(true, "Синхронизируем реестр");
    const ok = await loadRegistry({ silent: true });
    setBusy(false);
    toast(ok ? "Реестр синхронизирован." : "Не удалось обновить реестр.", ok ? "success" : "error");
  });
  byId("case-reload").addEventListener("click", () => void refreshCase());
  byId("case-report").addEventListener("click", () => {
    byId("report-form").reset();
    openDialog("report-dialog");
    window.setTimeout(() => byId("report-password").focus(), 60);
  });
  byId("ovr-search").addEventListener("input", renderBoard);
  byId("ovr-filters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    state.filter = button.dataset.filter;
    byId("ovr-filters").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    renderBoard();
  });
  byId("case-tabs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-tab]");
    if (button) selectTab(button.dataset.tab);
  });
  byId("case-save").addEventListener("click", () => void mutateCase("update", dossierPayload(), "Аналитическая карточка сохранена."));
  byId("case-add-note").addEventListener("click", async () => {
    const note = byId("case-note").value.trim();
    if (note.length < 3) { toast("Напишите служебную запись.", "error"); return; }
    const ok = await mutateCase("note", { note }, "Служебная запись добавлена.");
    if (ok) byId("case-note").value = "";
  });
  byId("material-new").addEventListener("click", () => openDialog("material-dialog"));
  byId("relation-new").addEventListener("click", () => openDialog("relation-dialog"));
  byId("task-new").addEventListener("click", () => openDialog("task-dialog"));
  byId("ovr-create-form").addEventListener("submit", createCase);
  byId("material-form").addEventListener("submit", submitMaterial);
  byId("relation-form").addEventListener("submit", submitRelation);
  byId("task-form").addEventListener("submit", submitTask);
  byId("report-form").addEventListener("submit", downloadProtectedReport);
  byId("confirm-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const action = state.confirmAction;
    const note = byId("confirm-note").value.trim();
    if (!action || note.length < 3) { toast("Добавьте служебное основание.", "error"); return; }
    closeDialog("confirm-dialog");
    state.confirmAction = null;
    await mutateCase(action, { note }, successMessage(action));
  });
  window.addEventListener("popstate", route);

  void (async () => {
    const loaded = await loadRegistry();
    if (loaded) route();
  })();
})();
