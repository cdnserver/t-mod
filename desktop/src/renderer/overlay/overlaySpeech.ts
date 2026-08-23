const SENTENCE_BOUNDARY = /[.!?…](?:[\s\n]|$)|\n{2,}/g;

export function sanitizeOverlaySpeech(value: string): string {
  return String(value || "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
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

export class IncrementalRussianSpeech {
  private buffer = "";
  private enabled = true;
  private rate = 1.08;
  private volume = 0.86;
  private readonly synthesis: SpeechSynthesis | undefined;

  constructor(synthesis = globalThis.speechSynthesis) {
    this.synthesis = synthesis;
  }

  configure(options: { enabled: boolean; rate: number; volume: number }): void {
    this.enabled = options.enabled;
    this.rate = Math.max(0.75, Math.min(1.45, options.rate));
    this.volume = Math.max(0, Math.min(1, options.volume));
    if (!this.enabled) this.cancel();
  }

  append(delta: string): void {
    if (!this.enabled || !this.synthesis) return;
    this.buffer += delta;
    this.flushSentences(false);
  }

  finish(): void {
    this.flushSentences(true);
  }

  cancel(): void {
    this.buffer = "";
    this.synthesis?.cancel();
  }

  private flushSentences(force: boolean): void {
    let boundary = 0;
    for (const match of this.buffer.matchAll(SENTENCE_BOUNDARY)) {
      const end = (match.index || 0) + match[0].length;
      if (end >= 28) boundary = end;
    }
    if (!boundary && this.buffer.length > 260) {
      boundary = this.buffer.lastIndexOf(" ", 220);
    }
    if (force) boundary = this.buffer.length;
    if (boundary <= 0) return;
    const phrase = sanitizeOverlaySpeech(this.buffer.slice(0, boundary));
    this.buffer = this.buffer.slice(boundary);
    if (!phrase) return;
    const utterance = new SpeechSynthesisUtterance(phrase.slice(0, 1_200));
    utterance.lang = "ru-RU";
    utterance.rate = this.rate;
    utterance.pitch = 0.96;
    utterance.volume = this.volume;
    const voices = this.synthesis?.getVoices() || [];
    utterance.voice = voices.find((voice) => /^ru(?:-|_)/i.test(voice.lang)) || null;
    this.synthesis?.speak(utterance);
  }
}
