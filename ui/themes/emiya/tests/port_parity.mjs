// Port parity test for the Emiya forge field.
//
// Runs the mockup's own engine (tests/mockup_engine_ref.js, a verbatim copy) and the port
// (ForgeEngine in fx/emiya_forge_field.js) side by side: same seeded Math.random, same frame
// times, same heat targets, focus points and forge blade. Every canvas call both engines make is
// recorded as a string (method, arguments, current fill/stroke style) and the two logs must be
// identical, frame by frame. The port draws with colours parsed from static/emiya.css, so a match
// also proves the CSS tokens carry exactly the mockup's colour values.
//
// Usage: node tests/port_parity.mjs   -> prints one JSON line; exit code 1 on any mismatch.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { ForgeEngine } from '../fx/emiya_forge_field.js';
import { MockupEngine } from './mockup_engine_ref.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const W = 1280;
const H = 800;
// The measured trace field in the spike's page: right of the 176 px rail, below the 64 px header.
const FIELD = { left: 176, top: 64, right: 1280, bottom: 800 };
const SEEDS = [1, 2, 3, 4];
// Frame times in seconds, cycled: 60, 120, 280 Hz, a 50 ms hitch (the clamp) and 30 Hz.
const DTS = [1 / 60, 1 / 120, 1 / 280, 1 / 60, 0.05, 1 / 30];
// The scenario both engines play: heat targets and focus points as the mockup's syncHeat() set them.
const PHASES = [
  { name: 'idle', seconds: 3, target: 1, focus: null },
  { name: 'forging', seconds: 5, target: 1.35, focus: { x: 990, y: 560 }, blade: [990, 660] },
  { name: 'done', seconds: 3, target: 1, focus: null },
  { name: 'cold', seconds: 4, target: 0.6, focus: null },
  { name: 'switch', seconds: 4, target: 1.35, focus: { x: 380, y: 620 } },
  { name: 'generate', seconds: 4, target: 1.35, focus: { x: 900, y: 470 } },
  { name: 'idle', seconds: 2, target: 1, focus: null },
];

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// --emiya-fx-* hex values from the theme stylesheet (var() references resolved).
function paletteFromCss() {
  const css = readFileSync(join(HERE, '..', 'static', 'emiya.css'), 'utf8');
  const decl = {};
  for (const m of css.matchAll(/(--emiya-[\w-]+)\s*:\s*([^;]+);/g)) decl[m[1]] = m[2].trim();
  const resolve = (v) => {
    const m = /^var\((--[\w-]+)\)$/.exec(v);
    return m ? resolve(decl[m[1]]) : v;
  };
  const rgb = (name) => {
    const m = /^#([0-9a-f]{6})$/i.exec(resolve(decl[name] || ''));
    if (!m) throw new Error('token ' + name + ' is not a 6-digit hex colour');
    return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
  };
  return {
    embers: [rgb('--emiya-fx-ember-1'), rgb('--emiya-fx-ember-2'), rgb('--emiya-fx-ember-3')],
    emberHot: rgb('--emiya-fx-ember-hot'),
    traceShadow: rgb('--emiya-fx-trace-shadow'),
    trace: rgb('--emiya-fx-trace'),
    traceBlade: rgb('--emiya-fx-trace-blade'),
    glow: rgb('--emiya-fx-glow'),
    spark: rgb('--emiya-fx-spark'),
  };
}

// A 2D context stand-in that records every call as a string.
class RecordingContext {
  constructor() {
    this.calls = [];
    this.fillStyle = null;
    this.strokeStyle = null;
    this.lineWidth = 1;
    this.lineCap = 'butt';
    this.lineJoin = 'miter';
  }
  rec(...parts) { this.calls.push(parts.map(String).join('|')); }
  clearRect(...a) { this.rec('clearRect', ...a); }
  beginPath() { this.rec('beginPath'); }
  moveTo(x, y) { this.rec('moveTo', x, y); }
  lineTo(x, y) { this.rec('lineTo', x, y); }
  arc(...a) { this.rec('arc', ...a); }
  fill() {
    const s = this.fillStyle;
    this.rec('fill', s && s.stops ? 'gradient(' + s.args.join(',') + ';' + s.stops.join(';') + ')' : s);
  }
  stroke() { this.rec('stroke', this.strokeStyle, this.lineWidth, this.lineCap, this.lineJoin); }
  createRadialGradient(...args) {
    return { args: args, stops: [], addColorStop(offset, color) { this.stops.push(offset + ':' + color); } };
  }
  take() { const c = this.calls; this.calls = []; return c; }
}

