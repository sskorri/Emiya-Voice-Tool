// Emiya theme - the forge field ("Trace On" canvas), a NiceGUI custom component.
//
// ForgeEngine is a port of the canvas engine in the Emiya Voice mockup (Main.dc.html, "the forge
// field"): fxInit, fxEmber, fxMeasure, fxSmooth, fxTrace, fxMinX, fxTraceOnce, fxForgeBlade, fxStep,
// fxDot, fxGlow, fxPathTo and fxDraw, lifted byte for byte from the forge FX spike. Particle logic,
// timings, alphas and the heat easing are the mockup's, statement for statement
// (tests/port_parity.mjs compares every canvas call). What differs, on purpose:
//   * Colours are read once, at mount, from CSS custom properties (--emiya-fx-*) on the page.
//     This file holds no colour values; the theme stylesheet is the single source.
//   * Geometry is measured, not fixed: focus points come from elements with the class
//     "emiya-anchor-<name>", the trace field from the element named by options.field, all via
//     getBoundingClientRect relative to the canvas. The canvas follows its element's size and
//     devicePixelRatio (capped at 2, as in the mockup).
// FieldRunner is attachFx plus syncHeat: a frame loop capped by a timer (state.fps_cap, so it wakes
// about that many times a second on any display), one pre-rolled still frame under Reduce Motion, no
// frames at all while state.paused (the window is minimized) or the document is hidden, and
// telemetry only with options.telemetry (tests/runner_cadence.mjs checks the cadence).
// Python drives it through props.state; see forge_field.py for the state shape.

// syncHeat() targets: normal, busy (switch / generate / forge, with Work Glow on), no model loaded.
const HEAT = { idle: 1, working: 1.35, cold: 0.6 };
const FOCUS_MIN_HEAT = 1.1;    // fxTraceOnce(): traces gather at the focus only above this heat
const FOCUS_SPREAD = 320;      // fxTraceOnce(): a square of this size centred on the focus
const EMBER_COUNT = 40;        // fxInit()
const STILL_STEPS = 300;       // attachFx(): Reduce Motion pre-rolls 300 steps of 1/30 s, then draws once
const MAX_DT = 0.05;           // attachFx(): longest simulated step per frame, in seconds
const DPR_MAX = 2;             // attachFx(): backing store resolution cap
const LONG_FRAME_MS = 25;      // telemetry: a frame-to-frame gap above this counts as a long frame
const TELEMETRY_MS = 1000;
const ANCHOR_PREFIX = 'emiya-anchor-';
// Trace-field insets from the measured field element. In the mockup's layout (176 px rail,
// 64 px header) they reproduce its spawn box (x 240..W-60, y 80..H-80), its "keep clear of the
// rail" retry (x < 190) and its ambient spark band (x 200..W-40).
const INSET = { left: 64, right: 60, top: 16, bottom: 80, clear: 14, sparkLeft: 24, sparkRight: 40 };
// The colour tokens the engine draws with. Their values live in the theme stylesheet.
const TOKENS = {
  ember1: '--emiya-fx-ember-1',
  ember2: '--emiya-fx-ember-2',
  ember3: '--emiya-fx-ember-3',
  emberHot: '--emiya-fx-ember-hot',
  traceShadow: '--emiya-fx-trace-shadow',
  trace: '--emiya-fx-trace',
  traceBlade: '--emiya-fx-trace-blade',
  glow: '--emiya-fx-glow',
  spark: '--emiya-fx-spark',
};

const round = (v, digits) => Math.round(v * 10 ** digits) / 10 ** digits;

// Canvas style strings for a token's channels, byte for byte the strings the mockup built inline:
// at alpha a (three decimals), and fully transparent (a glow's outer gradient stop).
const rgba = (c, a) => 'rgba(' + c[0] + ', ' + c[1] + ', ' + c[2] + ', ' + a.toFixed(3) + ')';
const rgbaClear = (c) => 'rgba(' + c[0] + ', ' + c[1] + ', ' + c[2] + ', 0)';

// Page-wide record of every field instance, so mounts, unmounts and loop state can be checked
// from outside (ui.run_javascript) even after an instance has gone.
function registry() {
  if (!window.__emiyaForgeField) window.__emiyaForgeField = { mounts: 0, unmounts: 0, instances: {} };
  return window.__emiyaForgeField;
}

