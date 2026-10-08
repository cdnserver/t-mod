import { describe, expect, it } from "vitest";
import { chatMessageLayout, isDeliveredChatMessage, isNearChatBottom, mergeDeliveredMessage, mergeThreadSnapshot, parseChatThread, unreadChatMessages } from "../src/shared/communicate-messages";

describe("Communicate thread replies", () => {
  const message = { id: 7, sender_id: "902235631952998410", recipient_id: "825331775857360906", body: "Привет", created_at: "2026-10-08T12:00:00Z" };
  const parse = (value: unknown) => parseChatThread(value, message.sender_id, message.recipient_id);
  it("accepts both directions and an actually empty conversation", () => {
    const thread = [message, { ...message, id: 8, sender_id: message.recipient_id, recipient_id: message.sender_id }];
    expect(parse(thread)).toBe(thread);
    expect(parse([])).toEqual([]);
  });
  it("never interprets a broken projection as an empty history", () => {
    for (const value of [undefined, null, {}, { error: "unavailable" }, [null]]) expect(() => parse(value)).toThrow();
  });
  it("rejects other dialogues, rounded Discord identifiers and unordered pages", () => {
    for (const value of [
      [{ ...message, sender_id: "5" }], [{ ...message, recipient_id: "5" }],
      [{ ...message, sender_id: 902235631952998400 }], [{ ...message, id: Number.MAX_SAFE_INTEGER + 1 }],
      [message, message], [message, { ...message, id: 6 }], [{ ...message, created_at: "invalid" }],
    ]) expect(() => parse(value)).toThrow();
  });
  it("allows the official welcome only in its dedicated channel", () => {
    const welcome = { ...message, id: 0, sender_id: "0", recipient_id: message.sender_id, created_at: "" };
    expect(parseChatThread([welcome], message.sender_id, "0")).toEqual([welcome]);
    expect(() => parse([welcome])).toThrow();
    expect(() => parseChatThread([message], message.sender_id, "0")).toThrow();
  });
});

describe("Communicate delivered messages", () => {
  it("requires a real acknowledgement for the intended recipient before clearing a draft", () => {
    const message = { id: 23, sender_id: "902235631952998410", recipient_id: "825331775857360906", body: "Привет", created_at: "2026-10-08T12:00:00Z" };
    expect(isDeliveredChatMessage(message, message.recipient_id)).toBe(true);
    expect(isDeliveredChatMessage(message, "another-person")).toBe(false);
    expect(isDeliveredChatMessage({ ...message, id: Number.NaN }, message.recipient_id)).toBe(false);
    expect(isDeliveredChatMessage({ ...message, sender_id: 902235631952998400 }, message.recipient_id)).toBe(false);
    expect(isDeliveredChatMessage({ ok: true }, message.recipient_id)).toBe(false);
    expect(isDeliveredChatMessage(undefined, message.recipient_id)).toBe(false);
  });
  it("groups only nearby messages from one sender on the same local day", () => {
    const layout = chatMessageLayout([
      { sender_id: "2", created_at: "2026-10-08T10:00:00" },
      { sender_id: "2", created_at: "2026-10-08T10:01:00" },
      { sender_id: "1", created_at: "2026-10-08T10:02:00" },
      { sender_id: "1", created_at: "2026-10-08T10:08:00" },
    ], new Date("2026-10-08T12:00:00"));
    expect(layout.map(item => item.grouped)).toEqual([false, true, false, false]);
    expect(layout.map(item => item.dayLabel)).toEqual(["Сегодня", "", "", ""]);
  });

  it("separates midnight and tolerates invalid or service timestamps", () => {
    const layout = chatMessageLayout([
      { sender_id: "2", created_at: "2026-10-07T23:59:00" },
      { sender_id: "2", created_at: "2026-10-08T00:01:00" },
      { sender_id: "2", created_at: "" },
      { sender_id: "2", created_at: "invalid" },
    ], new Date("2026-10-08T12:00:00"));
    expect(layout.map(item => item.dayLabel)).toEqual(["Вчера", "Сегодня", "", ""]);
    expect(layout.every(item => !item.grouped)).toBe(true);
  });
  it("does not duplicate a message already received by polling", () => {
    const messages = [{ id: 8, body: "Привет" }];
    expect(mergeDeliveredMessage(messages, { id: 8, body: "Привет" })).toBe(messages);
  });

  it("keeps messages ordered if a delayed acknowledgement arrives after newer messages", () => {
    expect(mergeDeliveredMessage([{ id: 7 }, { id: 9 }], { id: 8 }).map(message => message.id)).toEqual([7, 8, 9]);
  });

  it("keeps a newly delivered message when an older poll response arrives", () => {
    expect(mergeThreadSnapshot([{ id: 10 }, { id: 11 }], [{ id: 10 }]).map(message => message.id)).toEqual([10, 11]);
  });

  it("keeps loaded history when a fresh last-page snapshot arrives", () => {
    const history = Array.from({ length: 180 }, (_, index) => ({ id: index + 1 }));
    expect(mergeThreadSnapshot(history, [{ id: 180 }, { id: 181 }])).toHaveLength(181);
  });

  it("follows messages only near the bottom, including short and overscrolled threads", () => {
    expect(isNearChatBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 550 })).toBe(true);
    expect(isNearChatBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 300 })).toBe(false);
    expect(isNearChatBottom({ scrollHeight: 200, clientHeight: 400, scrollTop: 0 })).toBe(true);
    expect(isNearChatBottom({ scrollHeight: 1000, clientHeight: 400, scrollTop: 610 })).toBe(true);
  });

  it("counts only incoming messages beyond the last read message", () => {
    expect(unreadChatMessages([
      { id: 10, sender_id: "2" }, { id: 11, sender_id: "1" }, { id: 12, sender_id: "2" },
    ], 10, "1")).toBe(1);
  });
});
