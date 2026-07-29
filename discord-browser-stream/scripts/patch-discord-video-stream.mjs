import { readFile, writeFile } from "node:fs/promises";

const target =
  process.argv[2] ||
  "/app/node_modules/@dank074/discord-video-stream/dist/media/newApi.js";

function replaceOnce(source, before, after, label) {
  if (source.includes(after)) return source;
  const first = source.indexOf(before);
  if (first < 0 || source.indexOf(before, first + before.length) >= 0) {
    throw new Error(`Cannot apply discord-video-stream patch: ${label}`);
  }
  return source.slice(0, first) + after + source.slice(first + before.length);
}

let source = await readFile(target, "utf8");

source = replaceOnce(
  source,
  `    if (mergedOptions.type === "go-live") {
        conn = await streamer.createStream();
        stopStream = () => streamer.stopStream();
    }
`,
  `    if (mergedOptions.type === "go-live") {
        const connectionTimeoutMs = Number.isFinite(options.connectionTimeoutMs)
            ? Math.max(1000, options.connectionTimeoutMs)
            : 20000;
        let connectionTimer;
        let onConnectionAbort;
        try {
            conn = await Promise.race([
                streamer.createStream(),
                new Promise((_, reject) => {
                    connectionTimer = setTimeout(() => reject(new Error("stream_connection_timeout")), connectionTimeoutMs);
                }),
                new Promise((_, reject) => {
                    if (!cancelSignal)
                        return;
                    onConnectionAbort = () => reject(cancelSignal.reason ||
                        new DOMException("The operation was aborted", "AbortError"));
                    if (cancelSignal.aborted)
                        onConnectionAbort();
                    else
                        cancelSignal.addEventListener("abort", onConnectionAbort, { once: true });
                }),
            ]);
        }
        catch (error) {
            streamer.stopStream();
            throw error;
        }
        finally {
            clearTimeout(connectionTimer);
            if (onConnectionAbort)
                cancelSignal?.removeEventListener("abort", onConnectionAbort);
        }
        stopStream = () => streamer.stopStream();
    }
`,
  "bounded Go Live connection",
);

for (const marker of [
  "stream_connection_timeout",
  "onConnectionAbort",
  "streamer.stopStream();\n            throw error;",
]) {
  if (!source.includes(marker)) {
    throw new Error(`Incomplete discord-video-stream patch: ${marker}`);
  }
}

await writeFile(target, source, "utf8");
console.log(`Patched discord-video-stream runtime: ${target}`);
