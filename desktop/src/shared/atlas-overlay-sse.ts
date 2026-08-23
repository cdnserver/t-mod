export interface ServerSentEvent {
  event: string;
  data: string;
  id?: string;
}

const MAX_PENDING_SSE_BYTES = 1_048_576;

/**
 * Small streaming SSE decoder used by the native Atlas client. It deliberately
 * accepts bytes rather than strings so a Cyrillic code point split across two
 * network chunks can never be corrupted.
 */
export class ServerSentEventDecoder {
  private readonly decoder = new TextDecoder("utf-8", { fatal: false });
  private pending = "";
  private eventName = "message";
  private dataLines: string[] = [];
  private lastEventId: string | undefined;

  push(chunk: Uint8Array): ServerSentEvent[] {
    this.pending += this.decoder.decode(chunk, { stream: true });
    if (this.pending.length > MAX_PENDING_SSE_BYTES) {
      this.reset();
      throw new Error("atlas_overlay_sse_event_too_large");
    }
    return this.drainCompleteLines(false);
  }

  finish(): ServerSentEvent[] {
    this.pending += this.decoder.decode();
    const events = this.drainCompleteLines(true);
    const final = this.dispatch();
    if (final) events.push(final);
    return events;
  }

  reset(): void {
    this.decoder.decode();
    this.pending = "";
    this.eventName = "message";
    this.dataLines = [];
    this.lastEventId = undefined;
  }

  private drainCompleteLines(flush: boolean): ServerSentEvent[] {
    const events: ServerSentEvent[] = [];
    let cursor = 0;
    for (;;) {
      const newline = this.pending.indexOf("\n", cursor);
      if (newline < 0) break;
      let line = this.pending.slice(cursor, newline);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      cursor = newline + 1;
      const event = this.consumeLine(line);
      if (event) events.push(event);
    }
    this.pending = this.pending.slice(cursor);
    if (flush && this.pending) {
      let line = this.pending;
      if (line.endsWith("\r")) line = line.slice(0, -1);
      this.pending = "";
      const event = this.consumeLine(line);
      if (event) events.push(event);
    }
    return events;
  }

  private consumeLine(line: string): ServerSentEvent | undefined {
    if (!line) return this.dispatch();
    if (line.startsWith(":")) return undefined;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.dataLines.push(value);
    else if (field === "event") this.eventName = value || "message";
    else if (field === "id" && !value.includes("\0")) this.lastEventId = value;
    return undefined;
  }

  private dispatch(): ServerSentEvent | undefined {
    if (this.dataLines.length === 0) {
      this.eventName = "message";
      return undefined;
    }
    const event: ServerSentEvent = {
      event: this.eventName,
      data: this.dataLines.join("\n"),
      ...(this.lastEventId !== undefined ? { id: this.lastEventId } : {}),
    };
    this.eventName = "message";
    this.dataLines = [];
    return event;
  }
}

export function parseServerSentEventJson(event: ServerSentEvent): unknown {
  try {
    return JSON.parse(event.data) as unknown;
  } catch {
    throw new Error("atlas_overlay_sse_json_invalid");
  }
}
