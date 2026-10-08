export interface ChatDraft<T> { text: string; file: T | null; revision: number }
export interface ChatSend<T> extends ChatDraft<T> { recipient: string; nonce: string }

/** Memory-only drafts, independently owned by each recipient. Never persist
 * private text or file contents to browser storage just to survive a switch. */
export class CommunicateComposers<T> {
  private revision = 0;
  private drafts = new Map<string, ChatDraft<T>>();
  private pending = new Map<string, ChatSend<T>>();
  constructor(private readonly nonce: () => string) {}

  read(recipient: string): ChatDraft<T> {
    return this.drafts.get(recipient) || { text: "", file: null, revision: 0 };
  }

  update(recipient: string, patch: Partial<Pick<ChatDraft<T>, "text" | "file">>): ChatDraft<T> {
    const previous = this.read(recipient);
    const next = { ...previous, ...patch };
    if (next.text === previous.text && next.file === previous.file) return previous;
    next.revision = ++this.revision;
    this.drafts.set(recipient, next);
    return next;
  }

  select(recipient: string): ChatDraft<T> {
    // A shared page queued before choosing a person belongs to the first
    // writable dialogue, never to the official announcements channel.
    const unassigned = this.read("");
    if (recipient !== "0" && (unassigned.text || unassigned.file)) {
      const current = this.read(recipient);
      this.update(recipient, {
        text: [current.text, unassigned.text].filter(Boolean).join("\n"),
        file: current.file || unassigned.file,
      });
      this.update("", { text: "", file: null });
    }
    return this.read(recipient);
  }

  capture(recipient: string): ChatSend<T> {
    const draft = this.read(recipient);
    const pending = this.pending.get(recipient);
    // A response can be lost after server commit. Retrying unchanged content
    // must retain its nonce even if the user visited a different dialogue.
    const nonce = pending && pending.text === draft.text && pending.file === draft.file ? pending.nonce : this.nonce();
    const send = { ...draft, recipient, nonce };
    this.pending.set(recipient, send);
    return send;
  }

  acknowledge(send: ChatSend<T>): ChatDraft<T> {
    if (this.pending.get(send.recipient)?.nonce === send.nonce) this.pending.delete(send.recipient);
    if (this.read(send.recipient).revision === send.revision) {
      return this.update(send.recipient, { text: "", file: null });
    }
    // Do not erase a new draft, including text edited and changed back to the
    // same string while delivery was pending.
    return this.read(send.recipient);
  }
}
