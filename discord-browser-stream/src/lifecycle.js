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
        onAbort = () => reject(signal.reason);
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