// Resolve one CSS colour to [r, g, b] by letting the browser compute it on a probe element.
function toChannels(probe, value) {
  probe.style.color = '';
  probe.style.color = value;
  if (!probe.style.color) return null; // the browser rejected the value
  const computed = getComputedStyle(probe).color; // the legacy comma-separated rgb/rgba serialisation
  probe.style.color = '';
  if (!/^rgba?\(/.test(computed)) return null;
  const nums = computed.match(/[\d.]+/g);
  return nums && nums.length >= 3 ? nums.slice(0, 3).map(Number) : null;
}

// Read every colour token from CSS custom properties inherited by `el`.
function readPalette(el, probe) {
  const computed = getComputedStyle(el);
  const values = {};
  const channels = {};
  const missing = [];
  for (const [key, name] of Object.entries(TOKENS)) {
    const value = computed.getPropertyValue(name).trim();
    values[name] = value;
    const rgb = value ? toChannels(probe, value) : null;
    if (rgb) channels[key] = rgb;
    else missing.push(name);
  }
  const colors = missing.length
    ? null
    : {
        embers: [channels.ember1, channels.ember2, channels.ember3],
        emberHot: channels.emberHot,
        traceShadow: channels.traceShadow,
        trace: channels.trace,
        traceBlade: channels.traceBlade,
        glow: channels.glow,
        spark: channels.spark,
      };
  return { values, channels, missing, colors };
}

// ---------------------------------------------------------------------------------------------
// The engine: the mockup's fx* functions on a plain object (kept out of Vue's reactivity).
// ---------------------------------------------------------------------------------------------
class ForgeEngine {
  constructor(colors, onSpawn) {
    this.pal = colors; // null when a token is missing: the engine then never draws
    this.onSpawn = onSpawn;
    this.box = null;
    this.fx = null;
  }

  get ready() {
    return !!this.pal && !!this.fx && !!this.box;
  }

  // Spawn bounds from the measured trace field (replaces the mockup's fixed 240/80/190/200 px).
  setField(f) {
    this.box = {
      xMin: f.left + INSET.left,
      xMax: f.right - INSET.right,
      yMin: f.top + INSET.top,
      yMax: f.bottom - INSET.bottom,
      clearX: f.left + INSET.clear,
      sparkMin: f.left + INSET.sparkLeft,
      sparkMax: f.right - INSET.sparkRight,
    };
  }

  // fxInit
  init(W, H) {
    const fx = { W: W, H: H, t: 0, embers: [], traces: [], sparks: [], nextTrace: 0.8, nextSpark: 4, heat: 1, heatTarget: 1, focus: null };
    for (let i = 0; i < EMBER_COUNT; i++) {
      const e = this.ember(fx);
      e.life = Math.random() * e.max * 0.8;
      e.y -= e.vy * e.life;
      fx.embers.push(e);
    }
    this.fx = fx;
    return fx;
  }

  // fxEmber
  ember(fx) {
    const r = Math.random;
    const hot = r() < 0.12;
    const palette = this.pal ? this.pal.embers : [];
    return {
      x: r() * fx.W, y: fx.H * (0.5 + r() * 0.55), vy: 6 + r() * 12,
      sway: 3 + r() * 9, freq: 0.12 + r() * 0.35, phase: r() * Math.PI * 2,
      size: hot ? 1.1 + r() * 0.6 : 0.5 + r() * 1.0,
      life: 0, max: 8 + r() * 8, flick: 1.2 + r() * 2.4,
      rgb: hot ? (this.pal ? this.pal.emberHot : null) : palette[Math.floor(r() * palette.length)],
      base: hot ? 0.8 : 0.4 + r() * 0.25,
    };
  }

  // fxMeasure
  measure(p) {
    p.cum = [0];
    let len = 0;
    for (let i = 1; i < p.pts.length; i++) {
      len += Math.hypot(p.pts[i][0] - p.pts[i - 1][0], p.pts[i][1] - p.pts[i - 1][1]);
      p.cum.push(len);
    }
    p.len = Math.max(len, 0.001);
    return p;
  }

  // fxSmooth (Chaikin corner cutting)
  smooth(pts, passes) {
    let out = pts;
    for (let k = 0; k < passes; k++) {
      const next = [out[0]];
      for (let i = 0; i < out.length - 1; i++) {
        const a = out[i];
        const b = out[i + 1];
        next.push([a[0] * 0.75 + b[0] * 0.25, a[1] * 0.75 + b[1] * 0.25]);
        next.push([a[0] * 0.25 + b[0] * 0.75, a[1] * 0.25 + b[1] * 0.75]);
      }
      next.push(out[out.length - 1]);
      out = next;
    }
    return out;
  }

  // fxTrace: retry (up to 6 times) a trace that would reach into the rail.
  trace(fx) {
    let tr = this.traceOnce(fx);
    for (let i = 0; i < 6 && this.minX(tr) < this.box.clearX; i++) tr = this.traceOnce(fx);
    if (this.onSpawn) this.onSpawn(tr);
    return tr;
  }

  // fxMinX
  minX(tr) {
    let m = Infinity;
    tr.paths.forEach((p) => p.pts.forEach((q) => { if (q[0] < m) m = q[0]; }));
    return m;
  }

  // fxTraceOnce: a blade (15 %) or a circuit trace with up to two branches.
  traceOnce(fx) {
    const r = Math.random;
    const B = this.box;
    const blade = r() < 0.15;
    let x0 = B.xMin + r() * (B.xMax - B.xMin);
    let y0 = B.yMin + r() * (B.yMax - B.yMin);
    const focused = !!(fx.focus && fx.heat > FOCUS_MIN_HEAT);
    if (focused) {
      x0 = Math.max(B.xMin, Math.min(B.xMax, fx.focus.x + (r() - 0.5) * FOCUS_SPREAD));
      y0 = Math.max(B.yMin, Math.min(B.yMax, fx.focus.y + (r() - 0.5) * FOCUS_SPREAD));
    }
    const paths = [];
    let speed;
    if (blade) {
      const a = -Math.PI / 2 + (r() - 0.5) * 0.9;
      const len = 70 + r() * 70;
      const main = this.measure({ pts: [[x0, y0], [x0 + Math.cos(a) * len, y0 + Math.sin(a) * len]], delay: 0 });
      speed = main.len / 1.1;
      const gx = x0 + Math.cos(a) * len * 0.2;
      const gy = y0 + Math.sin(a) * len * 0.2;
      const px = Math.cos(a + Math.PI / 2) * 9;
      const py = Math.sin(a + Math.PI / 2) * 9;
      paths.push(main);
      paths.push(this.measure({ pts: [[gx - px, gy - py], [gx + px, gy + py]], delay: (len * 0.2) / speed + 0.08 }));
    } else {
      let x = x0;
      let y = y0;
      let a = Math.floor(r() * 8) * Math.PI / 4;
      const pts = [[x, y]];
      const nodes = [];
      const segs = 6 + Math.floor(r() * 8);
      for (let i = 0; i < segs; i++) {
        if (i > 0 && r() < 0.32) {
          a += (r() < 0.5 ? -1 : 1) * Math.PI / 4;
          nodes.push(pts.length - 1);
        }
        a += (r() - 0.5) * 0.16;
        const step = 12 + r() * 18;
        x += Math.cos(a) * step;
        y += Math.sin(a) * step;
        pts.push([x, y]);
      }
      const main = this.measure({ pts: this.smooth(pts, 2), delay: 0 });
      speed = main.len / (1.6 + r() * 1.0);
      paths.push(main);
      nodes.slice(0, 2).forEach((idx, k) => {
        if (r() < 0.6) {
          const o = pts[idx];
          let near = 0;
          let best = Infinity;
          main.pts.forEach((q, qi) => {
            const dd = (q[0] - o[0]) * (q[0] - o[0]) + (q[1] - o[1]) * (q[1] - o[1]);
            if (dd < best) { best = dd; near = qi; }
          });
          let bx = main.pts[near][0];
          let by = main.pts[near][1];
          let ba = Math.floor(r() * 8) * Math.PI / 4;
          const bpts = [[bx, by]];
          const n = 2 + Math.floor(r() * 3);
          for (let j = 0; j < n; j++) {
            if (r() < 0.3) ba += (r() < 0.5 ? -1 : 1) * Math.PI / 4;
            const stp = 9 + r() * 12;
            bx += Math.cos(ba) * stp;
            by += Math.sin(ba) * stp;
            bpts.push([bx, by]);
          }
          paths.push(this.measure({ pts: this.smooth(bpts, 1), delay: main.cum[near] / speed + 0.1 + k * 0.15 }));
        }
      });
    }
    let end = 0;
    paths.forEach((p) => { end = Math.max(end, p.delay + p.len / speed); });
    return { paths: paths, blade: blade, speed: speed, end: end, age: 0, hold: 0.6 + r() * 0.8, fade: 1.6 + r() * 1.2, sparked: false, force: false, focused: focused };
  }

  // fxForgeBlade: a forced blade (always sparks) growing up from (x0, y0).
  forgeBlade(x0, y0) {
    if (!this.fx) return;
    const a = -Math.PI / 2 - 0.18;
    const len = 130;
    const main = this.measure({ pts: [[x0, y0], [x0 + Math.cos(a) * len, y0 + Math.sin(a) * len]], delay: 0 });
    const speed = main.len / 1.2;
    const gx = x0 + Math.cos(a) * len * 0.2;
    const gy = y0 + Math.sin(a) * len * 0.2;
    const px = Math.cos(a + Math.PI / 2) * 9;
    const py = Math.sin(a + Math.PI / 2) * 9;
    const guard = this.measure({ pts: [[gx - px, gy - py], [gx + px, gy + py]], delay: (len * 0.2) / speed + 0.08 });
    let end = 0;
    [main, guard].forEach((p) => { end = Math.max(end, p.delay + p.len / speed); });
    this.fx.traces.push({ paths: [main, guard], blade: true, speed: speed, end: end, age: 0, hold: 1.4, fade: 2.2, sparked: false, force: true, focused: false });
  }

  // fxStep: heat easing (faster up than down), ember drift, trace and spark scheduling.
  step(dt) {
    const fx = this.fx;
    const B = this.box;
    const r = Math.random;
    fx.t += dt;
    const tau = fx.heatTarget > fx.heat ? 0.8 : 1.5;
    fx.heat += (fx.heatTarget - fx.heat) * Math.min(1, dt / tau);
    const hot = Math.max(0, Math.min(1, (fx.heat - 1) / 0.35));
    const cold = Math.max(0, Math.min(1, (1 - fx.heat) / 0.4));
    const lift = 1 + 0.35 * hot;
    for (let i = 0; i < fx.embers.length; i++) {
      const e = fx.embers[i];
      e.life += dt;
      e.y -= e.vy * lift * dt;
      if (e.life >= e.max || e.y < -12) fx.embers[i] = this.ember(fx);
    }
    fx.nextTrace -= dt;
    if (fx.nextTrace <= 0) {
      if (fx.traces.length < (hot > 0.5 ? 4 : 3)) fx.traces.push(this.trace(fx));
      fx.nextTrace = (1.6 + r() * 2.8) * (1 + cold) / (1 + hot);
    }
    fx.traces.forEach((tr) => {
      tr.age += dt;
      if (!tr.sparked && tr.age >= tr.end) {
        tr.sparked = true;
        if (tr.force || r() < 0.55) {
          const tip = tr.paths[0].pts[tr.paths[0].pts.length - 1];
          fx.sparks.push({ x: tip[0], y: tip[1], age: 0, life: 0.9 + r() * 0.5 });
        }
      }
    });
    fx.traces = fx.traces.filter((tr) => tr.age < tr.end + tr.hold + tr.fade);
    fx.nextSpark -= dt;
    if (fx.nextSpark <= 0) {
      fx.sparks.push({ x: B.sparkMin + r() * (B.sparkMax - B.sparkMin), y: fx.H * (0.35 + r() * 0.6), age: 0, life: 0.9 + r() * 0.6 });
      fx.nextSpark = hot > 0.5 ? 2 + r() * 2 : 5 + r() * 5;
    }
    fx.sparks.forEach((sp) => { sp.age += dt; sp.y -= 8 * dt; });
    fx.sparks = fx.sparks.filter((sp) => sp.age < sp.life);
  }

  // fxDot
  dot(ctx, x, y, radius, c, a) {
    if (a <= 0.003) return;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 6.2832);
    ctx.fillStyle = rgba(c, a);
    ctx.fill();
  }

  // fxGlow
  glow(ctx, x, y, radius, c, a) {
    if (a <= 0.003 || radius <= 0.5) return;
    const grad = ctx.createRadialGradient(x, y, 0, x, y, radius);
    grad.addColorStop(0, rgba(c, a));
    grad.addColorStop(1, rgbaClear(c));
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 6.2832);
    ctx.fillStyle = grad;
    ctx.fill();
  }

  // fxPathTo: the drawn part of a path at progress `prog`; returns the moving head.
  pathTo(ctx, p, prog) {
    const target = prog * p.len;
    ctx.beginPath();
    ctx.moveTo(p.pts[0][0], p.pts[0][1]);
    let head = p.pts[0];
    for (let i = 1; i < p.pts.length; i++) {
      if (p.cum[i] <= target) {
        ctx.lineTo(p.pts[i][0], p.pts[i][1]);
        head = p.pts[i];
      } else {
        const seg = p.cum[i] - p.cum[i - 1];
        const f = seg > 0 ? (target - p.cum[i - 1]) / seg : 0;
        head = [p.pts[i - 1][0] + (p.pts[i][0] - p.pts[i - 1][0]) * f, p.pts[i - 1][1] + (p.pts[i][1] - p.pts[i - 1][1]) * f];
        ctx.lineTo(head[0], head[1]);
        break;
      }
    }
    return head;
  }

  // fxDraw: traces (shadow stroke, line, glowing head), then embers, then sparks. Returns embers drawn.
  draw(ctx) {
    const fx = this.fx;
    const pal = this.pal;
    const hot = Math.max(0, Math.min(1, (fx.heat - 1) / 0.35));
    const cold = Math.max(0, Math.min(1, (1 - fx.heat) / 0.4));
    const shown = Math.round(fx.embers.length * (1 - 0.4 * cold));
    const bright = 1 + 0.15 * hot;
    ctx.clearRect(0, 0, fx.W, fx.H);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    fx.traces.forEach((tr) => {
      const alpha = tr.age < tr.end + tr.hold ? 1 : Math.max(0, 1 - (tr.age - tr.end - tr.hold) / tr.fade);
      tr.paths.forEach((p) => {
        const prog = Math.max(0, Math.min(1, ((tr.age - p.delay) * tr.speed) / p.len));
        if (prog <= 0) return;
        const head = this.pathTo(ctx, p, prog);
        ctx.strokeStyle = rgba(pal.traceShadow, 0.14 * alpha);
        ctx.lineWidth = 3.2;
        ctx.stroke();
        ctx.strokeStyle = rgba(tr.blade ? pal.traceBlade : pal.trace, 0.55 * alpha);
        ctx.lineWidth = 1;
        ctx.stroke();
        if (prog < 1) {
          this.glow(ctx, head[0], head[1], 9, pal.glow, 0.35);
          this.dot(ctx, head[0], head[1], 1.4, pal.spark, 0.95);
        }
      });
    });
    for (let i = 0; i < shown; i++) {
      const e = fx.embers[i];
      const fadeIn = Math.min(1, e.life / 1.2);
      const fadeOut = Math.min(1, (e.max - e.life) / 2.5);
      const flick = 0.75 + 0.25 * Math.sin(fx.t * e.flick * 6.2832 + e.phase);
      const a = Math.max(0, Math.min(1, e.base * fadeIn * fadeOut * flick * bright));
      const x = e.x + e.sway * Math.sin(fx.t * e.freq * 6.2832 + e.phase);
      this.glow(ctx, x, e.y, e.size * 5, e.rgb, a * 0.3);
      this.dot(ctx, x, e.y, e.size, e.rgb, a);
    }
    fx.sparks.forEach((sp) => {
      const k = sp.age / sp.life;
      const a = k < 0.18 ? k / 0.18 : Math.max(0, 1 - (k - 0.18) / 0.82);
      const grow = k < 0.18 ? k / 0.18 : 1;
      this.glow(ctx, sp.x, sp.y, 14 * grow, pal.glow, 0.4 * a);
      this.dot(ctx, sp.x, sp.y, 1.2 + 1.2 * grow, pal.spark, a);
    });
    return shown;
  }
}

