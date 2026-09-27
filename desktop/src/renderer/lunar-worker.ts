/* Orthographic lunar sphere projected from NASA's equirectangular LRO mosaic.
   No artificial craters, continuous render loop, remote fetches or GPU dependency. */
let map: Uint8ClampedArray | undefined;
let mapWidth = 0;
let mapHeight = 0;

self.onmessage = (event: MessageEvent<{ bitmap?: ImageBitmap; size: number; cssSize?: number }>) => {
  if (event.data.bitmap) {
    const bitmap = event.data.bitmap;
    mapWidth = bitmap.width; mapHeight = bitmap.height;
    const source = new OffscreenCanvas(mapWidth, mapHeight);
    const ctx = source.getContext("2d", { willReadFrequently: true })!;
    ctx.drawImage(bitmap, 0, 0); bitmap.close();
    map = ctx.getImageData(0, 0, mapWidth, mapHeight).data;
    source.width = 1; source.height = 1;
  }
  if (!map) return;
  const size = event.data.size;
  const output = new OffscreenCanvas(size, size);
  const ctx = output.getContext("2d")!;
  const pixels = ctx.createImageData(size, size);
  const data = pixels.data;
  const radius = size * .472;
  const lx = .748, ly = .347, lz = .566;
  for (let y = 0; y < size; y++) {
    const ny = (size * .5 - y - .5) / radius;
    if (Math.abs(ny) >= 1) continue;
    const extent = Math.sqrt(1 - ny * ny);
    const v = (.5 - Math.asin(ny) / Math.PI) * (mapHeight - 1);
    const y0 = Math.floor(v), fy = v - y0;
    const left = Math.max(0, Math.ceil(size * .5 - extent * radius));
    const right = Math.min(size, Math.floor(size * .5 + extent * radius));
    for (let x = left; x < right; x++) {
      const nx = (x + .5 - size * .5) / radius;
      const r2 = nx * nx + ny * ny;
      const nz = Math.sqrt(Math.max(0, 1 - r2));
      const u = (.5 + Math.atan2(nx, nz) / (2 * Math.PI)) * (mapWidth - 1);
      const x0 = Math.floor(u), fx = u - x0;
      const a = (y0 * mapWidth + x0) * 4;
      const b = a + mapWidth * 4;
      const shade = .055 + Math.pow(Math.max(0, nx * lx + ny * ly + nz * lz), .8) * 1.1;
      const dest = (y * size + x) * 4;
      for (let channel = 0; channel < 3; channel++) {
        const top = map[a + channel] * (1 - fx) + map[a + 4 + channel] * fx;
        const bottom = map[b + channel] * (1 - fx) + map[b + 4 + channel] * fx;
        data[dest + channel] = (top * (1 - fy) + bottom * fy) * shade;
      }
      const coverage = Math.max(0, Math.min(1, (1 - r2) / Math.min(.03, 8 / Math.max(1, event.data.cssSize || size))));
      data[dest + 3] = coverage * coverage * (3 - 2 * coverage) * 255;
    }
  }
  ctx.putImageData(pixels, 0, 0);
  const bitmap = output.transferToImageBitmap();
  (self as unknown as { postMessage: (message: unknown, transfer: Transferable[]) => void }).postMessage({ bitmap }, [bitmap]);
};

export {};
