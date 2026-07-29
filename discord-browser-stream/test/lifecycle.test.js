import assert from "node:assert/strict";
import test from "node:test";

import {
  canReuseVoiceConnection,
  createLifecycleQueue,
  delay,
  isExpectedBrowserCloseError,
  isRetryableBrowserLaunchError,
  isSameLiveStream,
  normalizeStreamUrl,
  stopCaptureStream,
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

test("lifecycle queue stays serialized across repeated start-stop bursts", async () => {
  const enqueue = createLifecycleQueue();
  let running = 0;
  let maximumRunning = 0;
  let completed = 0;

  const operations = Array.from({ length: 100 }, (_, index) =>
    enqueue(async () => {
      running += 1;
      maximumRunning = Math.max(maximumRunning, running);
      await delay(index % 3);
      running -= 1;
      completed += 1;
      if (index % 17 === 0) throw new Error(`expected-${index}`);
    }).catch(() => {}),
  );

  await Promise.all(operations);
  assert.equal(maximumRunning, 1);
  assert.equal(completed, 100);
  assert.equal(running, 0);
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

test("stream URL validation accepts web pages without leaking credentials", () => {
  assert.equal(normalizeStreamUrl(" https://tvr.lat/egg "), "https://tvr.lat/egg");
  assert.throws(() => normalizeStreamUrl("file:///etc/passwd"), /http/);
  assert.throws(
    () => normalizeStreamUrl("https://admin:secret@tvr.lat/"),
    /credentials/,
  );
  assert.throws(
    () => normalizeStreamUrl(`https://tvr.lat/${"x".repeat(2048)}`),
    /invalid/,
  );
});

test("capture cleanup supports both puppeteer-stream APIs", async () => {
  let stopped = 0;
  assert.equal(
    await stopCaptureStream({
      stop: async () => {
        stopped += 1;
      },
    }),
    "stop",
  );
  assert.equal(stopped, 1);

  let destroyed = 0;
  assert.equal(
    await stopCaptureStream({
      destroyed: false,
      destroy: () => {
        destroyed += 1;
      },
    }),
    "destroy",
  );
  assert.equal(destroyed, 1);

  let ended = 0;
  assert.equal(
    await stopCaptureStream({
      writableEnded: false,
      end: () => {
        ended += 1;
      },
    }),
    "end",
  );
  assert.equal(ended, 1);

  assert.equal(await stopCaptureStream(null), "none");
});

test("capture cleanup cannot hang forever and falls back to destroy", async () => {
  let destroyed = 0;
  await assert.rejects(
    stopCaptureStream(
      {
        destroyed: false,
        stop: () => new Promise(() => {}),
        destroy: () => {
          destroyed += 1;
        },
      },
      5,
    ),
    /capture_stop_timeout/,
  );
  assert.equal(destroyed, 1);
});

test("identical live stream starts are idempotent", () => {
  const session = {
    state: "streaming",
    url: "https://tvr.lat/egg",
    guildId: "guild",
    voiceChannelId: "voice",
  };

  assert.equal(
    isSameLiveStream(session, session.url, "guild", "voice"),
    true,
  );
  assert.equal(
    isSameLiveStream(
      { ...session, state: "starting" },
      session.url,
      "guild",
      "voice",
    ),
    true,
  );
  assert.equal(
    isSameLiveStream(
      { ...session, state: "stopping" },
      session.url,
      "guild",
      "voice",
    ),
    false,
  );
  assert.equal(
    isSameLiveStream(session, "https://tvr.lat/", "guild", "voice"),
    false,
  );
});
