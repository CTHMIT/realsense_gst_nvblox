#version 100
#ifdef GL_ES
precision mediump float;
#endif
varying vec2 v_texcoord;
uniform sampler2D tex;
void main() {
  float r  = texture2D(tex, v_texcoord).r;
  float v16 = r * 65535.0;
  float hi  = floor(v16 / 256.0);
  float g   = hi / 255.0;
  gl_FragColor = vec4(g, g, g, 1.0);
}
