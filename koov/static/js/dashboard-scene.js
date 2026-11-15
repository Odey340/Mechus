// Dashboard hero: the navigator you shaped in /welcome rises as the sun over a
// low-poly sea. Scrolling tilts the camera down to the water while the panels
// glide up over it. Also owns smooth scrolling and reveal-on-scroll.
import * as THREE from "/static/vendor/three.module.min.js";
import { PALETTES, loadSaved, createBlob } from "/static/js/blob.js";
import { createSea } from "/static/js/sea.js";

const root = document.documentElement;
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const cfg = loadSaved();
const pal = PALETTES[cfg.palette];
const hero = document.getElementById("hero");
const heroInner = hero.querySelector(".hero-inner");
const cue = hero.querySelector(".scroll-cue");
const topbar = document.querySelector(".topbar");
const canvas = document.getElementById("scene");

const darkQuery = matchMedia("(prefers-color-scheme: dark)");
const isDay = () => (root.dataset.theme ? root.dataset.theme === "light" : !darkQuery.matches);

// ---------- fallback hero colours (used when WebGL is unavailable) ----------
function paintFallback() {
  const tone = isDay() ? pal.day : pal.night;
  const st = root.style;
  st.setProperty("--hero-top", tone.top); st.setProperty("--hero-h", tone.horizon); st.setProperty("--hero-deep", tone.deep);
  st.setProperty("--sun-a", pal.a); st.setProperty("--sun-b", pal.b); st.setProperty("--sun-c", pal.c);
}

// ---------- smooth scroll ----------
let lenis = null;
if (!reduced && typeof window.Lenis === "function") {
  lenis = new window.Lenis({ lerp: 0.085, smoothWheel: true, wheelMultiplier: 1 });
  window.__koovLenis = lenis;
}
const scrollY = () => (lenis ? lenis.scroll : window.scrollY);

document.querySelectorAll("[data-scrollto]").forEach((a) => a.addEventListener("click", (e) => {
  const t = document.querySelector(a.getAttribute("href"));
  if (!t) return;
  e.preventDefault();
  const panel = t.closest(".panel") || t;
  // Measure where the panel will sit once its reveal transform (translateY 32px) has finished.
  const pending = root.classList.contains("js-reveal") && !panel.classList.contains("in") ? 32 : 0;
  panel.classList.add("in");
  const top = panel.getBoundingClientRect().top + scrollY() - pending - 84;
  if (lenis) lenis.scrollTo(top, { duration: 1.5 });
  else window.scrollTo({ top, behavior: reduced ? "auto" : "smooth" });
  if (t.id === "joinTitle") setTimeout(() => document.getElementById("link").focus({ preventScroll: true }), lenis ? 1300 : 400);
}));

const dlg = document.getElementById("settings");
if (dlg && lenis) {
  new MutationObserver(() => (dlg.open ? lenis.stop() : lenis.start())).observe(dlg, { attributes: true, attributeFilter: ["open"] });
}

// ---------- reveal on scroll ----------
const reveals = [...document.querySelectorAll(".reveal")];
if (root.classList.contains("js-reveal") && "IntersectionObserver" in window) {
  const io = new IntersectionObserver((entries) => {
    for (const en of entries) if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
  }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
  reveals.forEach((el, i) => { el.style.setProperty("--d", (i % 2) * 110 + "ms"); io.observe(el); });
} else {
  reveals.forEach((el) => el.classList.add("in"));
}

// ---------- 3D scene ----------
let renderer = null;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
} catch {
  renderer = null;
}

let render = () => {};
let step = () => {};

if (!renderer) {
  root.classList.add("no-scene");
  paintFallback();
} else {
  root.classList.add("has-scene");
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(46, 1, 0.1, 500);
  const sea = createSea(cfg.palette, { day: isDay() });
  scene.add(sea.group);
  const sun = createBlob(cfg, { detail: 30 });
  scene.add(sun.mesh);

  const sunBase = new THREE.Vector3(7, 3.6, -26);
  let sunScale = 3.1;
  const look = new THREE.Vector3();
  const ptr = { x: 0, y: 0, tx: 0, ty: 0 };

  const layout = () => {
    const w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    const wide = w / h > 1.15;
    sunBase.set(wide ? 8.5 : 0, wide ? 3.8 : 6.2, -26);
    sunScale = wide ? 3.2 : 2.6;
  };

  let heroH = hero.offsetHeight || window.innerHeight;
  const onResize = () => { heroH = hero.offsetHeight || window.innerHeight; layout(); if (reduced) render(); };
  window.addEventListener("resize", onResize);
  layout();

  window.addEventListener("pointermove", (e) => {
    ptr.tx = (e.clientX / window.innerWidth) * 2 - 1;
    ptr.ty = -((e.clientY / window.innerHeight) * 2 - 1);
  }, { passive: true });

  const retheme = () => { sea.setPalette(cfg.palette, isDay()); if (reduced) { sea.update(10, 0); render(); } };
  new MutationObserver(retheme).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  if (darkQuery.addEventListener) darkQuery.addEventListener("change", retheme);

  let t = 0;
  const place = (p, dt) => {
    ptr.x += (ptr.tx - ptr.x) * Math.min(1, dt * 2.5);
    ptr.y += (ptr.ty - ptr.y) * Math.min(1, dt * 2.5);
    camera.position.set(ptr.x * 0.7, 1.5 + p * 2.6, 8 - p * 3.2);
    look.set(ptr.x * 1.4, 2.0 + ptr.y * 0.4 - p * 7.5, -30);
    camera.lookAt(look);
    sun.mesh.position.set(sunBase.x, sunBase.y + p * 3.0, sunBase.z);
    sun.mesh.scale.setScalar(sunScale * (sun.mesh.userData.breathe || 1));
    sun.setIntensity(1.05 - p * 0.35);
    sun.pointer(ptr.x * 0.6, ptr.y * 0.6);
    sea.setSun(sun.mesh.position);
  };

  render = () => {
    place(Math.min(1.2, scrollY() / heroH), 0.016);
    if (reduced) sun.snap();
    renderer.render(scene, camera);
  };

  step = (dt, p) => {
    t += dt;
    sun.update(dt);
    sea.update(dt, t);
    place(p, dt);
    renderer.render(scene, camera);
  };

  if (reduced) { sea.update(10, 0); render(); }
}

// ---------- frame loop: smooth scroll, parallax, render ----------
let last = performance.now();
let scrolled = null;
function chrome(y) {
  const p = Math.min(1.2, y / (hero.offsetHeight || window.innerHeight));
  heroInner.style.transform = `translate3d(0, ${(-p * 90).toFixed(1)}px, 0)`;
  heroInner.style.opacity = String(Math.max(0, 1 - p * 2.2));
  if (cue) cue.style.opacity = String(Math.max(0, 1 - p * 4));
  const s = y > 40;
  if (s !== scrolled) { scrolled = s; topbar.classList.toggle("scrolled", s); }
  return p;
}

if (reduced) {
  window.addEventListener("scroll", () => { chrome(window.scrollY); render(); }, { passive: true });
  chrome(window.scrollY);
} else {
  const tick = (now) => {
    const dt = Math.min(0.1, Math.max(0, (now - last) / 1000));
    last = now;
    if (lenis) lenis.raf(now);
    const p = chrome(scrollY());
    // The content background covers the scene once the hero has scrolled away.
    if (!document.hidden && p < 1.15) step(dt, p);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

window.__koovSceneReady = true;
