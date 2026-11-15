// Mechus "navigator" blob: a noise-displaced sphere with an iridescent rim.
// Shared by /welcome (where you shape it) and /dashboard (where it becomes the sun).
import * as THREE from "/static/vendor/three.module.min.js";

// Palettes are named after places on the way home. Keep `accent` in sync with the
// small inline map at the top of dashboard.html (it runs before modules load).
export const PALETTES = {
  aegean: {
    name: "Aegean",
    a: "#123f6b", b: "#4fb6e0", c: "#f4ead5", rim: "#a6e8ff",
    bg: ["#04101c", "#0b2c44"],
    accent: { light: "#2d5d8a", dark: "#7fb0de" },
    night: { top: "#030a14", horizon: "#123a58", deep: "#03111f", crest: "#2a77a6" },
    day: { top: "#8fb8d8", horizon: "#e8eef0", deep: "#2b6f9a", crest: "#a4d6ea" },
  },
  ithaca: {
    name: "Ithaca Dawn",
    a: "#c4553a", b: "#f3c27e", c: "#fff3e2", rim: "#ffd3a1",
    bg: ["#140b17", "#3b1d27"],
    accent: { light: "#b4532f", dark: "#f0a078" },
    night: { top: "#0f0814", horizon: "#6b2f35", deep: "#140a17", crest: "#9a4c3d" },
    day: { top: "#efc2a0", horizon: "#fbeadb", deep: "#6d4a6b", crest: "#f1b58d" },
  },
  winedark: {
    name: "Wine-dark",
    a: "#3d0f2f", b: "#a33a52", c: "#ffd9e4", rim: "#ff9fbe",
    bg: ["#0c0510", "#2a0c1f"],
    accent: { light: "#8a2e4a", dark: "#e58aa8" },
    night: { top: "#07030b", horizon: "#3a1230", deep: "#0e0514", crest: "#6e2440" },
    day: { top: "#c9aecb", horizon: "#f3e6ee", deep: "#5b2a4d", crest: "#cd8fab" },
  },
  calypso: {
    name: "Calypso",
    a: "#0e5e57", b: "#5fd3a5", c: "#f7f3d0", rim: "#b5f5d9",
    bg: ["#03140f", "#0a3229"],
    accent: { light: "#1f6f5c", dark: "#6fd0b0" },
    night: { top: "#020c0c", horizon: "#0f3d3a", deep: "#031517", crest: "#1f8a7a" },
    day: { top: "#a3d9c8", horizon: "#eef6ef", deep: "#1d6f6a", crest: "#93e1c9" },
  },
};

export const DEFAULTS = { palette: "aegean", tide: 0.45, wind: 0.35, swell: 0.4 };

const clamp01 = (v, d) => (Number.isFinite(+v) ? Math.min(1, Math.max(0, +v)) : d);

export function loadSaved() {
  let raw = {};
  try { raw = JSON.parse(localStorage.getItem("mechus.blob") || "{}") || {}; } catch { raw = {}; }
  return {
    palette: PALETTES[raw.palette] ? raw.palette : DEFAULTS.palette,
    tide: clamp01(raw.tide, DEFAULTS.tide),
    wind: clamp01(raw.wind, DEFAULTS.wind),
    swell: clamp01(raw.swell, DEFAULTS.swell),
  };
}

export function saveBlob(cfg) {
  try { localStorage.setItem("mechus.blob", JSON.stringify(cfg)); } catch { /* private mode */ }
}

// Slider (0..1) -> shader units.
const tideAmp = (t) => 0.05 + t * 0.32;
const swellFreq = (s) => 0.6 + s * 2.4;
const windSpeed = (w) => 0.08 + w * 1.1;