// ---------------------------------------------------------------------------------------------
// The runner: canvas, loop, state from Python, measurement and telemetry (attachFx + syncHeat).
// ---------------------------------------------------------------------------------------------
const TIMER_SLACK_MS = 2;      // scheduleNext(): wake this long before the due time, then wait for its vsync

class FieldRunner {
  constructor(root, canvas, options, emit) {
    this.root = root;
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.options = options || {};
    this.uid = this.options.uid || 'forge-field';
    this.telemetry = this.options.telemetry === true;
    this.emit = emit;
    this.engine = null;
    this.state = {};
    this.lastSeq = undefined;
    this.focusName = null;
    this.speed = 1;
    this.cap = 0;
    this.raf = 0;
    this.wake = 0;
    this.timer = 0;
    this.running = false;
    this.still = false;
    this.paused = false;
    this.hidden = false;
    this.lastTick = 0;
    this.lastDraw = 0;
    this.nextDue = 0;
    this.shown = 0;
    this.layout = null;
    this.dpr = 0;
    this.acc = this.freshAccumulator(performance.now());
    this.rx = [];
    this.spawned = [];
    this.errors = 0;
    this.sent = 0;
    this.totals = { frames: 0, ticks: 0 };
    this.record = null;
    this.loopFn = (now) => this.loop(now);
    this.wakeFn = () => {
      this.wake = 0;
      if (this.running) this.raf = requestAnimationFrame(this.loopFn);
    };
    this.onDpr = () => { this.watchDpr(); this.refreshGeometry(); };
    this.onMotion = () => this.apply(this.state, false);
    this.onVisibility = () => this.visibilityChanged();
    this.onError = (ev) => {
      const t = ev.target;
      const what = t && t !== window && (t.src || t.href) ? 'failed to load ' + (t.src || t.href) : ev.message;
      this.reportError('error', what || 'error', ev.filename, ev.lineno);
    };
    this.onRejection = (ev) => {
      const reason = ev.reason;
      this.reportError('rejection', reason && (reason.stack || reason.message) ? reason.stack || reason.message : String(reason));
    };
  }

