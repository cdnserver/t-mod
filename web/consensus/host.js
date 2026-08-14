"use strict";

const byId = (id) => document.getElementById(id);
const query = new URLSearchParams(window.location.search);
const selectedMode = query.get("mode") === "simulation" ? "simulation" : "live";
const detailCache = new Map();
let state = null;
let fetching = false;
let pollTimer = null;
let clockTimer = null;
let toastTimer = null;
let promptSignature = "";

function readSetting(key, fallback = "") {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function writeSetting(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Суфлёр остаётся рабочим, даже если браузер запретил локальное хранилище.
  }
}

let autoScroll = readSetting("t-consensus-host-follow", "on") !== "off";
let scriptScale = Math.max(
  0.72,
  Math.min(1.45, Number(readSetting("t-consensus-host-scale", "1"))),
);

const STAGE_INDEX = {
  idle: "00",
  scheduled: "01",
  registration: "02",
  presentation: "03",
  voting: "04",
  discussion_type: "05",
  discussion: "06",
  paused: "07",
  finalizing: "08",
  after_result: "09",
  completed: "10",
};

const RESULT_LABELS = {
  accepted: "принят",
  rejected: "отклонён",
  vetoed: "завершён применением права вето",
  oral: "зафиксирован устно",
};

function text(id, value) {
  byId(id).textContent = String(value ?? "—");
}

