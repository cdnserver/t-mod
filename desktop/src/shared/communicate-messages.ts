interface ThreadMessage {
  id: number; sender_id: string; recipient_id: string; body: string; created_at: string;
}

/** Reject a malformed or wrong-dialogue reply as a whole. Treating a failed
 * projection as an empty chat would hide history and falsely report success. */
export function parseChatThread<T extends ThreadMessage>(value: unknown, viewer: string, partner: string): T[] {
  if (!/^[1-9]\d*$/.test(viewer) || !/^(0|[1-9]\d*)$/.test(partner) || !Array.isArray(value)) {
    throw new Error("communicate_thread_invalid");
  }
  let previous = -1;
  for (const entry of value) {
    if (!entry || typeof entry !== "object" || Array.isArray(entry)) throw new Error("communicate_thread_invalid");
    const message = entry as Record<string, unknown>;
    const service = partner === "0" && message.id === 0 && message.sender_id === "0" && message.recipient_id === viewer;
    const personal = partner !== "0" && typeof message.id === "number" && message.id > 0 &&
      ((message.sender_id === viewer && message.recipient_id === partner) ||
       (message.sender_id === partner && message.recipient_id === viewer));
    if ((!service && !personal) || typeof message.id !== "number" || !Number.isSafeInteger(message.id) || message.id <= previous ||
        typeof message.body !== "string" || typeof message.created_at !== "string" ||
        (!service && !Number.isFinite(Date.parse(message.created_at)))) throw new Error("communicate_thread_invalid");
    previous = message.id;
  }
  return value as T[];
}

export function isDeliveredChatMessage(value: unknown, recipient: string): value is {
  id: number; sender_id: string; recipient_id: string; body: string; created_at: string;
} {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const message = value as Record<string, unknown>;
  return typeof message.id === "number" && Number.isSafeInteger(message.id) && message.id > 0 &&
    typeof message.sender_id === "string" && /^[1-9]\d*$/.test(message.sender_id) &&
    message.recipient_id === recipient && typeof message.body === "string" && typeof message.created_at === "string";
}

export function mergeDeliveredMessage<T extends { id: number }>(messages: T[], delivered: T): T[] {
  if (messages.some(message => message.id === delivered.id)) return messages;
  return [...messages, delivered].sort((left, right) => left.id - right.id);
}

export function mergeThreadSnapshot<T extends { id: number }>(messages: T[], snapshot: T[]): T[] {
  const byId = new Map(messages.map(message => [message.id, message]));
  for (const message of snapshot) byId.set(message.id, message);
  return [...byId.values()].sort((left, right) => left.id - right.id);
}

export function isNearChatBottom(metrics: { scrollHeight: number; clientHeight: number; scrollTop: number }): boolean {
  return metrics.scrollHeight - metrics.clientHeight - metrics.scrollTop <= 72;
}

export function unreadChatMessages<T extends { id: number; sender_id: string }>(messages: T[], readThrough: number, viewerId: string): number {
  return messages.filter(message => message.id > readThrough && message.sender_id !== viewerId).length;
}

export function chatMessageLayout(messages: { sender_id: string; created_at: string }[], now = new Date()) {
  const dayKey = (date: Date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  return messages.map((message, index) => {
    const date = new Date(message.created_at);
    if (!message.created_at || !Number.isFinite(date.getTime())) return { dayLabel: "", grouped: false };
    const previous = messages[index - 1];
    const previousDate = new Date(previous?.created_at || "");
    const sameDay = Boolean(previous && dayKey(previousDate) === dayKey(date));
    const delta = date.getTime() - previousDate.getTime();
    const dayLabel = sameDay ? "" : dayKey(date) === dayKey(now) ? "Сегодня"
      : dayKey(date) === dayKey(yesterday) ? "Вчера"
      : date.toLocaleDateString("ru-RU", { day: "numeric", month: "long", ...(date.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) });
    return { dayLabel, grouped: sameDay && previous.sender_id === message.sender_id && delta >= 0 && delta < 5 * 60_000 };
  });
}
