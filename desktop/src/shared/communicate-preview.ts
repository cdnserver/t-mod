export interface DocumentPreview { title: string; detail: string; resource: string; status: string }

/** Metadata is optional, never a reason to break an otherwise readable message. */
export function readableDocumentPreview(value: unknown): DocumentPreview | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
  if (typeof source.title !== "string" || !source.title.trim()) return null;
  if (["detail", "resource", "status"].some(key => source[key] !== undefined && typeof source[key] !== "string")) return null;
  const clean = (key: string, limit: number) => String(source[key] || "").replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, limit);
  const title = clean("title", 150);
  return title ? { title, detail: clean("detail", 240), resource: clean("resource", 80), status: clean("status", 60) } : null;
}

/** Per-window, short-lived cache. Hidden cards do not enqueue requests. */
export function createDocumentPreviewLoader<T>(fetcher: (url: string) => Promise<T | null>, concurrency = 2, ttlMs = 30_000) {
  const cache = new Map<string, { expiresAt: number; result: Promise<T | null> }>();
  const queue: Array<() => void> = [];
  let running = 0;
  const drain = () => {
    while (running < Math.max(1, concurrency) && queue.length) queue.shift()!();
  };
  return (url: string): Promise<T | null> => {
    const cached = cache.get(url);
    if (cached && cached.expiresAt > Date.now()) return cached.result;
    const entry: { expiresAt: number; result: Promise<T | null> } = { expiresAt: Infinity, result: Promise.resolve(null) };
    entry.result = new Promise<T | null>(resolve => {
      queue.push(() => {
        running += 1;
        void Promise.resolve().then(() => fetcher(url)).then(resolve, () => resolve(null)).finally(() => {
          running -= 1;
          entry.expiresAt = Date.now() + ttlMs;
          for (const [key, item] of cache) {
            if (item.expiresAt <= Date.now()) cache.delete(key);
          }
          drain();
        });
      });
    });
    cache.set(url, entry);
    drain();
    return entry.result;
  };
}
