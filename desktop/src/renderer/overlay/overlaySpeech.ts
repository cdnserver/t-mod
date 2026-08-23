const SENTENCE_BOUNDARY = /(?:[.!?…]+[”»"')\]]*|\n{2,})(?=\s|$)/g;
const SOFT_BOUNDARY = /[,;:—–-](?=\s)|\s+/g;
const MIN_STREAMING_PHRASE_LENGTH = 14;
const MAX_STREAMING_PHRASE_LENGTH = 280;

const RUSSIAN_KEYBOARD_LAYOUT: Readonly<Record<string, string>> = {
  q: "й", w: "ц", e: "у", r: "к", t: "е", y: "н", u: "г", i: "ш", o: "щ", p: "з",
  a: "ф", s: "ы", d: "в", f: "а", g: "п", h: "р", j: "о", k: "л", l: "д",
  z: "я", x: "ч", c: "с", v: "м", b: "и", n: "т", m: "ь",
};

const COMMON_RUSSIAN_WORDS = new Set([
  "а", "без", "будет", "вам", "вас", "вы", "где", "дела", "для", "если", "есть", "же",
  "закон", "зачем", "здравствуйте", "и", "или", "как", "какие", "когда", "кто", "ли",
  "можно", "мне", "надо", "нет", "нужно", "ответ", "пожалуйста", "помоги", "почему", "право",
  "привет", "про", "разрешено", "с", "сейчас", "спасибо", "так", "что", "это", "я",
]);

const COMMON_ENGLISH_WORDS = new Set([
  "admin", "ai", "atlas", "case", "discord", "fps", "game", "gta", "hello", "help", "hey", "hi",
  "ping", "please", "report", "server", "test", "thanks", "thank", "the", "this", "tmod", "update",
  "what", "where", "why", "yes", "no",
]);

/**
 * Strip content that is useful on-screen but distracting or unsafe to read
 * aloud. Keeping this pure makes the streaming queue deterministic.
 */
export function sanitizeOverlaySpeech(value: string): string {
  return String(value || "")
    .replace(/\`\`\`[\s\S]*?\`\`\`/g, " ")
    .replace(/\`([^\`]+)\`/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\((?:https?:\/\/|\/)[^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/gi, " ")
    .replace(/\[(?:Источник\s*)?\d+(?:\s*[,;]\s*\d+)*\]/gi, " ")
    .replace(/^\s{0,3}[#>*+-]+\s*/gm, "")
    .replace(/[|*_~]/g, "")
    .replace(/[\u0000-\u001F\u007F]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function uppercaseLike(source: string, replacement: string): string {
  return source === source.toUpperCase() ? replacement.toUpperCase() : replacement;
}

function mapRussianKeyboardLayout(value: string): string {
  return value.replace(/[a-z]/gi, (letter) => {
    const mapped = RUSSIAN_KEYBOARD_LAYOUT[letter.toLowerCase()];
    return mapped ? uppercaseLike(letter, mapped) : letter;
  });
}

function russianLayoutScore(value: string): number {
  const words = value.toLowerCase().match(/[а-яё]+/g) || [];
  if (!words.length) return 0;
  let score = 0;
  for (const word of words) {
    if (COMMON_RUSSIAN_WORDS.has(word)) score += 5;
    if (/[аеёиоуыэюя]/.test(word)) score += 0.4;
    if (/(?:ст|но|то|на|ен|ов|ни|ра|ко|ро|по|пр|ве|ет|ка|де|ла|мо|же|те|ли|что)/.test(word)) {
      score += 0.8;
    }
    if (/(?:ать|ять|ить|еть|ого|ему|ами|ями|ость|ение|ство)$/.test(word)) score += 0.5;
  }
  return score;
}

/**
 * Converts a question typed in an English keyboard layout into Russian only
 * when the result has clear Russian-language signals. URLs, e-mail addresses,
 * mixed Cyrillic text and recognizable English requests remain untouched.
 */
export function normalizeRussianKeyboardInput(value: string): string {
  const source = String(value || "");
  if (
    !source.trim() ||
    /[а-яё]/i.test(source) ||
    /(?:https?:\/\/|www\.|@|\.[a-z]{2,}\b)/i.test(source)
  ) return source;

  const latinWords = source.toLowerCase().match(/[a-z]+/g) || [];
  const letters = source.match(/[a-z]/gi) || [];
  if (letters.length < 3 || !latinWords.length) return source;
  if (latinWords.some((word) => COMMON_ENGLISH_WORDS.has(word))) return source;

  const mapped = mapRussianKeyboardLayout(source);
  const russianWords = mapped.toLowerCase().match(/[а-яё]+/g) || [];
  const knownWords = russianWords.filter((word) => COMMON_RUSSIAN_WORDS.has(word)).length;
  const score = russianLayoutScore(mapped);
  const threshold = latinWords.length > 1 ? 3.5 : 4.5;
  return knownWords > 0 || score >= threshold ? mapped : source;
}

function isAbbreviationBoundary(value: string, end: number): boolean {
  const tail = value.slice(Math.max(0, end - 12), end).toLowerCase();
  return /(?:^|\s)(?:ст|п|г|т\.?\s*д|т\.?\s*п|и\.?\s*т\.?\s*д)\.$/.test(tail);
}

function lastSoftBoundary(value: string, before: number): number {
  let boundary = 0;
  for (const match of value.matchAll(SOFT_BOUNDARY)) {
    const end = (match.index || 0) + match[0].length;
    if (end > before) break;
    boundary = end;
  }
  return boundary;
}

export class IncrementalRussianSpeech {
  private buffer = "";
  private enabled = true;
  private rate = 1.08;
  private volume = 0.86;
  private voiceName = "";
  private readonly synthesis: SpeechSynthesis | undefined;

  constructor(synthesis = globalThis.speechSynthesis) {
    this.synthesis = synthesis;
  }

  configure(options: { enabled: boolean; rate: number; volume: number; voiceName?: string }): void {
    this.enabled = options.enabled;
    this.rate = Math.max(0.75, Math.min(1.45, options.rate));
    this.volume = Math.max(0, Math.min(1, options.volume));
    this.voiceName = String(options.voiceName || "");
    if (!this.enabled) this.cancel();
  }

  append(delta: string): void {
    if (!this.enabled || !this.synthesis) return;
    this.buffer += String(delta || "");
    this.flushSentences(false);
  }

  finish(): void {
    if (!this.enabled || !this.synthesis) return;
    this.flushSentences(true);
  }

  cancel(): void {
    this.buffer = "";
    this.synthesis?.cancel();
  }

  /**
   * This deliberately bypasses the enabled flag: callers use it only after
   * the main process explicitly reports that AI audio is unavailable.
   */
  speakFallback(value: string): Promise<void> {
    if (!this.synthesis) return Promise.resolve();
    this.cancel();
    const phrases = this.toPhrases(value);
    if (!phrases.length) return Promise.resolve();
    return new Promise((resolve) => {
      phrases.forEach((phrase, index) => {
        const utterance = this.createUtterance(phrase);
        if (index === phrases.length - 1) {
          utterance.onend = () => resolve();
          utterance.onerror = () => resolve();
        }
        this.synthesis?.speak(utterance);
      });
    });
  }

  // Kept as a compatibility alias for preview and test callers.
  speakNow(value: string): void {
    void this.speakFallback(value);
  }

  private flushSentences(force: boolean): void {
    for (;;) {
      const boundary = this.nextBoundary(this.buffer, force);
      if (!boundary) return;
      const phrase = sanitizeOverlaySpeech(this.buffer.slice(0, boundary));
      this.buffer = this.buffer.slice(boundary);
      if (phrase) this.synthesis?.speak(this.createUtterance(phrase.slice(0, MAX_STREAMING_PHRASE_LENGTH)));
      if (!force) return;
    }
  }

  private toPhrases(value: string): string[] {
    let remaining = sanitizeOverlaySpeech(value);
    const phrases: string[] = [];
    while (remaining) {
      const boundary = this.nextBoundary(remaining, true);
      if (!boundary) break;
      const phrase = sanitizeOverlaySpeech(remaining.slice(0, boundary));
      remaining = remaining.slice(boundary);
      if (phrase) phrases.push(phrase.slice(0, MAX_STREAMING_PHRASE_LENGTH));
    }
    return phrases;
  }

  private nextBoundary(value: string, force: boolean): number {
    let sentenceBoundary = 0;
    for (const match of value.matchAll(SENTENCE_BOUNDARY)) {
      const end = (match.index || 0) + match[0].length;
      if (isAbbreviationBoundary(value, end)) continue;
      if (end >= MIN_STREAMING_PHRASE_LENGTH) {
        sentenceBoundary = end;
        break;
      }
    }
    if (sentenceBoundary) return sentenceBoundary;

    if (value.length > MAX_STREAMING_PHRASE_LENGTH) {
      return lastSoftBoundary(value, MAX_STREAMING_PHRASE_LENGTH)
        || value.lastIndexOf(" ", MAX_STREAMING_PHRASE_LENGTH)
        || MAX_STREAMING_PHRASE_LENGTH;
    }
    return force ? value.length : 0;
  }

  private createUtterance(phrase: string): SpeechSynthesisUtterance {
    const utterance = new SpeechSynthesisUtterance(phrase);
    utterance.lang = "ru-RU";
    utterance.rate = this.rate;
    utterance.pitch = 0.96;
    utterance.volume = this.volume;
    const voices = this.synthesis?.getVoices() || [];
    utterance.voice = voices.find((voice) =>
      voice.voiceURI === this.voiceName || voice.name === this.voiceName,
    ) || voices.find((voice) => /^ru(?:-|_)/i.test(voice.lang)) || null;
    return utterance;
  }
}
