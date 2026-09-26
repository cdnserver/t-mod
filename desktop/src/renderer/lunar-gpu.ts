/* Rotate the lunar map, not its silhouette or light. The software projection
   remains available on devices without a usable graphics context. */
export function createLunarRenderer(canvas: HTMLCanvasElement, image: HTMLImageElement) {
  const gl = canvas.getContext("webgl", { alpha: true, premultipliedAlpha: false, antialias: false });
  if (!gl) return null;
  const shaders: WebGLShader[] = [];
  let program: WebGLProgram | null = null;
  let buffer: WebGLBuffer | null = null;
  let texture: WebGLTexture | null = null;
  const dispose = () => {
    shaders.forEach(shader => gl.deleteShader(shader));
    gl.deleteProgram(program); gl.deleteBuffer(buffer); gl.deleteTexture(texture);
  };
  try {
    const shader = (type: number, source: string) => {
      const result = gl.createShader(type)!;
      shaders.push(result); gl.shaderSource(result, source); gl.compileShader(result);
      if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) throw new Error("lunar_shader_unavailable");
      return result;
    };
    program = gl.createProgram()!;
    gl.attachShader(program, shader(gl.VERTEX_SHADER, `
      attribute vec2 position;
      varying vec2 point;
      void main() { point = position; gl_Position = vec4(position, 0.0, 1.0); }
    `));
    gl.attachShader(program, shader(gl.FRAGMENT_SHADER, `
      precision highp float;
      varying vec2 point;
      uniform sampler2D lunarMap;
      uniform float longitude;
      uniform float edge;
      void main() {
        vec2 p = point / .944;
        float r2 = dot(p, p);
        if (r2 >= 1.0) { gl_FragColor = vec4(0.0); return; }
        float z = sqrt(1.0 - r2);
        vec2 uv = vec2(fract(.5 + atan(p.x, z) / 6.283185307 + longitude),
                       .5 - asin(p.y) / 3.141592654);
        vec3 surface = texture2D(lunarMap, uv).rgb;
        float light = .055 + pow(max(0.0, dot(vec3(p, z), vec3(.748, .347, .566))), .8) * 1.1;
        gl_FragColor = vec4(surface * light, smoothstep(0.0, edge, 1.0 - r2));
      }
    `));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error("lunar_program_unavailable");
    gl.useProgram(program);
    buffer = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const attribute = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(attribute); gl.vertexAttribPointer(attribute, 2, gl.FLOAT, false, 0, 0);
    texture = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    const maxTexture = gl.getParameter(gl.MAX_TEXTURE_SIZE) as number;
    if (image.naturalWidth > maxTexture) throw new Error("lunar_texture_too_large");
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
    if (gl.getError() !== gl.NO_ERROR) throw new Error("lunar_upload_unavailable");
    const longitude = gl.getUniformLocation(program, "longitude");
    const edge = gl.getUniformLocation(program, "edge");
    return {
      render(turn: number) {
        const size = Math.max(1, Math.min(4096, Math.ceil(canvas.clientWidth * Math.min(2, window.devicePixelRatio || 1))));
        if (canvas.width !== size || canvas.height !== size) { canvas.width = size; canvas.height = size; }
        gl.viewport(0, 0, size, size);
        gl.uniform1f(longitude, turn); gl.uniform1f(edge, 2 / size);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      },
      dispose,
    };
  } catch { dispose(); return null; }
}
