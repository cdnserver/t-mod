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
let commanding = false;
let fetchSequence = 0;
let appliedSequence = 0;
let pendingCommand = null;
const commandJournal = [];

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
const SCRIPT_VARIANT_STORAGE_KEY = "t-consensus-host-script-variants-v1";

function readJsonSetting(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : fallback;
  } catch {
    return fallback;
  }
}

function writeJsonSetting(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Выбор реплики — удобство ведущего, а не условие работы суфлёра.
  }
}

let scriptVariantChoices = readJsonSetting(SCRIPT_VARIANT_STORAGE_KEY, {});
let activePrompt = null;

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

const ACTIONS = {
  start_vote: { label: "Представить первый проект", note: "Зафиксировать состав и открыть представление", confirm: true },
  resend_invitations: { label: "Повторить приглашения", note: "Добавить вошедших позже и повторить доставку" },
  cancel_session: { label: "Отменить заседание", note: "Заседание завершится без итогового протокола", confirm: true, danger: true },
  open_vote: { label: "Поставить на воут", note: "Кнопки голосования станут доступны сенаторам" },
  leader_vote: { label: "Голос ведущего", note: "Позиция ведущего учитывается как обычный голос" },
  finalize_vote: { label: "Зафиксировать досрочно", note: "Приём голосов завершится немедленно", confirm: true, danger: true },
  retry_finalization: { label: "Повторить фиксацию", note: "Безопасно повторить незавершённую запись результата" },
  pause: { label: "Объявить паузу", note: "Стадия, проект и голоса будут сохранены", confirm: true },
  resume: { label: "Продолжить заседание", note: "T-Mod сначала проверит голосовой кворум" },
  choose_discussion: { label: "Открыть дискуссию", note: "Выберите предмет дискуссии", confirm: true },
  end_discussion: { label: "Завершить дискуссию", note: "Воут продолжится с сохранёнными позициями", confirm: true },
  veto: { label: "Применить право вето", note: "Особое необратимое решение будет внесено в протокол", confirm: true, danger: true },
  next_bill: { label: "Представить следующий проект", note: "Воут останется закрыт до отдельной команды" },
  finish_session: { label: "Завершить консенсус", note: "Сформировать официальный итог и протокол", confirm: true, danger: true },
};

const PRIMARY_BY_STAGE = {
  registration: "start_vote",
  presentation: "open_vote",
  finalizing: "retry_finalization",
  discussion_type: "choose_discussion",
  discussion: "end_discussion",
  paused: "resume",
  after_result: "next_bill",
};

const STAGE_RAIL = [
  ["registration", "Состав"],
  ["presentation", "Проект"],
  ["voting", "Воут"],
  ["discussion", "Дискуссия"],
  ["finalizing", "Фиксация"],
  ["after_result", "Результат"],
  ["next", "Повестка"],
  ["finished", "Протокол"],
];

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

