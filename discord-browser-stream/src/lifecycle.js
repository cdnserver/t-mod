export function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export function isExpectedBrowserCloseError(error) {
  const message = String(error?.message || error);
  return (
    error?.name === "TargetCloseError" ||
    message.includes("Target closed") ||
    message.includes("Session closed")
  );
}

export function isRetryableBrowserLaunchError(error) {
  const message = String(error?.message || error);
  return (
    error?.name === "TargetCloseError" ||
    message.includes("Target closed") ||
    message.includes("Failed to launch the browser process")
  );
}

export function normalizeStreamUrl(value) {
  value = String(value || "").trim();
  if (!value || value.length > 2048) {
    throw new Error("URL is invalid");
  }
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error("URL is invalid");
  }
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("Only http:// and https:// URLs are supported");
  }
  if (!url.hostname || url.username || url.password) {
    throw new Error("URL must not contain credentials");
  }
  return url.toString();
}

export function isSameLiveStream(
  session,
  url,
  guildId,
  voiceChannelId,
) {
  return Boolean(
    session &&
      (session.state === "starting" || session.state === "streaming") &&
      session.url === url &&
      String(session.guildId) === String(guildId) &&
      String(session.voiceChannelId) === String(voiceChannelId),
  );
}

export async function stopCaptureStream(stream, timeoutMs = 2000) {
  if (!stream) return "none";
  if (typeof stream.stop === "function") {
    let timeout;
    try {
      await Promise.race([
        Promise.resolve().then(() => stream.stop()),
        new Promise((_, reject) => {
          timeout = setTimeout(
            () => reject(new Error("capture_stop_timeout")),
            timeoutMs,
          );
        }),
      ]);
      return "stop";
    } catch (error) {
      if (typeof stream.destroy === "function" && !stream.destroyed) {
        stream.destroy();
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
  if (typeof stream.destroy === "function") {
    if (!stream.destroyed) stream.destroy();
    return "destroy";
  }
  if (typeof stream.end === "function") {
    if (!stream.writableEnded) stream.end();
    return "end";
  }
  return "none";
}

export function createLifecycleQueue() {
  let tail = Promise.resolve();
  return (operation) => {
    const queued = tail.then(operation, operation);
    tail = queued.catch(() => {});
    return queued;
  };
}

export async function waitForVoiceConnection(promise, signal, timeoutMs) {
  let timeout;
  let onAbort;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timeout = setTimeout(
          () => reject(new Error("voice_connection_timeout")),
          timeoutMs,
        );
      }),
      new Promise((_, reject) => {
        onAbort = () =>
          reject(
            signal.reason ||
              new DOMException("The operation was aborted", "AbortError"),
          );
        if (signal.aborted) onAbort();
        else signal.addEventListener("abort", onAbort, { once: true });
      }),
    ]);
  } finally {
    clearTimeout(timeout);
    if (onAbort) signal.removeEventListener("abort", onAbort);
  }
}

export function canReuseVoiceConnection(
  connection,
  guildId,
  voiceChannelId,
) {
  return Boolean(
    connection &&
      !connection._closed &&
      connection.status?.started &&
      String(connection.guildId) === String(guildId) &&
      String(connection.channelId) === String(voiceChannelId),
  );
}