// 3D simplex noise, Ashima Arts / Stefan Gustavson (MIT).
export const NOISE_GLSL = /* glsl */ `
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1.0/6.0,1.0/3.0);
  const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy));
  vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz);
  vec3 l=1.0-g;
  vec3 i1=min(g.xyz,l.zxy);
  vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx;
  vec3 x2=x0-i2+C.yyy;
  vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857;
  vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z);
  vec4 x_=floor(j*ns.z);
  vec4 y_=floor(j-7.0*x_);
  vec4 x=x_*ns.x+ns.yyyy;
  vec4 y=y_*ns.x+ns.yyyy;
  vec4 h=1.0-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy);
  vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0;
  vec4 s1=floor(b1)*2.0+1.0;
  vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy;
  vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x);
  vec3 p1=vec3(a0.zw,h.y);
  vec3 p2=vec3(a1.xy,h.z);
  vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x;p1*=norm.y;p2*=norm.z;p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0);
  m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}
`;

const VERT = /* glsl */ `
uniform float uTime;
uniform float uTide;
uniform float uSwell;
uniform float uHover;
uniform vec3 uPointer;
varying vec3 vNormal;
varying vec3 vViewDir;
varying float vNoise;
${NOISE_GLSL}
float field(vec3 p){
  float n = snoise(p * uSwell + vec3(0.0, 0.0, uTime));
  n += 0.35 * snoise(p * uSwell * 2.1 - vec3(uTime * 0.7));
  float bulge = pow(max(dot(normalize(p), uPointer), 0.0), 4.0) * uHover;
  return n * uTide + bulge * 0.16;
}
vec3 displaced(vec3 p){ vec3 d = normalize(p); return d + d * field(d); }
void main(){
  vec3 n = normalize(normal);
  vec3 t = normalize(cross(n, abs(n.y) < 0.99 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0)));
  vec3 b = normalize(cross(n, t));
  float d0 = field(n);
  vec3 p0 = n + n * d0;
  vec3 p1 = displaced(n + t * 0.012);
  vec3 p2 = displaced(n + b * 0.012);
  vec3 dn = normalize(cross(p1 - p0, p2 - p0));
  if (dot(dn, n) < 0.0) dn = -dn;
  vNoise = d0 / max(uTide, 0.001);
  vec4 world = modelMatrix * vec4(p0, 1.0);
  vNormal = normalize(mat3(modelMatrix) * dn);
  vViewDir = normalize(cameraPosition - world.xyz);
  gl_Position = projectionMatrix * viewMatrix * world;
}
`;

const FRAG = /* glsl */ `
uniform vec3 uA;
uniform vec3 uB;
uniform vec3 uC;
uniform vec3 uRim;
uniform float uIntensity;
uniform float uTime;
varying vec3 vNormal;
varying vec3 vViewDir;
varying float vNoise;
void main(){
  vec3 N = normalize(vNormal);
  vec3 V = normalize(vViewDir);
  float ndv = max(dot(N, V), 0.0);
  float fres = pow(1.0 - ndv, 2.4);
  vec3 col = mix(uA, uB, smoothstep(-0.9, 0.9, vNoise));
  vec3 film = 0.5 + 0.5 * cos(6.2831 * (vec3(0.0, 0.33, 0.67) + ndv * 1.2 + vNoise * 0.35 + uTime * 0.05));
  col = mix(col, col * film * 1.6, 0.2);
  vec3 L = normalize(vec3(-0.4, 0.8, 0.6));
  col *= 0.55 + 0.6 * max(dot(N, L), 0.0);
  float spec = pow(max(dot(N, normalize(L + V)), 0.0), 48.0);
  col += uC * spec * 0.55;
  col += uRim * fres * 0.9;
  gl_FragColor = vec4(col * uIntensity, 1.0);
  #include <colorspace_fragment>
}
`;

