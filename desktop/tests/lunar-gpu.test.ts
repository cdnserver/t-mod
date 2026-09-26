import { afterEach, describe, expect, it, vi } from "vitest";
import { createLunarRenderer } from "../src/renderer/lunar-gpu";

afterEach(() => vi.unstubAllGlobals());

function fixture(maxTexture = 8192) {
  const methods = ["shaderSource", "compileShader", "attachShader", "linkProgram", "useProgram",
    "bindBuffer", "bufferData", "enableVertexAttribArray", "vertexAttribPointer", "bindTexture",
    "texParameteri", "texImage2D", "viewport", "uniform1f", "drawArrays", "deleteShader",
    "deleteProgram", "deleteBuffer", "deleteTexture"];
  const gl: Record<string, unknown> = Object.fromEntries(methods.map(name => [name, vi.fn()]));
  Object.assign(gl, {
    createShader: vi.fn(() => ({})), createProgram: vi.fn(() => ({})),
    createBuffer: vi.fn(() => ({})), createTexture: vi.fn(() => ({})),
    getShaderParameter: vi.fn(() => true), getProgramParameter: vi.fn(() => true),
    getAttribLocation: vi.fn(() => 0), getUniformLocation: vi.fn((_program, name) => name),
    getParameter: vi.fn(() => maxTexture), getError: vi.fn(() => 0), NO_ERROR: 0,
  });
  const canvas = { getContext: () => gl, clientWidth: 1000, width: 0, height: 0 } as unknown as HTMLCanvasElement;
  const image = { naturalWidth: 8192 } as HTMLImageElement;
  vi.stubGlobal("window", { devicePixelRatio: 2 });
  return { gl, canvas, image };
}

describe("lunar surface rendering", () => {
  it("falls back cleanly without WebGL", () => {
    expect(createLunarRenderer({ getContext: () => null } as unknown as HTMLCanvasElement, {} as HTMLImageElement)).toBeNull();
  });
  it("rejects unsupported 8K textures and releases graphics resources", () => {
    const { gl, canvas, image } = fixture(4096);
    expect(createLunarRenderer(canvas, image)).toBeNull();
    expect(gl.deleteProgram).toHaveBeenCalledOnce();
    expect(gl.deleteTexture).toHaveBeenCalledOnce();
  });
  it("updates longitude independently of fixed lighting and keeps retina resolution", () => {
    const { gl, canvas, image } = fixture();
    const renderer = createLunarRenderer(canvas, image)!;
    renderer.render(.125);
    expect(canvas.width).toBe(2000);
    expect(canvas.height).toBe(2000);
    expect(gl.uniform1f).toHaveBeenCalledWith("longitude", .125);
    expect(gl.drawArrays).toHaveBeenCalledOnce();
    renderer.dispose();
    expect(gl.deleteShader).toHaveBeenCalledTimes(2);
    expect(gl.deleteBuffer).toHaveBeenCalledOnce();
  });
});
