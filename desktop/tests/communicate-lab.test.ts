import { randomBytes } from "node:crypto";
import { describe, expect, it } from "vitest";
import { downloadCommunicateAttachment, uploadCommunicateAttachment } from "../src/main/communicate-transfer";
import { CommunicateComposers } from "../src/shared/communicate-composer";
import { ProtectedAccessGate } from "../src/shared/protected-access";

// Explicit opt-in: loopback-only, disposable actors and real Python routes.
// Start desktop/scripts/media-lab.py before setting BLACKBIRD_LAB_TOKEN.
const token = process.env.BLACKBIRD_LAB_TOKEN;
const remote = "https://reactor.tvr.lat/api/blackbird/communicate";
function dependencies(actor: string, loseReply = false) {
  let lost = false;
  return {
    access: new ProtectedAccessGate(() => ({ revision: 1, accountId: actor, locked: false, banned: false })),
    viewer: () => ({ id: Number(actor), id_exact: actor }),
    headers: () => ({ "X-Blackbird-Lab": token!, "X-Lab-Actor": actor }),
    onDenied: () => {}, onMismatch: () => { throw new Error("unexpected_lab_account_mismatch"); },
    fetch: async (url: string, init: RequestInit) => {
      if (!url.startsWith(remote)) throw new Error("lab_endpoint_not_allowed");
      const response = await fetch(url.replace(remote, "http://127.0.0.1:5182/api/blackbird/communicate"), init);
      if (loseReply && !lost && init.method === "POST" && response.ok) {
        lost = true; await response.arrayBuffer(); throw new Error("simulated_lost_ack_after_commit");
      }
      return response;
    },
  };
}

describe.skipIf(!token)("Communicate native pipeline with real loopback backend", () => {
  it("delivers a binary file from a non-Senate account to the other account intact", async () => {
    const bytes = new Uint8Array([0, 1, 2, 127, 128, 255]);
    const result = await uploadCommunicateAttachment(dependencies("100"), {
      partnerId: "200", filename: "Проверка.txt", caption: "Проверка нативного пути передачи", bytes,
      clientNonce: randomBytes(16).toString("hex"),
    });
    const message = result.result as { id: number; recipient_id: string; attachment: { id: string } };
    expect(message.recipient_id).toBe("200");
    const received = await downloadCommunicateAttachment(dependencies("200"), message.attachment.id);
    expect(received.bytes).toEqual(bytes);
    expect(received.filename).toBe("Проверка.txt");
  }, 15_000);

  it("keeps the draft after a lost acknowledgement and delivers its retry only once", async () => {
    const deps = dependencies("100", true);
    const composer = new CommunicateComposers<Uint8Array>(() => randomBytes(16).toString("hex"));
    composer.update("200", { text: "Проверка потерянного ответа", file: new Uint8Array([72, 105]) });
    const first = composer.capture("200");
    const upload = (send: typeof first) => uploadCommunicateAttachment(deps, {
      partnerId: send.recipient, filename: "Повтор.txt", caption: send.text, bytes: send.file, clientNonce: send.nonce,
    });
    await expect(upload(first)).rejects.toThrow("simulated_lost_ack_after_commit");
    expect(composer.read("200").file).not.toBeNull();
    const retry = composer.capture("200");
    expect(retry.nonce).toBe(first.nonce);
    const second = await upload(retry);
    const third = await upload(retry);
    expect((third.result as { id: number }).id).toBe((second.result as { id: number }).id);
    expect(composer.acknowledge(retry)).toMatchObject({ text: "", file: null });
  }, 15_000);
});