  freshAccumulator(start) {
    return { start: start, frames: 0, ticks: 0, gaps: [], maxGap: 0, long: 0, work: 0, workMax: 0 };
  }

  mount(state) {
    const reg = registry();
    reg.mounts += 1;
    const previous = reg.instances[this.uid];
    this.record = reg.instances[this.uid] = {
      uid: this.uid, mounts: (previous ? previous.mounts : 0) + 1, mountedAt: Date.now(), unmountedAt: null,
      running: false, totals: this.totals, telemetry: 0,
    };
    this.motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    this.motionQuery.addEventListener('change', this.onMotion);
    document.addEventListener('visibilitychange', this.onVisibility);
    this.hidden = document.visibilityState === 'hidden';
    if (this.telemetry) {
      window.addEventListener('error', this.onError, true);
      window.addEventListener('unhandledrejection', this.onRejection);
    }

    this.palette = readPalette(this.root, this.canvas);
    this.refreshGeometry();
    this.engine = new ForgeEngine(this.palette.colors, this.telemetry ? (tr) => this.noteSpawn(tr) : null);
    this.engine.setField(this.layout.field);
    this.engine.init(this.layout.W, this.layout.H);
    this.lastSeq = state.seq;
    window.__emiyaFieldClock = this;
    this.apply(state, true); // syncHeat(true): start at the target heat, then run, hold a still frame or wait
    document.dispatchEvent(new Event('emiya-field-clock'));

    this.resizeObserver = new ResizeObserver(() => this.refreshGeometry());
    this.resizeObserver.observe(this.root);
    this.watchDpr();
    this.timer = setInterval(() => this.everySecond(), TELEMETRY_MS);
    this.emit('ready', {
      uid: this.uid, t_js: Date.now(), seq: state.seq,
      tokens: this.palette.values, channels: this.palette.channels, missing: this.palette.missing,
      layout: this.layoutSummary(), running: this.running, still: this.still, paused: this.paused,
    });
  }