function cleanSpeech(value) {
  return String(value || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/\[([^\]]+)]\([^\)]+\)/g, "$1")
    .replace(/[*_~`>#|]/g, " ")
    .replace(/https?:\/\/\S+/g, "ссылка доступна в материалах")
    .replace(/\s+/g, " ")
    .trim();
}

function clip(value, limit = 620) {
  const clean = cleanSpeech(value);
  if (clean.length <= limit) return clean;
  return `${clean.slice(0, limit).replace(/\s+\S*$/, "").trim()}…`;
}

function formatNumber(value) {
  return String(Math.max(0, Number(value) || 0)).padStart(3, "0");
}

function formatPercent(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "0";
  return numeric.toLocaleString("ru-RU", { maximumFractionDigits: 1 });
}

function plural(value, one, few, many) {
  const number = Math.abs(Number(value) || 0) % 100;
  const tail = number % 10;
  if (number > 10 && number < 20) return many;
  if (tail === 1) return one;
  if (tail >= 2 && tail <= 4) return few;
  return many;
}

function ordinal(value) {
  const words = {
    1: "Первый", 2: "Второй", 3: "Третий", 4: "Четвёртый", 5: "Пятый",
    6: "Шестой", 7: "Седьмой", 8: "Восьмой", 9: "Девятый", 10: "Десятый",
    11: "Одиннадцатый", 12: "Двенадцатый", 13: "Тринадцатый",
    14: "Четырнадцатый", 15: "Пятнадцатый", 16: "Шестнадцатый",
    17: "Семнадцатый", 18: "Восемнадцатый", 19: "Девятнадцатый",
    20: "Двадцатый",
  };
  const number = Number(value) || 0;
  return words[number] || `${number}-й`;
}

function formatMoment(value) {
  if (!value) return "время уточняется";
  const moment = new Date(value);
  if (Number.isNaN(moment.getTime())) return "время уточняется";
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
  }).format(moment);
}

function formatTimer(value) {
  if (!value) return "не запущен";
  const remaining = Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 1000));
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function billEssence(bill) {
  return clip(bill?.summary, 720) || "Содержание проекта изложено в опубликованном тексте и материалах.";
}

function billIntroduction(bill) {
  const number = formatNumber(bill?.bill_number);
  const title = cleanSpeech(bill?.title) || "Без названия";
  const author = cleanSpeech(bill?.author?.name) || "автор не указан";
  return [
    `Рассматривается законопроект №${number} — «${title}».`,
    `Автор проекта — ${author}.`,
    `Предмет решения: ${billEssence(bill)}`,
    "Полный текст, приложения и первоначально поданный материал доступны в личных панелях и на трансляции.",
  ].join("\n\n");
}

function voteOpening(bill) {
  return `Законопроект №${formatNumber(bill?.bill_number)} поставлен на воут. Голосование открыто. Доступны три позиции: «За», «Против» и «Воздержаться». Прошу проверить название проекта перед подтверждением выбора.`;
}

function resultSpeech(result, bill) {
  const status = String(result?.status || "");
  const number = formatNumber(result?.bill_number || bill?.bill_number);
  const title = cleanSpeech(result?.title || bill?.title) || "Без названия";
  if (status === "vetoed") {
    return `Система зафиксировала применение права вето к законопроекту №${number} — «${title}». Текущий проект завершён в особом порядке. Зафиксированный итог включается в официальный протокол без ручного повторения действия.`;
  }
  const overall = formatPercent(result?.overall_percent);
  const opposed = formatPercent(result?.opposed_percent);
  const accepted = status === "accepted";
  return [
    "Приём голосов завершён. Результат зафиксирован системой.",
    `По законопроекту №${number} — «${title}» общий консенсус составил ${overall} процента за и ${opposed} процента против.`,
    accepted
      ? "В соответствии с действующим порядком законопроект принят."
      : "Установленный порог принятия не достигнут. Законопроект отклонён.",
    "Решение будет включено в официальный протокол заседания.",
  ].join("\n\n");
}

function completedSpeech(session) {
  const results = session?.results || [];
  const accepted = results.filter((item) => item.status === "accepted").length;
  const rejected = results.filter((item) => item.status === "rejected").length;
  const special = results.length - accepted - rejected;
  return [
    "Сенаторы Товарищества. Повестка исчерпана, результаты сохранены, официальный протокол сформирован системой.",
    `Сегодня рассмотрено ${results.length} ${plural(results.length, "проект", "проекта", "проектов")}: принято — ${accepted}, отклонено — ${rejected}, завершено в особом порядке — ${special}.`,
    "Благодарю участников за точность позиции, соблюдение порядка и ответственность перед общим решением.",
    `${ordinal(session?.plenary_number)} пленарный консенсус Товарищества объявляется завершённым.`,
    "Товарищество — светлый круг. Заседание окончено.",
  ].join("\n\n");
}

function derivePrompt(data) {
  const session = data.session;
  const schedule = data.schedule;
  const queue = data.queue || [];
  if (!session) {
    if (data.broadcast_phase === "completed" && data.last_session) {
      return {
        key: `completed:${data.last_session.key || data.last_session.plenary_number}`,
        stage: "completed",
        label: "Заседание завершено",
        context: "Финальная формула",
        speech: completedSpeech(data.last_session),
        action: "Проверить итоговый протокол",
        actionNote: "Сверьте карточки решений, графический итог и PDF.",
        next: "Официальным источником итогов является протокол T-Mod.",
        note: "Не запускайте повторное формирование, если доставка ещё выполняется.",
      };
    }
    if (schedule) {
      const number = schedule.plenary_number;
      const count = queue.length;
      return {
        key: `scheduled:${schedule.id}:${schedule.revision}`,
        stage: "scheduled",
        label: "Заседание запланировано",
        context: `Начало · ${formatMoment(schedule.scheduled_for)}`,
        speech: [
          `Сенаторы Товарищества, созывается ${ordinal(number).toLowerCase()} пленарный консенсус Товарищества.`,
          `Начало — ${formatMoment(schedule.scheduled_for)} по времени Товарищества. К рассмотрению подготовлено ${count} ${plural(count, "проект", "проекта", "проектов")}.`,
          "Прошу заранее ознакомиться с материалами и подтвердить возможность участия.",
        ].join("\n\n"),
        action: "Проверить готовность",
        actionNote: "Перед регистрацией сверьте голосовой канал, очередь и состав.",
        next: "До открытия регистрации остаётся несколько минут. Просьба проверить личные сообщения T-Mod и сохранять присутствие в голосовом канале.",
        note: "После проверки нажмите «Подготовить заседание» в основном пульте.",
      };
    }
    return {
      key: "idle",
      stage: "idle",
      label: "Ожидание плана",
      context: "Заседание ещё не запланировано",
      speech: "Пленарный консенсус ещё не созван. После публикации плана здесь автоматически появятся дата, повестка и готовый текст приглашения.",
      action: "Запланировать заседание",
      actionNote: "Ядерный Реактор → Консенсус → Планирование.",
      next: "Сенаторы Товарищества, созывается очередной пленарный консенсус.",
      note: "Суфлёр останется на связи и обновится после сохранения плана.",
    };
  }

  const bill = session.current_bill;
  const quorum = session.quorum || {};
  const voting = session.voting || {};
  switch (session.stage) {
    case "registration":
      return {
        key: `registration:${session.key}`,
        stage: "registration",
        label: "Регистрация",
        context: `${quorum.confirmed || 0} из ${quorum.invited || 0} участников · кворум ${quorum.ready ? "собран" : "ожидается"}`,
        speech: quorum.ready
          ? `Регистрация подходит к завершению. Подтверждённый состав — ${quorum.confirmed || 0} участников. Кворум собран. Возражения по составу и готовности к открытию заседания принимаются сейчас.`
          : `Открыта регистрация участников ${ordinal(session.plenary_number).toLowerCase()} пленарного консенсуса Товарищества. Прошу подтвердить участие через личный пульт T-Mod. Сейчас подтверждено ${quorum.confirmed || 0} из ${quorum.invited || 0} участников.`,
        action: quorum.ready ? "Представить первый проект" : "Дождаться кворума",
        actionNote: quorum.ready
          ? "Если подтвердились не все приглашённые, отдельно подтвердите начало текущим составом."
          : "Для недавно вошедших участников используйте «Повторить приглашения».",
        next: `${ordinal(session.plenary_number)} пленарный консенсус Товарищества объявляется открытым.`,
        note: "Не объявляйте заседание открытым до появления первого проекта.",
      };
    case "presentation":
      return {
        key: `presentation:${session.key}:${bill?.id || 0}`,
        stage: "presentation",
        label: "Представление проекта",
        context: `Проект №${formatNumber(bill?.bill_number)} · воут закрыт`,
        speech: billIntroduction(bill),
        action: "Поставить на воут",
        actionNote: "Нажмите только после доклада и уточняющих вопросов.",
        next: voteOpening(bill),
        note: "Участники уже видят текст, но пока не могут голосовать.",
      };
    case "voting":
      return {
        key: `voting:${session.key}:${bill?.id || 0}`,
        stage: "voting",
        label: "Воут открыт",
        context: `${voting.received || 0} из ${voting.expected || 0} бюллетеней · ${formatTimer(session.timer_deadline)}`,
        speech: `${voteOpening(bill)}\n\nГолосование продолжается. Принято ${voting.received || 0} из ${voting.expected || 0} бюллетеней.`,
        action: Number(voting.received || 0) >= Number(voting.expected || 0) && Number(voting.expected || 0) > 0
          ? "Дождаться автоматической фиксации"
          : "Следить за голосами и таймером",
        actionNote: session.timer_deadline
          ? "При необходимости добавьте время; текущий срок не сбрасывается."
          : "При необходимости установите таймер на 30 секунд, 1, 3 или 5 минут.",
        next: "Приём голосов завершён. Результат фиксируется системой.",
        note: "Не называйте направления голосов и промежуточный процент.",
      };
    case "discussion_type":
      return {
        key: `discussion-type:${session.key}:${bill?.id || 0}`,
        stage: "discussion_type",
        label: "Запрос дискуссии",
        context: `Проект №${formatNumber(bill?.bill_number)}`,
        speech: `Поступил запрос на дискуссию по законопроекту №${formatNumber(bill?.bill_number)}. Течение воута приостановлено. Ранее поданные позиции сохранены и могут быть изменены после возвращения к голосованию.`,
        action: "Выбрать тип дискуссии",
        actionNote: "Правовая, фактическая, процедурная или иная.",
        next: "Дискуссия открывается. Прошу выступать по существу, отделять факты от оценки и не повторять уже изложенные доводы.",
        note: "Таймер голосования на этом этапе не действует.",
      };
    case "discussion":
      return {
        key: `discussion:${session.key}:${bill?.id || 0}:${session.discussion?.type || ""}`,
        stage: "discussion",
        label: `${session.discussion?.type || "Открытая"} дискуссия`,
        context: `Проект №${formatNumber(bill?.bill_number)}`,
        speech: `Открыта ${(session.discussion?.type || "иная").toLowerCase()} дискуссия по законопроекту №${formatNumber(bill?.bill_number)}. Прошу выступать по существу, отделять факты от оценки и не повторять уже изложенные доводы.`,
        action: "Завершить дискуссию после последнего слова",
        actionNote: "Перед завершением спросите о новых существенных доводах.",
        next: "Дискуссия завершена. Голосование продолжено. Ранее поданные позиции сохранены; до фиксации их можно изменить.",
        note: "После возвращения к воуту при необходимости заново установите таймер.",
      };
    case "paused":
      return {
        key: `paused:${session.key}:${session.pause_reason || ""}`,
        stage: "paused",
        label: "Процедурная пауза",
        context: cleanSpeech(session.pause_reason) || "Причина указана в пульте",
        speech: `Для восстановления процедурных условий объявляется пауза. Причина: ${cleanSpeech(session.pause_reason) || "техническая проверка"}. Текущий проект и принятые системой действия сохранены.`,
        action: "Продолжить после восстановления условий",
        actionNote: "T-Mod не позволит продолжить без голосового кворума.",
        next: `${ordinal(session.plenary_number)} пленарный консенсус продолжает работу с сохранённой стадии.`,
        note: "Сначала нажмите «Продолжить» и дождитесь подтверждения, затем произнесите реплику.",
      };
    case "finalizing":
      return {
        key: `finalizing:${session.key}:${bill?.id || 0}`,
        stage: "finalizing",
        label: "Фиксация результата",
        context: `Проект №${formatNumber(bill?.bill_number)} · голоса заблокированы`,
        speech: "Приём голосов завершён. Результат фиксируется системой. До появления итоговой карточки решение не оглашается.",
        action: "Дождаться итоговой карточки",
        actionNote: "Не повторяйте действие. «Повторить фиксацию» используйте только по предложению системы.",
        next: "Результат зафиксирован системой.",
        note: "Переход к следующему проекту пока запрещён.",
      };
    case "after_result": {
      const result = session.current_result || (session.results || []).at(-1);
      return {
        key: `result:${session.key}:${result?.bill_id || 0}:${result?.status || ""}`,
        stage: "after_result",
        label: `Проект ${RESULT_LABELS[result?.status] || "завершён"}`,
        context: `Общий консенсус · ${formatPercent(result?.overall_percent)}% за`,
        speech: resultSpeech(result, bill),
        action: queue.length ? "Следующий проект" : "Завершить заседание",
        actionNote: queue.length
          ? `В очереди остаётся ${queue.length} ${plural(queue.length, "проект", "проекта", "проектов")}.`
          : "Сначала сверьте количество результатов, затем сформируйте протокол.",
        next: queue.length
          ? "Решение зафиксировано. Переходим к следующему вопросу повестки."
          : completedSpeech(session),
        note: "Оглашайте только значения из итоговой карточки T-Mod.",
      };
    }
    default:
      return {
        key: `${session.stage}:${session.key}`,
        stage: session.stage || "idle",
        label: session.stage_label || "Текущий этап",
        context: "Состояние получено от T-Mod",
        speech: "Система обновила состояние заседания. Перед продолжением сверьте текущий этап в основном пульте.",
        action: "Открыть основной пульт",
        actionNote: "Продолжайте только после проверки стадии.",
        next: "Заседание продолжается в установленном порядке.",
        note: "Суфлёр не выполняет управляющие команды.",
      };
  }
}

function agendaItems(data) {
  const session = data.session;
  const items = new Map();
  (session?.results || []).forEach((result) => {
    const bill = result.bill || {
      id: result.bill_id,
      bill_number: result.bill_number,
      title: result.title,
      author: { name: "Автор не указан" },
    };
    items.set(Number(bill.id || result.bill_id), { bill, result, status: "done" });
  });
  if (session?.current_bill) {
    items.set(Number(session.current_bill.id), {
      bill: session.current_bill,
      result: session.current_result,
      status: "current",
    });
  }
  (data.queue || []).forEach((bill) => {
    const id = Number(bill.id || 0);
    if (id > 0 && !items.has(id)) items.set(id, { bill, result: null, status: "queued" });
  });
  return [...items.values()].sort(
    (left, right) => Number(left.bill.bill_number || 0) - Number(right.bill.bill_number || 0),
  );
}

function preparedBillSpeech(item) {
  const bill = detailCache.get(Number(item.bill.id))?.bill || item.bill;
  const presentation = billIntroduction(bill);
  const vote = voteOpening(bill);
  return `${presentation}\n\nПосле представления:\n«${vote}»`;
}

function node(tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== undefined) element.textContent = String(value);
  return element;
}

function renderAgenda(data) {
  const list = byId("host-agenda-list");
  const items = agendaItems(data);
  const completed = items.filter((item) => item.status === "done").length;
  text("host-agenda-count", items.length);
  text("host-agenda-progress", `${completed} / ${items.length}`);
  list.replaceChildren();
  if (!items.length) {
    list.append(node("div", "host-agenda-empty", "Повестка пока пуста. Реплики появятся после публикации законопроектов."));
    return;
  }
  items.forEach((item) => {
    const fullBill = detailCache.get(Number(item.bill.id))?.bill || item.bill;
    const card = node("article", `host-bill-script ${item.status}`);
    card.dataset.billId = String(fullBill.id || 0);
    const number = node("div", "host-bill-number", `№${formatNumber(fullBill.bill_number)}`);
    number.append(node(
      "small",
      "",
      item.status === "current" ? "СЕЙЧАС" : item.status === "done" ? "ЗАВЕРШЁН" : "ВПЕРЕДИ",
    ));
    const copy = node("div", "host-bill-copy");
    copy.append(
      node("h3", "", cleanSpeech(fullBill.title) || "Без названия"),
      node("small", "", `Автор · ${cleanSpeech(fullBill.author?.name) || "не указан"}`),
    );
    const detail = document.createElement("details");
    if (item.status === "current") detail.open = true;
    detail.append(node("summary", "", "Подготовленная реплика"));
    detail.append(node("p", "", preparedBillSpeech({ ...item, bill: fullBill })));
    copy.append(detail);
    const button = node("button", "", "Копировать");
    button.type = "button";
    button.addEventListener("click", () => void copyText(preparedBillSpeech({ ...item, bill: fullBill })));
    card.append(number, copy, button);
    list.append(card);
  });
}

async function hydrateAgenda(data) {
  const missing = agendaItems(data)
    .map((item) => Number(item.bill.id || 0))
    .filter((id) => id > 0 && !detailCache.has(id));
  if (!missing.length) return;
  await Promise.allSettled(missing.map(async (id) => {
    detailCache.set(id, null);
    const response = await fetch(`/api/bills/${id}?mode=${encodeURIComponent(selectedMode)}`, {
      credentials: "same-origin",
      cache: "no-store",
      signal: timeoutSignal(8000),
    });
    if (!response.ok) {
      detailCache.set(id, { error: true });
      return;
    }
    detailCache.set(id, await response.json());
  }));
  if (state === data) renderAgenda(data);
}

function renderMetrics(data) {
  const session = data.session;
  text("host-quorum", session ? `${session.quorum?.confirmed || 0} / ${session.quorum?.invited || 0}` : "—");
  text("host-votes", session ? `${session.voting?.received || 0} / ${session.voting?.expected || 0}` : "—");
  text("host-timer", session ? formatTimer(session.timer_deadline) : "—");
  text("host-agenda-count", agendaItems(data).length || data.queue?.length || 0);
}

function render(data) {
  state = data;
  const viewer = data.viewer || {};
  const allowed = Boolean(viewer.leader || viewer.chair || viewer.administrator);
  if (!allowed) {
    byId("host-shell").hidden = true;
    byId("host-gate").hidden = false;
    text("host-gate-title", "Суфлёр закрыт");
    text("host-gate-copy", "Эта страница предназначена для ведущего и председателей Товарищества.");
    return;
  }

  byId("host-gate").hidden = true;
  byId("host-shell").hidden = false;
  const prompt = derivePrompt(data);
  const session = data.session;
  const schedule = data.schedule;
  const plenary = session?.plenary_number || schedule?.plenary_number || data.last_session?.plenary_number || 0;
  text("host-mode", data.mode === "simulation" ? "УЧЕБНЫЙ КОНТУР" : "РАБОЧИЙ КОНТУР");
  text("host-session-title", plenary ? `${ordinal(plenary)} пленарный консенсус` : "Пленарный консенсус");
  text(
    "host-session-meta",
    session
      ? `Ведущий · ${session.leader?.name || viewer.name || "—"}`
      : schedule
        ? `Ведущий · ${schedule.host?.name || viewer.name || "—"} · ${formatMoment(schedule.scheduled_for)}`
        : `Подготовка · ${viewer.name || "председатель"}`,
  );
  text("host-stage-index", STAGE_INDEX[prompt.stage] || "—");
  text("host-stage-label", prompt.label);
  text("host-live-context", prompt.context);
  text("host-live-text", prompt.speech);
  text("host-live-note", prompt.note);
  text("host-next-action", prompt.action);
  text("host-next-action-note", prompt.actionNote);
  text("host-next-speech", prompt.next);
  text("host-updated-at", `сверено ${new Date(data.updated_at || Date.now()).toLocaleTimeString("ru-RU")}`);
  renderMetrics(data);
  renderAgenda(data);
  void hydrateAgenda(data);

  if (prompt.key !== promptSignature) {
    const live = byId("host-live");
    live.classList.remove("prompt-changed");
    void live.offsetWidth;
    live.classList.add("prompt-changed");
    if (promptSignature) globalThis.TModTabSignal?.pulse(`Новая реплика · ${prompt.label}`);
    promptSignature = prompt.key;
    if (autoScroll) live.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function setConnection(kind, label) {
  const element = byId("host-connection");
  element.className = kind;
  element.querySelector("b").textContent = label;
}

function timeoutSignal(milliseconds) {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return AbortSignal.timeout(milliseconds);
  }
  const controller = new AbortController();
  setTimeout(() => controller.abort(), milliseconds);
  return controller.signal;
}

function schedulePoll(delay) {
  clearTimeout(pollTimer);
  const next = delay ?? (state?.active ? 1500 : 5000);
  pollTimer = setTimeout(async () => {
    if (!document.hidden) await fetchState();
    schedulePoll();
  }, next);
}

async function fetchState() {
  if (fetching) return;
  fetching = true;
  try {
    const response = await fetch(`/api/state?mode=${encodeURIComponent(selectedMode)}`, {
      credentials: "same-origin",
      cache: "no-store",
      signal: timeoutSignal(10000),
    });
    if (response.status === 401) {
      window.location.replace("/login?next=/host");
      return;
    }
    if (response.status === 403) {
      throw new Error("forbidden");
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload.viewer?.authenticated) {
      window.location.replace("/login?next=/host");
      return;
    }
    render(payload);
    setConnection("online", "в эфире");
  } catch (error) {
    setConnection("offline", "нет связи");
    if (!state) {
      byId("host-gate").hidden = false;
      text("host-gate-title", "Не удалось открыть суфлёр");
      text("host-gate-copy", "Проверьте соединение и попробуйте снова. Основной пульт продолжает работать независимо.");
      byId("host-login-link").hidden = false;
    }
  } finally {
    fetching = false;
  }
}

async function copyText(value) {
  const content = String(value || "");
  let helper = null;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(content);
    } else {
      helper = document.createElement("textarea");
      helper.value = content;
      helper.style.position = "fixed";
      helper.style.left = "-9999px";
      document.body.append(helper);
      helper.select();
      if (!document.execCommand("copy")) throw new Error("copy_failed");
    }
    const toast = byId("host-toast");
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 1800);
  } catch {
    text("host-toast", "Не удалось скопировать");
    byId("host-toast").hidden = false;
  } finally {
    helper?.remove();
  }
}

function applyScale(next) {
  scriptScale = Math.max(0.72, Math.min(1.45, Number(next)));
  document.documentElement.style.setProperty("--script-scale", String(scriptScale));
  writeSetting("t-consensus-host-scale", String(scriptScale));
}

byId("host-copy-live").addEventListener("click", () => void copyText(byId("host-live-text").textContent));
byId("host-font-down").addEventListener("click", () => applyScale(scriptScale - 0.08));
byId("host-font-up").addEventListener("click", () => applyScale(scriptScale + 0.08));
byId("host-auto-scroll").addEventListener("click", () => {
  autoScroll = !autoScroll;
  writeSetting("t-consensus-host-follow", autoScroll ? "on" : "off");
  byId("host-auto-scroll").setAttribute("aria-pressed", String(autoScroll));
});
byId("host-fullscreen").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch {
    // Полноэкранный режим может быть запрещён настройками браузера.
  }
});

function updateClock() {
  text("host-moscow-clock", new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date()));
  if (state?.session) text("host-timer", formatTimer(state.session.timer_deadline));
}

applyScale(scriptScale);
byId("host-auto-scroll").setAttribute("aria-pressed", String(autoScroll));
updateClock();
clockTimer = setInterval(updateClock, 1000);
fetchState().finally(() => schedulePoll());

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(pollTimer);
  else {
    void fetchState();
    schedulePoll(500);
  }
});

window.addEventListener("pagehide", () => {
  clearTimeout(pollTimer);
  clearInterval(clockTimer);
}, { once: true });
