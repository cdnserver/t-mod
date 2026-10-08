export const CHAT_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
export const CHAT_FILE_LIMIT = 8 * 1024 * 1024;

export function attachmentSizeLabel(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "Размер неизвестен";
  if (bytes < 1024) return `${Math.round(bytes)} Б`;
  const divisor = bytes < 1024 * 1024 ? 1024 : 1024 * 1024;
  const value = bytes / divisor;
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: value < 10 ? 1 : 0 }).format(value)} ${divisor === 1024 ? "КБ" : "МБ"}`;
}

/** No retained binary cache: off-screen history must not fill memory with photos.
 * Queued work is cancellable; an already-running IPC request finishes privately.
 */
export function createAttachmentQueue<T>(fetcher: (id: string) => Promise<T>, concurrency = 2) {
  const queue: Array<{ cancelled: boolean; start: () => void }> = [];
  let running = 0;
  const drain = () => {
    while (running < Math.max(1, concurrency) && queue.length) {
      const next = queue.shift()!;
      if (!next.cancelled) next.start();
    }
  };
  return (id: string) => {
    let settle: (value: T | null) => void;
    const promise = new Promise<T | null>(resolve => { settle = resolve; });
    const entry = { cancelled: false, start: () => {
      running++;
      void Promise.resolve().then(() => fetcher(id)).then(
        value => settle(entry.cancelled ? null : value), () => settle(null),
      ).finally(() => { running--; drain(); });
    } };
    queue.push(entry);
    drain();
    return { promise, cancel: () => { entry.cancelled = true; settle(null); drain(); } };
  };
}
