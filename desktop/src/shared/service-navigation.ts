/** A navigation intent outlives its retries, but never a newer destination. */
export class ServiceNavigation {
  private revision = 0;
  private attempt = 0;
  destination = "";
  ready = false;
  status = 0;

  begin(destination = ""): number {
    this.revision++;
    this.destination = destination;
    this.ready = false;
    this.status = 0;
    return this.revision;
  }

  get ticket(): number { return this.revision; }
  current(ticket: number): boolean { return ticket === this.revision; }
  matches(url: string): boolean {
    if (!url || !this.destination) return false;
    try {
      const actual = new URL(url), expected = new URL(this.destination);
      actual.hash = ""; expected.hash = "";
      return actual.href === expected.href;
    } catch { return false; }
  }

  startAttempt(): number {
    this.ready = false;
    this.status = 0;
    return ++this.attempt;
  }
  currentAttempt(ticket: number, attempt: number): boolean {
    return this.current(ticket) && attempt === this.attempt;
  }
  redirect(url: string): void { this.destination = url; }
  finish(url: string, allowed: boolean): boolean {
    if (!this.matches(url)) return false;
    this.ready = allowed && this.status < 400;
    return this.ready;
  }
}

export function navigationWasAborted(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const value = error as { code?: unknown; errno?: unknown; message?: unknown };
  return value.code === "ERR_ABORTED" || value.errno === -3 ||
    (typeof value.message === "string" && /\bERR_ABORTED\b/.test(value.message));
}

/** Renderer requests can resolve after a newer click or after the screen locks. */
export class NavigationIntent {
  private revision = 0;
  begin(): number { return ++this.revision; }
  current(ticket: number): boolean { return ticket === this.revision; }
}