  dispose() {
    this.stop();
    if (window.__emiyaFieldClock === this) {
      delete window.__emiyaFieldClock;
      document.dispatchEvent(new Event('emiya-field-clock'));
    }
    if (this.timer) clearInterval(this.timer);
    this.timer = 0;
    if (this.resizeObserver) this.resizeObserver.disconnect();
    if (this.dprQuery) this.dprQuery.removeEventListener('change', this.onDpr);
    if (this.motionQuery) this.motionQuery.removeEventListener('change', this.onMotion);
    document.removeEventListener('visibilitychange', this.onVisibility);
    if (this.telemetry) {
      window.removeEventListener('error', this.onError, true);
      window.removeEventListener('unhandledrejection', this.onRejection);
    }
    registry().unmounts += 1;
    if (this.record) {
      this.record.running = false;
      this.record.unmountedAt = Date.now();
    }
  }

  // A new state from Python (the Vue watcher). Re-renders that carry the same seq are ignored.
  receive(st) {
    if (!st || st.seq === this.lastSeq) return;
    this.lastSeq = st.seq;
    if (this.telemetry) {
      const t = Date.now();
      this.rx.push({ seq: st.seq, t_js: t });
      this.emit('ack', { uid: this.uid, seq: st.seq, t_js_rx: t });
    }
    this.refreshGeometry();
    const previousCap = this.cap;
    this.apply(st, false);
    if (this.running && this.cap !== previousCap) document.dispatchEvent(new Event('emiya-field-clock'));
  }

