import assert from "node:assert/strict";
import test from "node:test";

import {
  canReuseVoiceConnection,
  createLifecycleQueue,
  delay,
  isExpectedBrowserCloseError,
  isRetryableBrowserLaunchError,
  waitForVoiceConnection,
} from "../src/lifecycle.js";

test("lifecycle queue serializes operations and survives a rejection", async () => {
  const enqueue = createLifecycleQueue();
  const events = [];

  const first = enqueue(async () => {
    events.push("first:start");
    await delay(15);
    events.push("first:end");
  });
  const failed = enqueue(async () => {
    events.push("failed:start");
    throw new Error("expected");
  });
  const last = enqueue(async () => {
    events.push("last:start");
  });

  await first;
  await assert.rejects(failed, /expected/);
  await last;
  assert.deepEqual(events, [
    "first:start",
    "first:end",
    "failed:start",
    "last:start",
  ]);
});

test("voice connection is reused only for the same live target", () => {
  const connection = {
    _closed: false,
    status: { started: true },
    guildId: "guild",
    channelId: "voice",
  };

  assert.equal(canReuseVoiceConnection(connection, "guild", "voice"), true);
  assert.equal(canReuseVoiceConnection(connection, "guild", "other"), false);
  assert.equal(
    canReuseVoiceConnection(
      { ...connection, status: { started: false } },
      "guild",
      "voice",
    ),
    false,
  );
  assert.equal(
    canReuseVoiceConnection(
      { ...connection, _closed: true },
      "guild",
      "voice",
    ),
    false,
  );
});

test("voice connection wait supports success, timeout, and abort", async () => {
  const active = new AbortController();
  assert.equal(
    await waitForVoiceConnection(Promise.resolve("connected"), active.signal, 50),
    "connected",
  );

  await assert.rejects(
    waitForVoiceConnection(new Promise(() => {}), active.signal, 5),
    /voice_connection_timeout/,
  );

  const cancelled = new AbortController();
  const pending = waitForVoiceConnection(
    new Promise(() => {}),
    cancelled.signal,
    100,
  );
  cancelled.abort();
  await assert.rejects(pending, { name: "AbortError" });
});

test("only expected Chromium shutdown errors are classified as harmless", () => {
  assert.equal(
    isExpectedBrowserCloseError(
      Object.assign(new Error("Protocol error: Target closed"), {
        name: "TargetCloseError",
      }),
    ),
    true,
  );
  assert.equal(isExpectedBrowserCloseError(new Error("network failed")), false);
});

test("only browser process startup failures are retried", () => {
  assert.equal(
    isRetryableBrowserLaunchError(
      Object.assign(new Error("Protocol error: Target closed"), {
        name: "TargetCloseError",
      }),
    ),
    true,
  );
  assert.equal(
    isRetryableBrowserLaunchError(
      new Error("Failed to launch the browser process"),
    ),
    true,
  );
  assert.equal(isRetryableBrowserLaunchError(new Error("invalid URL")), false);
});
