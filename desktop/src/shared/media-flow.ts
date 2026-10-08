/** One visible destination owns read responses; navigation invalidates older reads. */
export class MediaReadGate {
  private revision = 0;
  begin(): number { return ++this.revision; }
  accepts(ticket: number): boolean { return ticket === this.revision; }
  invalidate(): void { this.revision++; }
}

/** Account-scoped LRU: browsing profiles must not retain every binary forever. */
export class MediaAssetCache<T> {
  private readonly entries = new Map<string, Promise<T>>();
  constructor(private readonly limit = 24) {
    if (!Number.isInteger(limit) || limit < 1) throw new RangeError("invalid_media_cache_limit");
  }
  load(key: string, loader: () => Promise<T>): Promise<T> {
    const cached = this.entries.get(key);
    if (cached) {
      this.entries.delete(key); this.entries.set(key, cached);
      return cached;
    }
    const pending = Promise.resolve().then(loader);
    this.entries.set(key, pending);
    while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value!);
    // An evicted request can fail after a replacement was installed for this key.
    void pending.catch(() => { if (this.entries.get(key) === pending) this.entries.delete(key); });
    return pending;
  }
}

export function mergeMediaPosts<T extends { id: number }>(current: T[], incoming: T[]): T[] {
  return [...new Map([...current, ...incoming].map(post => [post.id, post])).values()].sort((a, b) => b.id - a.id);
}

export type MediaPostKind = "post" | "rollback";
export interface MediaDraft { body: string; source: string; revision: number }
export interface MediaPublication extends MediaDraft { kind: MediaPostKind; nonce: string }

/** Memory-only, independent drafts. A late acknowledgement owns the captured
 * revision, never a new draft or the other composer after a tab switch. */
export class MediaComposers {
  private revision = 0;
  private drafts = new Map<MediaPostKind, MediaDraft>();
  private pending = new Map<MediaPostKind, MediaPublication>();
  constructor(private readonly nonce: () => string) {}
  read(kind: MediaPostKind): MediaDraft { return this.drafts.get(kind) || { body: "", source: "", revision: 0 }; }
  update(kind: MediaPostKind, patch: Partial<Pick<MediaDraft, "body" | "source">>): MediaDraft {
    const previous = this.read(kind), next = { ...previous, ...patch };
    if (previous.body === next.body && previous.source === next.source) return previous;
    next.revision = ++this.revision;
    this.drafts.set(kind, next);
    return next;
  }
  capture(kind: MediaPostKind): MediaPublication {
    const draft = this.read(kind), pending = this.pending.get(kind);
    const same = pending && pending.body.trim() === draft.body.trim() && (kind === "post" || pending.source.trim() === draft.source.trim());
    const send = { ...draft, kind, nonce: same ? pending.nonce : this.nonce() };
    this.pending.set(kind, send);
    return send;
  }
  acknowledge(send: MediaPublication): void {
    if (this.pending.get(send.kind)?.nonce === send.nonce) this.pending.delete(send.kind);
    if (this.read(send.kind).revision === send.revision) this.update(send.kind, { body: "", source: "" });
  }
}

export function isPublishedMediaPost(value: unknown, author: string, send: MediaPublication): boolean {
  if (!value || typeof value !== "object") return false;
  const post = value as Record<string, unknown>;
  return Number.isSafeInteger(post.id) && Number(post.id) > 0 && post.author_id === author && post.kind === send.kind
    && post.body === send.body.trim() && post.source_url === (send.kind === "rollback" ? send.source.trim() : "")
    && typeof post.created_at === "string" && typeof post.display_name === "string";
}
