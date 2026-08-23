const SENTENCE_BOUNDARY = /(?:[.!?…]+[”»"')\]]*|\n{2,})(?=\s|$)/g;
const SOFT_BOUNDARY = /[,;:—–-](?=\s)|\s+/g;
const MIN_PHRASE_LENGTH = 18;

export interface AtlasOverlaySpeechSplit {
  phrases: string[];
  remainder: string;
}

function isAbbreviationBoundary(value: string, end: number): boolean {
  const tail = value.slice(Math.max(0, end - 16), end).toLowerCase();
  return /(?:^|\s)(?:ст|п|г|т\.?\s*д|т\.?\s*п|и\.?\s*т\.?\s*д)\.$/.test(tail);
}

function softBoundary(value: string, before: number): number {
  let boundary = 0;
  for (const match of value.matchAll(SOFT_BOUNDARY)) {
    const end = (match.index || 0) + match[0].length;
    if (end > before) break;
    boundary = end;
  }
  return boundary || value.lastIndexOf(" ", before) || before;
}

function nextBoundary(value: string, force: boolean, maximum: number): number {
  for (const match of value.matchAll(SENTENCE_BOUNDARY)) {
    const end = (match.index || 0) + match[0].length;
    if (isAbbreviationBoundary(value, end)) continue;
    if (end >= MIN_PHRASE_LENGTH) return end;
  }
  if (value.length > maximum) return softBoundary(value, maximum);
  return force ? value.length : 0;
}

/**
 * Splits streaming answer text into natural, bounded phrases.  The remaining
 * partial sentence stays untouched so the next SSE delta can complete it.
 */
export function splitAtlasOverlaySpeech(
  value: string,
  options: { flush?: boolean; maximumChars?: number; maximumPhrases?: number } = {},
): AtlasOverlaySpeechSplit {
  const maximum = Math.max(80, Math.min(800, Math.round(options.maximumChars || 320)));
  const maximumPhrases = Math.max(1, Math.min(24, Math.round(options.maximumPhrases || 24)));
  const flush = options.flush === true;
  let remainder = String(value || "");
  const phrases: string[] = [];

  while (phrases.length < maximumPhrases) {
    const boundary = nextBoundary(remainder, flush, maximum);
    if (!boundary) break;
    const phrase = remainder.slice(0, boundary).trim();
    remainder = remainder.slice(boundary);
    if (phrase) phrases.push(phrase);
  }
  return { phrases, remainder };
}