function play(kind, seed, palette) {
  Math.random = mulberry32(seed);
  const ctx = new RecordingContext();
  let fx;
  let step;
  let draw;
  let blade;
  if (kind === 'mockup') {
    const m = new MockupEngine();
    m.fx = m.fxInit(W, H);
    fx = () => m.fx;
    step = (dt) => m.fxStep(dt);
    draw = () => m.fxDraw(ctx);
    blade = (x, y) => m.fxForgeBlade(x, y);
  } else {
    const e = new ForgeEngine(palette, null);
    e.setField(FIELD);
    e.init(W, H);
    fx = () => e.fx;
    step = (dt) => e.step(dt);
    draw = () => e.draw(ctx);
    blade = (x, y) => e.forgeBlade(x, y);
  }
  const frames = [];
  let i = 0;
  for (const phase of PHASES) {
    fx().heatTarget = phase.target;
    fx().focus = phase.focus ? { x: phase.focus.x, y: phase.focus.y } : null;
    if (phase.blade) blade(phase.blade[0], phase.blade[1]);
    for (let t = 0; t < phase.seconds; i++) {
      const dt = DTS[i % DTS.length];
      t += dt;
      step(dt);
      draw();
      frames.push(ctx.take());
    }
  }
  for (let k = 0; k < 300; k++) step(1 / 30); // Reduce Motion pre-roll, then one still frame
  draw();
  frames.push(ctx.take());
  // Final engine state; the port adds a "focused" flag to traces for telemetry, ignored here.
  const state = JSON.stringify(fx(), (key, value) => (key === 'focused' ? undefined : value));
  return { frames, state };
}

const palette = paletteFromCss();
const report = { ok: true, seeds: SEEDS.length, frames: 0, calls: 0, mismatches: 0, first: null, coverage: {} };
const coverage = { blade_strokes: 0, trace_strokes: 0, gradients: 0, still_frame_calls: 0 };
for (const seed of SEEDS) {
  const ref = play('mockup', seed, null);
  const port = play('port', seed, palette);
  if (ref.frames.length !== port.frames.length) {
    report.ok = false;
    report.mismatches += 1;
    report.first = report.first || { seed, reason: 'frame count', ref: ref.frames.length, port: port.frames.length };
  }
  for (let f = 0; f < Math.min(ref.frames.length, port.frames.length); f++) {
    const a = ref.frames[f];
    const b = port.frames[f];
    report.frames += 1;
    report.calls += a.length;
    const n = Math.max(a.length, b.length);
    for (let c = 0; c < n; c++) {
      if (a[c] !== b[c]) {
        report.ok = false;
        report.mismatches += 1;
        report.first = report.first || { seed, frame: f, call: c, mockup: a[c], port: b[c] };
        break;
      }
    }
    for (const call of b) {
      if (call.startsWith('stroke|rgba(200, 74, 50')) coverage.blade_strokes += 1;
      if (call.startsWith('stroke|rgba(172, 40, 44')) coverage.trace_strokes += 1;
      if (call.startsWith('fill|gradient(')) coverage.gradients += 1;
    }
  }
  coverage.still_frame_calls += port.frames[port.frames.length - 1].length;
  if (ref.state !== port.state) {
    report.ok = false;
    report.mismatches += 1;
    report.first = report.first || { seed, reason: 'final engine state differs' };
  }
}
report.coverage = coverage;
console.log(JSON.stringify(report));
process.exit(report.ok ? 0 : 1);