  // syncHeat(): heat target and focus from the state; then run, hold a still frame, or draw nothing at
  // all while paused (state.paused: the window is minimized) or while the document is hidden.
  apply(st, force) {
    this.state = st || {};
    const fx = this.engine.fx;
    const glow = this.state.glow !== false;
    let target = HEAT.idle;
    this.focusName = null;
    if (this.state.heat === 'working' && glow) {
      target = HEAT.working;
      this.focusName = this.state.focus || null;
    } else if (this.state.heat === 'cold') {
      target = HEAT.cold;
    }
    fx.heatTarget = target;
    fx.focus = this.focusName ? this.anchorPoint(this.focusName) : null;
    if (force) fx.heat = target;
    this.speed = typeof this.state.speed === 'number' && this.state.speed > 0 ? this.state.speed : 1;
    this.cap = typeof this.state.fps_cap === 'number' && this.state.fps_cap > 0 ? this.state.fps_cap : 0;
    this.paused = this.state.paused === true;
    const reduce = this.state.reduce === true || (this.state.reduce === 'system' && this.motionQuery.matches);
    if (this.paused || this.hidden) {
      this.stop();
    } else if (reduce) {
      if (!this.still) this.drawStill();
    } else if (!this.running) {
      this.start();
    }
  }

  visibilityChanged() {
    this.hidden = document.visibilityState === 'hidden';
    this.apply(this.state, false);
  }

  start() {
    this.still = false;
    if (this.running || !this.engine || !this.engine.ready) return;
    this.running = true;
    this.record.running = true;
    this.lastTick = 0;
    this.lastDraw = 0;
    this.nextDue = 0;
    this.raf = requestAnimationFrame(this.loopFn);
    document.dispatchEvent(new Event('emiya-field-clock'));
  }

  // Leaves nothing scheduled: no animation frame and no wake-up timer.
  stop() {
    const wasRunning = this.running;
    this.running = false;
    if (this.record) this.record.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
    if (this.wake) clearTimeout(this.wake);
    this.wake = 0;
    if (wasRunning) document.dispatchEvent(new Event('emiya-field-clock'));
  }

  // attachFx(), Reduce Motion branch: pre-roll the simulation, draw one frame and hold it.
  drawStill() {
    this.still = true;
    this.stop();
    if (!this.engine.ready) return;
    for (let i = 0; i < STILL_STEPS; i++) this.engine.step(1 / 30);
    this.shown = this.engine.draw(this.ctx);
  }

