// Low-poly ocean + gradient sky for the dashboard hero. Night palette in dark
// mode (with a few navigation stars), day palette in light mode.
import * as THREE from "/static/vendor/three.module.min.js";
import { NOISE_GLSL, PALETTES } from "/static/js/blob.js";

const SEA_VERT = /* glsl */ `
uniform float uTime;
varying vec3 vWorld;
varying float vH;
${NOISE_GLSL}
float wave(vec2 p, vec2 dir, float freq, float speed, float amp){
  return sin(dot(p, normalize(dir)) * freq + uTime * speed) * amp;
}
void main(){
  vec3 p = position;
  vec2 q = p.xz;
  float h = wave(q, vec2(1.0, 0.3), 0.32, 0.8, 0.24)
          + wave(q, vec2(-0.6, 1.0), 0.55, 1.2, 0.12)
          + wave(q, vec2(0.2, -1.0), 1.05, 1.8, 0.05)
          + snoise(vec3(q * 0.22, uTime * 0.12)) * 0.2;
  p.y += h;
  vH = h;
  vec4 w = modelMatrix * vec4(p, 1.0);
  vWorld = w.xyz;
  gl_Position = projectionMatrix * viewMatrix * w;
}
`;

const SEA_FRAG = /* glsl */ `
uniform vec3 uDeep;
uniform vec3 uCrest;
uniform vec3 uHorizon;
uniform vec3 uSun;
uniform vec3 uSunPos;
uniform float uFogNear;
uniform float uFogFar;
varying vec3 vWorld;
varying float vH;
void main(){
  vec3 N = normalize(cross(dFdx(vWorld), dFdy(vWorld)));
  if (N.y < 0.0) N = -N;
  vec3 V = normalize(cameraPosition - vWorld);
  float fres = pow(1.0 - max(dot(N, V), 0.0), 3.0);
  vec3 col = mix(uDeep, uCrest, smoothstep(-0.35, 0.45, vH));
  col = mix(col, uHorizon, fres * 0.55);
  vec3 L = normalize(uSunPos - vWorld);
  vec3 R = reflect(-V, N);
  col += uSun * pow(max(dot(R, L), 0.0), 70.0) * 1.3;
  col += uSun * pow(max(dot(R, L), 0.0), 8.0) * 0.08;
  float d = length(vWorld.xz - cameraPosition.xz);
  col = mix(col, uHorizon, smoothstep(uFogNear, uFogFar, d));
  gl_FragColor = vec4(col, 1.0);
  #include <colorspace_fragment>
}
`;

const SKY_VERT = /* glsl */ `
varying vec3 vWorld;
void main(){
  vec4 w = modelMatrix * vec4(position, 1.0);
  vWorld = w.xyz;
  gl_Position = projectionMatrix * viewMatrix * w;
}
`;

const SKY_FRAG = /* glsl */ `
uniform vec3 uTop;
uniform vec3 uHorizon;
uniform vec3 uSun;
uniform vec3 uSunPos;
uniform float uNight;
varying vec3 vWorld;
float hash(vec3 p){ return fract(sin(dot(p, vec3(12.9898, 78.233, 37.719))) * 43758.5453); }
void main(){
  vec3 dir = normalize(vWorld - cameraPosition);
  vec3 col = mix(uHorizon, uTop, smoothstep(0.0, 0.42, dir.y));
  vec3 sd = normalize(uSunPos - cameraPosition);
  float s = max(dot(dir, sd), 0.0);
  col += uSun * (pow(s, 6.0) * 0.18 + pow(s, 40.0) * 0.25);
  vec3 cell = floor(dir * 420.0);
  float star = step(0.9986, hash(cell)) * smoothstep(0.08, 0.45, dir.y) * uNight;
  col += vec3(star) * (0.55 + 0.45 * hash(cell + 3.1));
  gl_FragColor = vec4(col, 1.0);
  #include <colorspace_fragment>
}
`;

export function createSea(paletteKey = "aegean", { day = false } = {}) {
  const group = new THREE.Group();
  const seaUniforms = {
    uTime: { value: 0 },
    uDeep: { value: new THREE.Color() },
    uCrest: { value: new THREE.Color() },
    uHorizon: { value: new THREE.Color() },
    uSun: { value: new THREE.Color() },
    uSunPos: { value: new THREE.Vector3(0, 3, -18) },
    uFogNear: { value: 12 },
    uFogFar: { value: 46 },
  };
  const skyUniforms = {
    uTop: { value: new THREE.Color() },
    uHorizon: seaUniforms.uHorizon,
    uSun: seaUniforms.uSun,
    uSunPos: seaUniforms.uSunPos,
    uNight: { value: day ? 0 : 1 },
  };

  const seaGeo = new THREE.PlaneGeometry(120, 90, 150, 110);
  seaGeo.rotateX(-Math.PI / 2);
  seaGeo.translate(0, 0, -25);
  const sea = new THREE.Mesh(seaGeo, new THREE.ShaderMaterial({ uniforms: seaUniforms, vertexShader: SEA_VERT, fragmentShader: SEA_FRAG }));
  const sky = new THREE.Mesh(
    new THREE.SphereGeometry(220, 32, 16),
    new THREE.ShaderMaterial({ uniforms: skyUniforms, vertexShader: SKY_VERT, fragmentShader: SKY_FRAG, side: THREE.BackSide, depthWrite: false }),
  );
  group.add(sky, sea);

  const target = { deep: new THREE.Color(), crest: new THREE.Color(), horizon: new THREE.Color(), top: new THREE.Color(), sun: new THREE.Color(), night: 0 };
  function setPalette(key, isDay) {
    const p = PALETTES[key] || PALETTES.aegean;
    const tone = isDay ? p.day : p.night;
    target.deep.set(tone.deep); target.crest.set(tone.crest);
    target.horizon.set(tone.horizon); target.top.set(tone.top);
    target.sun.set(p.rim);
    target.night = isDay ? 0 : 1;
  }
  setPalette(paletteKey, day);
  seaUniforms.uDeep.value.copy(target.deep); seaUniforms.uCrest.value.copy(target.crest);
  seaUniforms.uHorizon.value.copy(target.horizon); skyUniforms.uTop.value.copy(target.top);
  seaUniforms.uSun.value.copy(target.sun); skyUniforms.uNight.value = target.night;

  return {
    group,
    setPalette,
    setSun(v) { seaUniforms.uSunPos.value.copy(v); },
    update(dt, t) {
      seaUniforms.uTime.value = t * 0.9;
      const k = 1 - Math.exp(-Math.min(dt, 0.1) * 3);
      seaUniforms.uDeep.value.lerp(target.deep, k); seaUniforms.uCrest.value.lerp(target.crest, k);
      seaUniforms.uHorizon.value.lerp(target.horizon, k); skyUniforms.uTop.value.lerp(target.top, k);
      seaUniforms.uSun.value.lerp(target.sun, k);
      skyUniforms.uNight.value += (target.night - skyUniforms.uNight.value) * k;
    },
  };
}