function stableHash(value) {
  let hash = 2166136261;
  for (const character of String(value || "")) {
    hash ^= character.codePointAt(0) || 0;
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function stableVariantIndex(key, count) {
  return count > 0 ? stableHash(key) % count : 0;
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

function billIntroductionVariants(bill) {
  const number = formatNumber(bill?.bill_number);
  const title = cleanSpeech(bill?.title) || "Без названия";
  const author = cleanSpeech(bill?.author?.name) || "автор не указан";
  const essence = billEssence(bill);
  return [
    [
      `Рассматривается законопроект №${number} — «${title}».`,
      `Автор проекта — ${author}.`,
      `Предмет решения: ${essence}`,
      "Полный текст, приложения и первоначально поданный материал доступны в личных панелях и на трансляции.",
    ].join("\n\n"),
    [
      `Переходим к проекту №${number}: «${title}».`,
      `Проект представлен ${author}.`,
      `Для решения предлагается следующее: ${essence}`,
      "Прошу сверять формулировки с материалами, а не с кратким изложением на экране.",
    ].join("\n\n"),
    [
      `На рассмотрение вынесен законопроект №${number} «${title}».`,
      `Инициатор — ${author}.`,
      `Суть вопроса: ${essence}`,
      "После представления будет время для уточнений; до отдельного объявления воут не открывается.",
    ].join("\n\n"),
  ];
}

function billIntroduction(bill) {
  return billIntroductionVariants(bill)[0];
}

function voteOpeningVariants(bill) {
  const number = formatNumber(bill?.bill_number);
  return [
    `Законопроект №${number} поставлен на воут. Голосование открыто. Доступны три позиции: «За», «Против» и «Воздержаться». Прошу проверить название проекта перед подтверждением выбора.`,
    `Открываю голосование по законопроекту №${number}. Выберите «За», «Против» или «Воздержаться» в личном пульте T-Mod и подтвердите именно свою позицию.`,
    `Проект №${number} вынесен на голосование. Воут открыт для подтверждённого состава. Перед выбором ещё раз сверьте номер и название законопроекта.`,
  ];
}

function voteOpening(bill) {
  return voteOpeningVariants(bill)[0];
}

function resultSpeechVariants(result, bill) {
  const status = String(result?.status || "");
  const number = formatNumber(result?.bill_number || bill?.bill_number);
  const title = cleanSpeech(result?.title || bill?.title) || "Без названия";
  if (status === "vetoed") {
    return [
      `Система зафиксировала применение права вето к законопроекту №${number} — «${title}». Текущий проект завершён в особом порядке. Зафиксированный итог включается в официальный протокол без ручного повторения действия.`,
      `По проекту №${number} «${title}» применено право вето. Решение уже зафиксировано системой и будет отражено в официальном протоколе заседания.`,
    ];
  }
  const overall = formatPercent(result?.overall_percent);
  const opposed = formatPercent(result?.opposed_percent);
  const accepted = status === "accepted";
  const conclusion = accepted
    ? "В соответствии с действующим порядком законопроект принят."
    : "Установленный порог принятия не достигнут. Законопроект отклонён.";
  return [
    [
      "Приём голосов завершён. Результат зафиксирован системой.",
      `По законопроекту №${number} — «${title}» общий консенсус составил ${overall} процента за и ${opposed} процента против.`,
      conclusion,
      "Решение будет включено в официальный протокол заседания.",
    ].join("\n\n"),
    [
      `Оглашается итог по проекту №${number} «${title}».`,
      `Общий консенсус: ${overall}% за; против: ${opposed}%.`,
      conclusion,
      "Итоговая карточка и протокол являются официальной фиксацией решения.",
    ].join("\n\n"),
    [
      `Система завершила подсчёт по законопроекту №${number}.`,
      `По проекту «${title}» зафиксировано ${overall}% за и ${opposed}% против.`,
      conclusion,
      "Переход к следующему вопросу возможен после сверки итоговой карточки.",
    ].join("\n\n"),
  ];
}

function resultSpeech(result, bill) {
  return resultSpeechVariants(result, bill)[0];
}

function completedSpeechVariants(session) {
  const results = session?.results || [];
  const accepted = results.filter((item) => item.status === "accepted").length;
  const rejected = results.filter((item) => item.status === "rejected").length;
  const special = results.length - accepted - rejected;
  const summary = `Сегодня рассмотрено ${results.length} ${plural(results.length, "проект", "проекта", "проектов")}: принято — ${accepted}, отклонено — ${rejected}, завершено в особом порядке — ${special}.`;
  const closing = `${ordinal(session?.plenary_number)} пленарный консенсус Товарищества объявляется завершённым.`;
  return [
    [
      "Сенаторы Товарищества. Повестка исчерпана, результаты сохранены, официальный протокол сформирован системой.",
      summary,
      "Благодарю участников за точность позиции, соблюдение порядка и ответственность перед общим решением.",
      closing,
      "Товарищество — светлый круг. Заседание окончено.",
    ].join("\n\n"),
    [
      "Работа по повестке завершена. Все решения внесены в протокол T-Mod.",
      summary,
      "Спасибо за собранность, аргументированность и уважение к общей процедуре.",
      `${closing} Светлый круг остаётся в работе.`,
    ].join("\n\n"),
    [
      "Итоги заседания зафиксированы, повестка закрыта.",
      summary,
      "Официальные материалы доступны в карточках решений и итоговом протоколе.",
      `${closing} Благодарю всех участников.`,
    ].join("\n\n"),
  ];
}

function completedSpeech(session) {
  return completedSpeechVariants(session)[0];
}

function promptSpeechVariants(prompt, data) {
  const session = data?.session || null;
  const schedule = data?.schedule || null;
  const bill = session?.current_bill || null;
  const quorum = session?.quorum || {};
  const voting = session?.voting || {};
  const result = session?.current_result || (session?.results || []).at(-1) || null;
  const number = formatNumber(bill?.bill_number);

  switch (prompt.stage) {
    case "completed":
      return completedSpeechVariants(data?.last_session || session);
    case "scheduled": {
      const plenary = ordinal(schedule?.plenary_number).toLowerCase();
      const count = Number(data?.queue?.length || 0);
      const moment = formatMoment(schedule?.scheduled_for);
      return [
        prompt.speech,
        [
          `Объявляется созыв ${plenary} пленарного консенсуса Товарищества.`,
          `Заседание начнётся ${moment} по времени Товарищества. В повестке — ${count} ${plural(count, "проект", "проекта", "проектов")}.`,
          "Прошу заранее сверить материалы, личный пульт и возможность присутствия.",
        ].join("\n\n"),
        [
          `Товарищество готовится к ${plenary} пленарному консенсусу.`,
          `Начало назначено на ${moment}. К рассмотрению подготовлено ${count} ${plural(count, "вопрос", "вопроса", "вопросов")}.`,
          "До регистрации ознакомьтесь с повесткой и подтвердите участие через T-Mod.",
        ].join("\n\n"),
      ];
    }
    case "idle":
      return [
        prompt.speech,
        "План следующего пленарного консенсуса ещё не опубликован. После назначения даты суфлёр автоматически подготовит повестку, приглашение и последовательность действий ведущего.",
      ];
    case "registration":
      if (quorum.ready) {
        return [
          prompt.speech,
          `Кворум подтверждён: ${quorum.confirmed || 0} участников. Перед переходом к повестке прошу сообщить только существенные возражения по составу и готовности.`,
          `Состав для заседания собран — ${quorum.confirmed || 0} участников. Если процедурных замечаний нет, переходим к первому вопросу повестки.`,
        ];
      }
      return [
        prompt.speech,
        `Переходим к подтверждению состава ${ordinal(session?.plenary_number).toLowerCase()} пленарного консенсуса. Сейчас в кворуме ${quorum.confirmed || 0} из ${quorum.invited || 0}. Прошу подтвердить участие через личный пульт T-Mod.`,
        `Регистрация продолжается. Подтверждено ${quorum.confirmed || 0} из ${quorum.invited || 0} участников. До набора кворума повестка не открывается.`,
      ];
    case "presentation":
      return billIntroductionVariants(bill);
    case "voting":
      return voteOpeningVariants(bill).map((opening) => `${opening}\n\nГолосование продолжается. Принято ${voting.received || 0} из ${voting.expected || 0} бюллетеней.`);
    case "discussion_type":
      return [
        prompt.speech,
        `По законопроекту №${number} поступил запрос на дискуссию. Воут временно остановлен; ранее подтверждённые позиции сохранены. Сейчас определим предмет обсуждения.`,
        `Переходим к вопросу о дискуссии по проекту №${number}. Голосование приостановлено процедурно и возобновится только после завершения обсуждения.`,
      ];
    case "discussion": {
      const kind = (session?.discussion?.type || "иная").toLowerCase();
      return [
        prompt.speech,
        `Открыта ${kind} дискуссия по проекту №${number}. Прошу формулировать новые существенные доводы кратко, по существу и с отделением фактов от оценки.`,
        `По законопроекту №${number} идёт ${kind} дискуссия. Слово предоставляется по очереди; повторённые аргументы не требуют повторного изложения.`,
      ];
    }
    case "paused": {
      const reason = cleanSpeech(session?.pause_reason) || "техническая проверка";
      return [
        prompt.speech,
        `Объявлена процедурная пауза: ${reason}. Состояние проекта и все принятые системой действия сохранены. О продолжении будет объявлено отдельно.`,
      ];
    }
    case "finalizing":
      return [
        prompt.speech,
        `Голосование по проекту №${number} завершено. T-Mod фиксирует результат; до появления итоговой карточки не оглашаются ни проценты, ни решение.`,
        `Идёт защищённая фиксация результата по законопроекту №${number}. Прошу дождаться итоговой карточки и не повторять команды.`,
      ];
    case "after_result":
      return resultSpeechVariants(result, bill);
    default:
      return [prompt.speech];
  }
}

function selectedPromptVariant(baseKey, count) {
  const chosen = Number(scriptVariantChoices[baseKey]);
  if (Number.isInteger(chosen) && chosen >= 0 && chosen < count) return chosen;
  return stableVariantIndex(baseKey, count);
}

function choosePromptVariant(prompt, data) {
  const alternatives = promptSpeechVariants(prompt, data)
    .map((value) => String(value || "").trim())
    .filter((value, index, all) => value && all.indexOf(value) === index);
  const baseKey = String(prompt.key || "idle");
  const variantIndex = selectedPromptVariant(baseKey, alternatives.length);
  return {
    ...prompt,
    baseKey,
    key: `${baseKey}:variant:${variantIndex}`,
    speech: alternatives[variantIndex] || prompt.speech,
    variantIndex,
    variantCount: alternatives.length,
  };
}

function advancePromptVariant() {
  if (!activePrompt || activePrompt.variantCount < 2 || !state) return;
  const next = (activePrompt.variantIndex + 1) % activePrompt.variantCount;
  scriptVariantChoices = { ...scriptVariantChoices, [activePrompt.baseKey]: next };
  const keys = Object.keys(scriptVariantChoices);
  if (keys.length > 120) delete scriptVariantChoices[keys[0]];
  writeJsonSetting(SCRIPT_VARIANT_STORAGE_KEY, scriptVariantChoices);
  render(state);
  showToast(`Выбран вариант ${next + 1} из ${activePrompt.variantCount}`);
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
  const seed = `agenda:${bill?.id || bill?.bill_number || 0}`;
  const introductions = billIntroductionVariants(bill);
  const openings = voteOpeningVariants(bill);
  const presentation = introductions[stableVariantIndex(`${seed}:presentation`, introductions.length)];
  const vote = openings[stableVariantIndex(`${seed}:vote`, openings.length)];
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

function addJournal(message, kind = "ok") {
  commandJournal.unshift({
    message: String(message || "Состояние обновлено"),
    kind,
    at: new Date(),
  });
  commandJournal.splice(20);
  const list = byId("host-command-log");
  list.replaceChildren();
  commandJournal.forEach((entry) => {
    const item = node("li", entry.kind === "error" ? "error" : "");
    item.append(
      node("time", "", entry.at.toLocaleTimeString("ru-RU")),
      node("span", "", entry.message),
    );
    list.append(item);
  });
  text("host-command-count", commandJournal.length);
}

function currentBillId(data = state) {
  return Number(data?.session?.current_bill?.id || 0) || null;
}

function isStateOlder(candidate, current) {
  const nextSession = candidate?.session;
  const activeSession = current?.session;
  if (!nextSession || !activeSession) return false;
  if (String(nextSession.key) !== String(activeSession.key)) return false;
  return Number(nextSession.revision || 0) < Number(activeSession.revision || 0);
}

function renderStageRail(data) {
  const rail = byId("host-stage-rail");
  const session = data.session;
  const stage = String(session?.stage || (data.schedule ? "scheduled" : "idle"));
  const results = session?.results || [];
  rail.replaceChildren();
  STAGE_RAIL.forEach(([key, label], index) => {
    const item = node("div", "host-stage-step");
    const aliases = key === "discussion" ? ["discussion", "discussion_type"] : [key];
    const current = aliases.includes(stage);
    let status = "ожидает";
    let done = false;
    if (key === "registration") done = Boolean(session && stage !== "registration");
    if (key === "presentation") done = Boolean(session?.current_bill && !["registration", "presentation"].includes(stage));
    if (key === "voting") done = Boolean(results.length && stage === "after_result");
    if (key === "discussion") status = current ? "сейчас" : "по запросу";
    if (key === "finalizing") done = Boolean(results.length && stage === "after_result");
    if (key === "after_result") done = results.length > 0 && stage !== "after_result";
    if (key === "next") status = `${Number(data.queue?.length || 0)} впереди`;
    if (key === "finished") done = Boolean(session?.finished || stage === "finished");
    if (current) {
      item.classList.add("current");
      status = "сейчас";
    } else if (done) {
      item.classList.add("done");
      status = "зафиксировано";
    }
    item.append(node("span", "", `${String(index + 1).padStart(2, "0")} · ${label}`), node("small", "", status));
    rail.append(item);
  });
}

function renderRoster(data) {
  const roster = byId("host-roster");
  const participants = data.session?.participants || [];
  const confirmed = participants.filter((item) => item.confirmed).length;
  text("host-roster-summary", `${confirmed} / ${participants.length}`);
  roster.replaceChildren();
  if (!participants.length) {
    roster.append(node("div", "host-roster-empty", "Состав появится после подготовки заседания."));
    return;
  }
  participants.forEach((participant) => {
    const item = node(
      "div",
      `host-roster-item${participant.confirmed ? " confirmed" : ""}${participant.voted ? " voted" : ""}`,
    );
    const identity = node("div", "");
    identity.append(
      node("strong", "", participant.name || "Участник"),
      node("small", "", `${participant.kind || "Участник"} · ${participant.dm_ready ? "пульт доставлен" : "доставка ожидается"}`),
    );
    let marker = participant.confirmed ? "подтверждён" : "ожидается";
    if (participant.voted) {
      marker = participant.vote
        ? `голос: ${{ yes: "за", no: "против", abstain: "воздержался" }[participant.vote] || "принят"}`
        : "голос принят";
    }
    item.append(node("i", ""), identity, node("span", "", marker));
    roster.append(item);
  });
}

function renderTelemetry(data) {
  const session = data.session;
  const updated = new Date(data.updated_at || Date.now());
  const age = Math.max(0, Math.round((Date.now() - updated.getTime()) / 1000));
  text("host-sync-age", age < 2 ? "сейчас" : `${age} сек назад`);
  byId("host-sync-badge").className = `host-sync-badge${age > 8 ? " stale" : ""}`;
  text("host-revision", session ? `r${Number(session.revision || 0)}` : "—");
  text("host-session-key", session?.key ? String(session.key).slice(0, 10) : "—");
  text("host-cache-state", String(data.cache_state || "live"));
  text("host-leader-name", session?.leader?.name || data.schedule?.host?.name || "—");
  const integrity = String(session?.integrity?.status || "nominal");
  text(
    "host-integrity-state",
    integrity === "critical" ? "СТОП" : integrity === "warning" ? "внимание" : "норма",
  );
  byId("host-integrity-state").dataset.status = integrity;
  const canSettings = Boolean((data.capabilities || []).includes("update_session_settings") && data.schedule);
  byId("host-session-settings").disabled = commanding || !canSettings;
}

function actionPayload(action) {
  if (action === "start_vote") return { confirm_current_roster: true };
  if (["cancel_session", "finalize_vote", "veto", "finish_session"].includes(action)) return { confirm: true };
  if (action === "pause") return { reason: "Процедурная пауза объявлена ведущим через единый пульт." };
  return {};
}

function makeCommandButton(action, { primary = false, payload = null, label = null } = {}) {
  const meta = ACTIONS[action] || { label: action, note: "Команда заседания" };
  const button = node("button", meta.danger ? "danger" : "");
  button.type = "button";
  button.dataset.consensusCommand = action;
  button.disabled = commanding;
  if (primary) {
    button.className = `host-primary-command${commanding ? " busy" : ""}`;
    button.append(node("span", "", commanding ? "Команда выполняется" : (label || meta.label)), node("small", "", commanding ? "Ждём подтверждение живого состояния" : meta.note));
  } else {
    button.textContent = label || meta.label;
    button.title = meta.note;
  }
  button.addEventListener("click", () => requestCommand(action, payload || actionPayload(action)));
  return button;
}

function choosePrimaryAction(data) {
  const capabilities = new Set(data.capabilities || []);
  const stage = String(data.session?.stage || "");
  if (stage === "voting") {
    const received = Number(data.session?.voting?.received || 0);
    const expected = Number(data.session?.voting?.expected || 0);
    return capabilities.has("finalize_vote") && expected > 0 && received >= expected ? "finalize_vote" : null;
  }
  const selected = PRIMARY_BY_STAGE[stage];
  if (selected === "next_bill" && !Number(data.queue?.length || 0) && capabilities.has("finish_session")) return "finish_session";
  return selected && capabilities.has(selected) ? selected : null;
}

function renderControls(data) {
  const capabilities = new Set(data.capabilities || []);
  const core = byId("host-secondary-controls");
  const existingPrimary = byId("host-primary-command");
  const primaryAction = choosePrimaryAction(data);
  const primary = primaryAction
    ? makeCommandButton(primaryAction, { primary: true })
    : makeCommandButton("noop", { primary: true, label: data.session?.stage === "voting" ? "Голосование идёт" : "Нет активной команды" });
  primary.id = "host-primary-command";
  if (!primaryAction) {
    primary.disabled = true;
    primary.dataset.consensusCommand = "";
    const small = primary.querySelector("small");
    if (small) small.textContent = data.session?.stage === "voting"
      ? "Следим за составом, голосами и таймером"
      : "Пульт ожидает следующего состояния";
  }
  existingPrimary.replaceWith(primary);
  core.replaceChildren();

  const secondaryOrder = [
    "resend_invitations", "pause", "resume", "retry_finalization",
    "next_bill", "finish_session", "cancel_session", "veto",
  ];
  secondaryOrder.forEach((action) => {
    if (capabilities.has(action) && action !== primaryAction) core.append(makeCommandButton(action));
  });
  if (capabilities.has("leader_vote")) {
    [["yes", "Голос: за"], ["no", "Голос: против"], ["abstain", "Воздержаться"]].forEach(([vote, label]) => {
      core.append(makeCommandButton("leader_vote", { payload: { vote }, label }));
    });
  }
  if (capabilities.has("finalize_vote") && primaryAction !== "finalize_vote") {
    core.append(makeCommandButton("finalize_vote"));
  }
  text("host-command-guidance", primaryAction
    ? ACTIONS[primaryAction].note
    : data.session?.stage === "voting"
      ? "Состояние обновляется автоматически. Позиции сенаторов скрыты от всех, кроме ведущего."
      : "Доступные команды вычисляются из фактической стадии, а не из истории на экране.");

  const timerEnabled = capabilities.has("set_timer") && !commanding;
  byId("host-timer-buttons").querySelectorAll("button").forEach((button) => { button.disabled = !timerEnabled; });
}

function renderMachine(data) {
  renderStageRail(data);
  renderControls(data);
  renderRoster(data);
  renderTelemetry(data);
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
  const prompt = choosePromptVariant(derivePrompt(data), data);
  activePrompt = prompt;
  const session = data.session;
  const schedule = data.schedule;
  const plenary = session?.plenary_number || schedule?.plenary_number || data.last_session?.plenary_number || 0;
  text("host-mode", data.mode === "simulation" ? "УЧЕБНЫЙ КОНТУР" : "РАБОЧИЙ КОНТУР");
  text(
    "host-session-title",
    cleanSpeech(schedule?.title)
      || (plenary ? `${ordinal(plenary)} пленарный консенсус` : "Пленарный консенсус"),
  );
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
  const alternateButton = byId("host-alternate-live");
  alternateButton.disabled = prompt.variantCount < 2;
  alternateButton.textContent = prompt.variantCount > 1
    ? `Другой вариант · ${prompt.variantIndex + 1}/${prompt.variantCount}`
    : "Одна формула";
  alternateButton.title = prompt.variantCount > 1
    ? "Показать другую утверждённую формулировку без изменения состояния заседания"
    : "Для этой процедурной формулы варианта нет";
  text("host-next-action", prompt.action);
  text("host-next-action-note", prompt.actionNote);
  text("host-next-speech", prompt.next);
  text("host-updated-at", `сверено ${new Date(data.updated_at || Date.now()).toLocaleTimeString("ru-RU")}`);
  renderMetrics(data);
  renderMachine(data);
  renderAgenda(data);
  void hydrateAgenda(data);
  updateClock();

  if (prompt.key !== promptSignature) {
    const hadPrompt = Boolean(promptSignature);
    const live = byId("host-live");
    live.classList.remove("prompt-changed");
    void live.offsetWidth;
    live.classList.add("prompt-changed");
    if (promptSignature) globalThis.TModTabSignal?.pulse(`Новая реплика · ${prompt.label}`);
    promptSignature = prompt.key;
    if (autoScroll && hadPrompt) live.scrollIntoView({ behavior: "smooth", block: "start" });
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

async function fetchState({ force = false } = {}) {
  if (fetching && !force) return;
  const sequence = ++fetchSequence;
  fetching = true;
  try {
    const freshness = force ? "&fresh=1" : "";
    const response = await fetch(`/api/state?mode=${encodeURIComponent(selectedMode)}${freshness}`, {
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
    if (sequence < appliedSequence || isStateOlder(payload, state)) return;
    appliedSequence = sequence;
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
    if (sequence === fetchSequence) fetching = false;
  }
}

function showToast(message, kind = "ok") {
  const toast = byId("host-toast");
  text("host-toast", message);
  toast.dataset.kind = kind;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
}

function confirmExtra(action, payload) {
  const container = byId("host-confirm-extra");
  container.replaceChildren();
  if (action === "choose_discussion") {
    const label = node("label", "", "Тип дискуссии");
    const select = document.createElement("select");
    select.id = "host-discussion-type";
    ["Правовая", "Фактическая", "Процедурная", "Иная"].forEach((value) => {
      const option = node("option", "", value);
      option.value = value;
      select.append(option);
    });
    label.append(select);
    container.append(label);
  }
  if (action === "pause") {
    const label = node("label", "", "Причина паузы");
    const input = document.createElement("input");
    input.id = "host-pause-reason";
    input.type = "text";
    input.maxLength = 300;
    input.value = payload.reason || "Процедурная пауза";
    label.append(input);
    container.append(label);
  }
  if (action === "start_vote") {
    container.append(node("p", "host-dialog-warning", "Начало фиксирует текущий подтверждённый состав. Неподтвердившиеся участники не войдут в кворум автоматически."));
  }
}

function requestCommand(action, payload = {}) {
  if (commanding || !state) return;
  const meta = ACTIONS[action] || { label: action, note: "Команда заседания" };
  if (meta.confirm) {
    pendingCommand = { action, payload: { ...payload } };
    text("host-confirm-title", meta.label);
    text("host-confirm-copy", meta.note);
    confirmExtra(action, payload);
    byId("host-confirm-submit").classList.toggle("danger", Boolean(meta.danger));
    byId("host-confirm-dialog").showModal();
    return;
  }
  void executeCommand(action, payload);
}

async function executeCommand(action, payload = {}) {
  const snapshot = state;
  const session = snapshot?.session;
  if (commanding || !snapshot?.viewer?.csrf_token) return;
  commanding = true;
  renderControls(snapshot);
  const meta = ACTIONS[action] || { label: action };
  addJournal(`${meta.label}: команда отправлена`, "pending");
  try {
    const response = await fetch("/api/command", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": snapshot.viewer.csrf_token,
        "X-Idempotency-Key": globalThis.crypto?.randomUUID?.() || `host-${Date.now()}-${Math.random()}`,
      },
      body: JSON.stringify({
        mode: selectedMode,
        action,
        session_key: String(session?.key || ""),
        revision: Number(session?.revision || 0),
        bill_id: currentBillId(snapshot),
        payload,
      }),
      signal: timeoutSignal(18000),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(result.message || `Команда не выполнена · HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    if (result.state && !isStateOlder(result.state, state)) render(result.state);
    addJournal(result.message || `${meta.label}: выполнено`);
    showToast(result.message || "Команда выполнена");
    globalThis.TModTabSignal?.pulse(meta.label);
    window.setTimeout(() => void fetchState({ force: true }), 220);
  } catch (error) {
    const message = error?.name === "TimeoutError"
      ? "Ответ задерживается. Пульт сверяет фактическое состояние — не повторяйте команду."
      : String(error?.message || "Команда не выполнена");
    addJournal(message, "error");
    showToast(message, "error");
    await fetchState({ force: true });
  } finally {
    commanding = false;
    if (state) renderControls(state);
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
byId("host-alternate-live").addEventListener("click", advancePromptVariant);
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

byId("host-confirm-submit").addEventListener("click", () => {
  if (!pendingCommand) return;
  const command = pendingCommand;
  if (command.action === "choose_discussion") {
    command.payload.discussion_type = document.querySelector("#host-discussion-type")?.value || "Иная";
  }
  if (command.action === "pause") {
    command.payload.reason = document.querySelector("#host-pause-reason")?.value || "Процедурная пауза";
  }
  pendingCommand = null;
  byId("host-confirm-dialog").close();
  void executeCommand(command.action, command.payload);
});

byId("host-confirm-dialog").addEventListener("close", () => { pendingCommand = null; });

byId("host-timer-buttons").addEventListener("click", (event) => {
  const button = event.target.closest("[data-timer-seconds]");
  if (!button) return;
  const seconds = Number(button.dataset.timerSeconds || 0);
  if (seconds > 0) void executeCommand("set_timer", { seconds, mode: "extend" });
});

byId("host-custom-timer").addEventListener("click", () => {
  byId("host-timer-dialog").showModal();
});

byId("host-timer-submit").addEventListener("click", () => {
  const seconds = (Number(byId("host-timer-minutes").value || 0) * 60)
    + Number(byId("host-timer-seconds").value || 0);
  if (seconds < 15 || seconds > 7200) {
    showToast("Укажите от 15 секунд до 2 часов", "error");
    return;
  }
  const mode = document.querySelector('input[name="host-timer-operation"]:checked')?.value || "extend";
  byId("host-timer-dialog").close();
  void executeCommand("set_timer", { seconds, mode });
});

byId("host-session-settings").addEventListener("click", () => {
  if (!state?.schedule) return;
  byId("host-settings-title").value = state.schedule.title || "";
  byId("host-settings-duration").value = String(state.schedule.duration_minutes || 90);
  byId("host-settings-dialog").showModal();
});

byId("host-settings-submit").addEventListener("click", () => {
  const title = byId("host-settings-title").value.trim();
  const duration = Number(byId("host-settings-duration").value || 0);
  if (title.length < 3 || title.length > 100 || duration < 15 || duration > 480) {
    showToast("Проверьте название и длительность заседания", "error");
    return;
  }
  const scheduleRevision = Number(state?.schedule?.revision || 0);
  byId("host-settings-dialog").close();
  void executeCommand("update_session_settings", {
    title,
    duration_minutes: duration,
    schedule_revision: scheduleRevision,
  });
});

byId("host-refresh").addEventListener("click", () => void fetchState({ force: true }));

function updateClock() {
  text("host-moscow-clock", new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date()));
  if (state?.session) {
    const timer = formatTimer(state.session.timer_deadline);
    text("host-timer", timer);
    text("host-timer-large", state.session.timer_deadline ? timer : "--:--");
    const remaining = state.session.timer_deadline
      ? Math.max(0, Math.ceil((new Date(state.session.timer_deadline).getTime() - Date.now()) / 1000))
      : null;
    byId("host-timer-large").closest(".host-timer-face")?.classList.toggle("urgent", remaining !== null && remaining <= 30);
    text(
      "host-timer-mode",
      remaining === null
        ? "не запущен"
        : state.session.timer_added_seconds
          ? `продлён суммарно на ${state.session.timer_added_seconds} сек`
          : "отсчёт синхронизирован",
    );
    if (state.updated_at) {
      const age = Math.max(0, Math.round((Date.now() - new Date(state.updated_at).getTime()) / 1000));
      text("host-sync-age", age < 2 ? "сейчас" : `${age} сек назад`);
      byId("host-sync-badge").classList.toggle("stale", age > 8);
    }
  } else {
    text("host-timer-large", "--:--");
    text("host-timer-mode", "не запущен");
  }
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