export function createBlob(cfg = DEFAULTS, { detail = 36 } = {}) {
  const pal = PALETTES[cfg.palette] || PALETTES.aegean;
  const uniforms = {
    uTime: { value: 0 },
    uTide: { value: tideAmp(cfg.tide) },
    uSwell: { value: swellFreq(cfg.swell) },
    uHover: { value: 0 },
    uPointer: { value: new THREE.Vector3(0, 0, 1) },
    uA: { value: new THREE.Color(pal.a) },
    uB: { value: new THREE.Color(pal.b) },
    uC: { value: new THREE.Color(pal.c) },
    uRim: { value: new THREE.Color(pal.rim) },
    uIntensity: { value: 1 },
  };
  const material = new THREE.ShaderMaterial({ uniforms, vertexShader: VERT, fragmentShader: FRAG });
  const mesh = new THREE.Mesh(new THREE.IcosahedronGeometry(1, detail), material);

  const target = {
    tide: tideAmp(cfg.tide), swell: swellFreq(cfg.swell), wind: windSpeed(cfg.wind),
    a: new THREE.Color(pal.a), b: new THREE.Color(pal.b), c: new THREE.Color(pal.c), rim: new THREE.Color(pal.rim),
    hover: 0, pointer: new THREE.Vector3(0, 0, 1), intensity: 1,
  };
  let wind = target.wind;
  let phase = Math.random() * 10;
  let splash = 0;
  let clock = 0;
  const rot = { x: 0, y: 0 };
  const inv = new THREE.Quaternion();

  return {
    mesh,
    uniforms,
    set(next) {
      const p = PALETTES[next.palette] || pal;
      target.tide = tideAmp(clamp01(next.tide, DEFAULTS.tide));
      target.swell = swellFreq(clamp01(next.swell, DEFAULTS.swell));
      target.wind = windSpeed(clamp01(next.wind, DEFAULTS.wind));
      target.a.set(p.a); target.b.set(p.b); target.c.set(p.c); target.rim.set(p.rim);
    },
    setIntensity(v) { target.intensity = v; },
    pointer(x, y) {
      target.pointer.set(x, y, 0.85).normalize();
      target.hover = 1;
      rot.y = x * 0.35;
      rot.x = -y * 0.25;
    },
    leave() { target.hover = 0; rot.x = rot.y = 0; },
    splash() { splash = 1; },
    // Apply all changes at once (reduced motion: no easing).
    snap() {
      uniforms.uTide.value = target.tide; uniforms.uSwell.value = target.swell; wind = target.wind;
      uniforms.uA.value.copy(target.a); uniforms.uB.value.copy(target.b);
      uniforms.uC.value.copy(target.c); uniforms.uRim.value.copy(target.rim);
      uniforms.uIntensity.value = target.intensity;
    },
    update(dt) {
      dt = Math.min(dt, 0.1);
      clock += dt;
      const k = 1 - Math.exp(-dt * 3.5);
      wind += (target.wind - wind) * k;
      phase += dt * wind;
      splash *= Math.exp(-dt * 2.2);
      uniforms.uTime.value = phase;
      uniforms.uTide.value += (target.tide + splash * 0.35 - uniforms.uTide.value) * k * 1.6;
      uniforms.uSwell.value += (target.swell - uniforms.uSwell.value) * k;
      uniforms.uHover.value += (target.hover - uniforms.uHover.value) * k;
      uniforms.uIntensity.value += (target.intensity - uniforms.uIntensity.value) * k;
      uniforms.uA.value.lerp(target.a, k); uniforms.uB.value.lerp(target.b, k);
      uniforms.uC.value.lerp(target.c, k); uniforms.uRim.value.lerp(target.rim, k);
      mesh.rotation.y += (rot.y + clock * 0.06 - mesh.rotation.y) * k * 0.6;
      mesh.rotation.x += (rot.x - mesh.rotation.x) * k * 0.6;
      inv.copy(mesh.quaternion).invert();
      uniforms.uPointer.value.copy(target.pointer).applyQuaternion(inv);
      const breathe = 1 + Math.sin(clock * 0.9) * 0.018;
      mesh.userData.breathe = breathe;
    },
    dispose() { mesh.geometry.dispose(); material.dispose(); },
  };
}