  // attachFx(), running branch: one step and one draw per callback (every callback that runs is due),
  // then the next wake-up.
  loop(now) {
    this.raf = 0;
    if (!this.running) return;
    this.totals.ticks += 1;
    if (this.telemetry) this.noteTick(now);
    const dt = this.lastDraw ? Math.min(MAX_DT, (now - this.lastDraw) / 1000) : 1 / 60;
    this.lastDraw = now;
    const t0 = this.telemetry ? performance.now() : 0;
    this.engine.step(dt * this.speed);
    this.shown = this.engine.draw(this.ctx);
    this.totals.frames += 1;
    if (this.telemetry) this.noteWork(performance.now() - t0);
    this.scheduleNext(now);
  }

  // The frame cap (state.fps_cap): sleep on a timer until just before the next due time, then ask for
  // one frame, so the loop wakes about cap times a second on any display instead of on every vsync.
  // Without a cap (0, a dev comparison only) it asks for every frame.
  scheduleNext(now) {
    if (!this.running) return;
    if (!this.cap) {
      this.raf = requestAnimationFrame(this.loopFn);
      return;
    }
    const period = 1000 / this.cap;
    const due = this.nextDue ? this.nextDue + period : 0;
    this.nextDue = due > now ? due : Math.floor(now / period + 1) * period;
    const wait = Math.max(0, this.nextDue - performance.now() - TIMER_SLACK_MS);
    this.wake = setTimeout(this.wakeFn, wait);
  }

  // Frame statistics, kept only with options.telemetry (the accumulator is emptied every second).
  noteTick(now) {
    const a = this.acc;
    a.ticks += 1;
    if (this.lastTick) {
      const gap = now - this.lastTick;
      a.gaps.push(gap);
      if (gap > a.maxGap) a.maxGap = gap;
      if (gap > LONG_FRAME_MS) a.long += 1;
    }
    this.lastTick = now;
  }

  noteWork(work) {
    const a = this.acc;
    a.frames += 1;
    a.work += work;
    if (work > a.workMax) a.workMax = work;
  }

  // Once a second: re-measure, so the anchors follow layout changes; with telemetry, also report.
  everySecond() {
    if (this.telemetry) this.sendTelemetry();
    else this.refreshGeometry();
  }

  // Canvas size, trace field and anchors, all relative to the canvas element.
  measure() {
    const base = this.root.getBoundingClientRect();
    const rel = (r) => ({ left: r.left - base.left, top: r.top - base.top, right: r.right - base.left, bottom: r.bottom - base.top });
    const fieldEl = this.options.field ? document.querySelector(this.options.field) : null;
    const field = fieldEl ? rel(fieldEl.getBoundingClientRect()) : { left: 0, top: 0, right: base.width, bottom: base.height };
    const anchors = {};
    for (const el of document.querySelectorAll('[class*="' + ANCHOR_PREFIX + '"]')) {
      for (const cls of el.classList) {
        if (!cls.startsWith(ANCHOR_PREFIX)) continue;
        const r = rel(el.getBoundingClientRect());
        anchors[cls.slice(ANCHOR_PREFIX.length)] = { x: (r.left + r.right) / 2, y: (r.top + r.bottom) / 2 };
      }
    }
    return { W: base.width, H: base.height, field: field, anchors: anchors };
  }

