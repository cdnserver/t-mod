import { describe, expect, it } from "vitest";
import { CommunicateComposers } from "../src/shared/communicate-composer";

const create = () => {
  let nonce = 0;
  return new CommunicateComposers<object>(() => String(++nonce).padStart(32, "0"));
};

describe("Communicate recipient-owned composers", () => {
  it("never carries another person's text or file into a selected chat", () => {
    const composers = create(), file = {};
    composers.update("alice", { text: "Привет", file });
    expect(composers.select("bob")).toMatchObject({ text: "", file: null });
    expect(composers.select("alice")).toMatchObject({ text: "Привет", file });
  });
  it("clears delivered content after leaving and returning before acknowledgement", () => {
    const composers = create();
    composers.update("alice", { text: "Уже отправлено", file: {} });
    const sending = composers.capture("alice");
    composers.select("bob");
    composers.select("alice");
    expect(composers.acknowledge(sending)).toMatchObject({ text: "", file: null });
  });
  it("does not clear a new draft when delivery of the previous one completes", () => {
    const composers = create();
    composers.update("alice", { text: "Первое" });
    const sending = composers.capture("alice");
    composers.update("alice", { text: "Следующее", file: {} });
    expect(composers.acknowledge(sending).text).toBe("Следующее");
    expect(composers.read("alice").file).not.toBeNull();
  });
  it("preserves a draft edited back to identical text during delivery", () => {
    const composers = create();
    composers.update("alice", { text: "Ещё раз" });
    const sending = composers.capture("alice");
    composers.update("alice", { text: "" });
    composers.update("alice", { text: "Ещё раз" });
    expect(composers.acknowledge(sending).text).toBe("Ещё раз");
  });
  it("keeps the request identity after a lost reply and a chat switch", () => {
    const composers = create();
    composers.update("alice", { text: "С файлом", file: {} });
    const first = composers.capture("alice");
    composers.select("bob"); composers.select("alice");
    expect(composers.capture("alice").nonce).toBe(first.nonce);
  });
  it("uses a new request identity for changed content or a new send after delivery", () => {
    const composers = create();
    composers.update("alice", { text: "Первое" });
    const first = composers.capture("alice");
    composers.update("alice", { text: "Второе" });
    const second = composers.capture("alice");
    expect(second.nonce).not.toBe(first.nonce);
    composers.acknowledge(second);
    composers.update("alice", { text: "Второе" });
    expect(composers.capture("alice").nonce).not.toBe(second.nonce);
  });
  it("queues an unassigned share through the official channel and transfers it exactly once", () => {
    const composers = create();
    composers.update("", { text: "https://reactor.tvr.lat/reactor/case/42" });
    expect(composers.select("0").text).toBe("");
    composers.update("alice", { text: "Посмотри" });
    expect(composers.select("alice").text).toBe("Посмотри\nhttps://reactor.tvr.lat/reactor/case/42");
    expect(composers.select("bob").text).toBe("");
    expect(composers.select("alice").text).toBe("Посмотри\nhttps://reactor.tvr.lat/reactor/case/42");
  });
  it("cannot let an old acknowledgement erase a later send for the same person", () => {
    const composers = create();
    composers.update("alice", { text: "Первое" });
    const first = composers.capture("alice");
    composers.acknowledge(first);
    composers.update("alice", { text: "Следующее" });
    const next = composers.capture("alice");
    composers.acknowledge(first);
    expect(composers.read("alice").text).toBe("Следующее");
    expect(composers.capture("alice").nonce).toBe(next.nonce);
  });
});
