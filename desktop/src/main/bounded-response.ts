/** Enforce the limit while reading, including chunked/decompressed responses.
 * A declared Content-Length is only an early rejection, never a safety limit. */
export async function readBoundedBytes(response: Response, limit: number, signal?: AbortSignal): Promise<Uint8Array> {
  if (!Number.isSafeInteger(limit) || limit < 1) throw new RangeError("response_limit_invalid");
  signal?.throwIfAborted();
  const declared = Number(response.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > limit) {
    await response.body?.cancel().catch(() => undefined);
    throw new Error("response_size_exceeded");
  }
  if (!response.body) return new Uint8Array();
  const reader = response.body.getReader();
  const abort = () => { void reader.cancel(signal?.reason).catch(() => undefined); };
  signal?.addEventListener("abort", abort, { once: true });
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      signal?.throwIfAborted();
      const chunk = await reader.read();
      signal?.throwIfAborted();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > limit) {
        await reader.cancel().catch(() => undefined);
        throw new Error("response_size_exceeded");
      }
      chunks.push(chunk.value);
    }
    const result = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
    return result;
  } finally {
    signal?.removeEventListener("abort", abort);
    reader.releaseLock();
  }
}