  refreshGeometry() {
    const next = this.measure();
    const dpr = Math.min(DPR_MAX, window.devicePixelRatio || 1);
    const resized = !this.layout || next.W !== this.layout.W || next.H !== this.layout.H || dpr !== this.dpr;
    this.layout = next;
    if (resized) {
      this.dpr = dpr;
      this.canvas.width = Math.round(next.W * dpr);
      this.canvas.height = Math.round(next.H * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    if (this.engine && this.engine.fx) {
      this.engine.setField(next.field);
      this.engine.fx.W = next.W;
      this.engine.fx.H = next.H;
      if (this.focusName) this.engine.fx.focus = this.anchorPoint(this.focusName);
      if (resized && this.still && this.engine.ready) this.shown = this.engine.draw(this.ctx); // resizing cleared it
    }
  }

  watchDpr() {
    if (this.dprQuery) this.dprQuery.removeEventListener('change', this.onDpr);
    this.dprQuery = window.matchMedia('(resolution: ' + (window.devicePixelRatio || 1) + 'dppx)');
    this.dprQuery.addEventListener('change', this.onDpr);
  }

  anchorPoint(name) {
    const p = this.layout && this.layout.anchors[name];
    return p ? { x: p.x, y: p.y } : null;
  }

  layoutSummary() {
    const L = this.layout;
    const anchors = {};
    for (const [name, p] of Object.entries(L.anchors)) anchors[name] = { x: round(p.x, 1), y: round(p.y, 1) };
    const f = L.field;
    return {
      W: round(L.W, 1), H: round(L.H, 1), dpr: this.dpr, canvas: [this.canvas.width, this.canvas.height],
      field: { left: round(f.left, 1), top: round(f.top, 1), right: round(f.right, 1), bottom: round(f.bottom, 1) },
      anchors: anchors,
    };
  }

  noteSpawn(tr) {
    const o = tr.paths[0].pts[0];
    const f = this.engine.fx.focus;
    this.spawned.push({
      x: round(o[0], 1), y: round(o[1], 1), blade: tr.blade, focused: tr.focused,
      fx: tr.focused && f ? round(f.x, 1) : null, fy: tr.focused && f ? round(f.y, 1) : null,
    });
    if (this.spawned.length > 50) this.spawned.shift();
  }

  reportError(kind, message, source, line) {
    this.errors += 1;
    this.emit('jserror', { uid: this.uid, kind: kind, message: String(message).slice(0, 500), source: source || '', line: line || 0, t_js: Date.now() });
  }

  sendTelemetry() {
    const now = performance.now();
    const a = this.acc;
    this.acc = this.freshAccumulator(now);
    const span = Math.max(0.001, (now - a.start) / 1000);
    this.refreshGeometry();
    const gaps = a.gaps.sort((x, y) => x - y);
    const fx = this.engine.fx;
    const mem = performance.memory;
    this.sent += 1;
    this.record.telemetry = this.sent;
    this.emit('telemetry', {
      uid: this.uid, n: this.sent, t_js: Date.now(), span_s: round(span, 3),
      frames: a.frames, fps: round(a.frames / span, 1), ticks: a.ticks, tick_hz: round(a.ticks / span, 1),
      gap_med_ms: gaps.length ? round(gaps[gaps.length >> 1], 3) : null,
      gap_max_ms: gaps.length ? round(a.maxGap, 2) : null,
      long_frames: a.long,
      work_avg_ms: a.frames ? round(a.work / a.frames, 4) : null,
      work_max_ms: a.frames ? round(a.workMax, 3) : null,
      heat: round(fx.heat, 4), heat_target: fx.heatTarget,
      focus: fx.focus ? { name: this.focusName, x: round(fx.focus.x, 1), y: round(fx.focus.y, 1) } : null,
      running: this.running, still: this.still, paused: this.paused, hidden: this.hidden,
      reduce: this.state.reduce === true, cap: this.cap, speed: this.speed,
      counts: { embers: this.shown, traces: fx.traces.length, sparks: fx.sparks.length },
      spawned: this.spawned.splice(0), rx: this.rx.splice(0),
      layout: this.layoutSummary(),
      mem: mem ? { used: mem.usedJSHeapSize, total: mem.totalJSHeapSize, limit: mem.jsHeapSizeLimit } : null,
      errors: this.errors, totals: { frames: this.totals.frames, ticks: this.totals.ticks }, mounts: registry().mounts,
    });
  }

  forgeBlade(name) {
    this.refreshGeometry();
    const p = this.anchorPoint(name);
    if (!p || !this.engine || !this.engine.fx) return null;
    this.engine.forgeBlade(p.x, p.y);
    return { x: round(p.x, 1), y: round(p.y, 1), traces: this.engine.fx.traces.length };
  }

  snapshot() {
    const fx = this.engine && this.engine.fx;
    return {
      uid: this.uid, running: this.running, still: this.still, paused: this.paused, hidden: this.hidden,
      lastSeq: this.lastSeq, telemetry: this.sent,
      totals: { frames: this.totals.frames, ticks: this.totals.ticks },
      heat: fx ? round(fx.heat, 4) : null,
      counts: fx ? { embers: this.shown, traces: fx.traces.length, sparks: fx.sparks.length } : null,
    };
  }
}

// Named exports for tests/port_parity.mjs and tests/runner_cadence.mjs (NiceGUI only uses the default export).
export { ForgeEngine, FieldRunner };

export default {
  template:
    '<div class="emiya-forge-field" style="position: absolute; inset: 0; pointer-events: none">' +
    '<canvas ref="canvas" aria-hidden="true" style="position: absolute; inset: 0; display: block; width: 100%; height: 100%"></canvas>' +
    '</div>',
  props: {
    state: Object,
    options: Object,
  },
  mounted() {
    this.runner = new FieldRunner(this.$el, this.$refs.canvas, this.options, (name, payload) => this.$emit(name, payload));
    this.runner.mount(this.state || {});
  },
  unmounted() {
    if (this.runner) this.runner.dispose();
    this.runner = null;
  },
  watch: {
    state(next) {
      if (this.runner) this.runner.receive(next);
    },
  },
  methods: {
    forgeBlade(anchor) {
      return this.runner ? this.runner.forgeBlade(anchor) : null;
    },
    snapshot() {
      return this.runner ? this.runner.snapshot() : null;
    },
  },
};
