// Wave and bar logic of fx/emiya_take_wave.js and fx/emiya_bars.js against the Emiya Voice mockup.
//
// MockupWave below is a VERBATIM copy of the mockup's clock (Main.dc.html L1424-1428), circuit
// (L1434-1456), pathD (L1458-1460), along (L1462-1473), barsFor (L1475-1492) and clipBarsFor
// (L1494-1510), from the file whose sha256 is
// a26e1d8cf5899ec55de68dfc23db36e98f8b8cfc112924826e2dba63a77eb3e2 (the source of mockup_engine_ref.js).
// The mockup draws pseudo-random bars; the real app feeds each bar's value v from the audio peaks and
// computes the height itself, so barValues/clipValues replay the mockup's draws to recover v (and are
// checked against the verbatim heights). The colour rules of the Forge, Voices and compare bars
// (L2635-2638, L2731-2733, L2773) decide which class each bar must carry.
//
// Usage: node ui/themes/emiya/tests/wave_logic.mjs   -> prints one JSON line; exit code 1 on any failure.
import * as wave from '../fx/emiya_take_wave.js';
import * as bars from '../fx/emiya_bars.js';

class MockupWave {
  constructor() {
    this.barCache = {};
    this.clipCache = {};
  }

  clock(t) {
    const m = Math.floor(t / 60);
    const sec = t - m * 60;
    return m + ':' + (sec < 10 ? '0' : '') + sec.toFixed(1);
  }

  circuit(w, h) {
    const mid = h / 2;
    const amp = Math.max(6, Math.min(18, h / 2 - 8));
    const pts = [[3, mid]];
    let x = 3;
    let up = true;
    let i = 0;
    while (x + 180 < w) {
      x += 34 + ((i * 37) % 46);
      pts.push([x, mid]);
      const dy = up ? -amp : amp;
      x += amp;
      pts.push([x, mid + dy]);
      x += 22 + ((i * 53) % 36);
      pts.push([x, mid + dy]);
      x += amp;
      pts.push([x, mid]);
      up = !up;
      i += 1;
    }
    pts.push([Math.max(4, w - 3), mid]);
    return pts;
  }

  pathD(pts) {
    return 'M' + pts.map((p) => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' L');
  }

  along(pts, prog) {
    const cum = [0];
    for (let i = 1; i < pts.length; i++) cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
    const target = Math.max(0, Math.min(1, prog)) * cum[cum.length - 1];
    for (let i = 1; i < pts.length; i++) {
      if (cum[i] >= target) {
        const f = (target - cum[i - 1]) / Math.max(0.001, cum[i] - cum[i - 1]);
        return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * f, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * f];
      }
    }
    return pts[pts.length - 1];
  }

  barsFor(seed) {
    if (this.barCache[seed]) return this.barCache[seed];
    const n = 230;
    let x = (seed * 7919 + 104729) % 233280;
    const rnd = () => { x = (x * 9301 + 49297) % 233280; return x / 233280; };
    const out = [];
    let left = 0;
    let amp = 0.6;
    for (let i = 0; i < n; i++) {
      if (left <= 0) { left = 12 + Math.floor(rnd() * 24); amp = 0.4 + rnd() * 0.6; }
      const env = Math.pow(Math.sin(Math.PI * (i + 0.5) / n), 0.3);
      const v = left <= 2 ? 0.03 + rnd() * 0.04 : (0.22 + 0.78 * rnd()) * amp * env;
      left -= 1;
      out.push({ h: Math.max(3, Math.round(3 + v * 50)), d: Math.round((i / n) * 240) });
    }
    this.barCache[seed] = out;
    return out;
  }

  clipBarsFor(idx) {
    if (this.clipCache[idx]) return this.clipCache[idx];
    const n = 230;
    let x = (idx * 4513 + 7727) % 233280;
    const rnd = () => { x = (x * 9301 + 49297) % 233280; return x / 233280; };
    const out = [];
    let left = 0;
    let amp = 0.6;
    for (let i = 0; i < n; i++) {
      if (left <= 0) { left = 10 + Math.floor(rnd() * 20); amp = 0.35 + rnd() * 0.65; }
      const v = left <= 2 ? 0.03 + rnd() * 0.05 : (0.2 + 0.8 * rnd()) * amp;
      left -= 1;
      out.push(Math.max(3, Math.round(3 + v * 56)));
    }
    this.clipCache[idx] = out;
    return out;
  }
}

// ---- the mockup's draws, returning v ----------------------------------------------------------

function barValues(seed) {
  const n = 230;
  let x = (seed * 7919 + 104729) % 233280;
  const rnd = () => { x = (x * 9301 + 49297) % 233280; return x / 233280; };
  const out = [];
  let left = 0;
  let amp = 0.6;
  for (let i = 0; i < n; i++) {
    if (left <= 0) { left = 12 + Math.floor(rnd() * 24); amp = 0.4 + rnd() * 0.6; }
    const env = Math.pow(Math.sin(Math.PI * (i + 0.5) / n), 0.3);
    out.push(left <= 2 ? 0.03 + rnd() * 0.04 : (0.22 + 0.78 * rnd()) * amp * env);
    left -= 1;
  }
  return out;
}

function clipValues(idx) {
  const n = 230;
  let x = (idx * 4513 + 7727) % 233280;
  const rnd = () => { x = (x * 9301 + 49297) % 233280; return x / 233280; };
  const out = [];
  let left = 0;
  let amp = 0.6;
  for (let i = 0; i < n; i++) {
    if (left <= 0) { left = 10 + Math.floor(rnd() * 20); amp = 0.35 + rnd() * 0.65; }
    out.push(left <= 2 ? 0.03 + rnd() * 0.05 : (0.2 + 0.8 * rnd()) * amp);
    left -= 1;
  }
  return out;
}

// ---- the mockup's colour rules and the classes that must carry them ---------------------------

const GOLD_72 = 'rgba(252, 219, 127, 0.72)';
const GOLD_70 = 'rgba(252, 219, 127, 0.7)';
const WHITE_42 = 'rgba(255, 255, 255, 0.42)';
const WHITE_35 = 'rgba(255, 255, 255, 0.35)';
const WHITE_12 = 'rgba(255, 255, 255, 0.12)';
// The theme stylesheet colours these classes inside .emiya-bars--forge / --voices with exactly these values.
const FORGE_CLASS_COLOUR = { 'emiya-bar in-timbre': GOLD_72, 'emiya-bar in-sel': WHITE_42, 'emiya-bar out': WHITE_12 };
const VOICES_CLASS_COLOUR = { 'emiya-bar in-timbre': GOLD_70, 'emiya-bar in-sel': WHITE_35 };

// M L2635-2638: gold in the selection up to 10 s, white after, dim outside it.
function mockupForgeColour(i, n, dur, sel) {
  const at = ((i + 0.5) / n) * dur;
  return at <= sel ? (at <= Math.min(sel, 10) ? GOLD_72 : WHITE_42) : WHITE_12;
}

// The zone the Forge behaviours send for the same bar (viewmodels.Bars).
function forgeZone(i, n, dur, sel) {
  const at = ((i + 0.5) / n) * dur;
  return at <= sel ? (at <= Math.min(sel, 10) ? 'timbre' : 'selection') : 'outside';
}

// M L2731-2733: gold up to 10 s of the clip, white after.
function mockupVoicesColour(i, dur) {
  const at = ((i + 0.5) / 138) * (dur || 10);
  return at <= 10 ? GOLD_70 : WHITE_35;
}

function voicesZone(i, dur) {
  return ((i + 0.5) / 138) * (dur || 10) <= 10 ? 'timbre' : 'rest';
}

// ---- checks ------------------------------------------------------------------------------------

const ref = new MockupWave();
const failures = [];
const stats = {};
let checks = 0;

function check(name, ok, detail) {
  checks += 1;
  if (!ok) failures.push({ name, detail: detail === undefined ? null : detail });
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const nearTie = (x) => Math.abs((x % 1) - 0.5) < 1e-6;
const COPIES = { take_wave: wave, bars: bars };

// circuit, pathD and along: both copies, many sizes.
check('circuit(692, 56) is the take wave trace', same(wave.circuit(692, 56), ref.circuit(692, 56)));
for (const [name, mod] of Object.entries(COPIES)) {
  let bad = null;
  for (const h of [20, 40, 56, 64, 80]) {
    for (let w = 1; w <= 900 && !bad; w++) {
      const pts = ref.circuit(w, h);
      if (!same(mod.circuit(w, h), pts)) bad = { w, h, what: 'circuit' };
      else if (mod.pathD(pts) !== ref.pathD(pts)) bad = { w, h, what: 'pathD' };
    }
  }
  check(name + ': circuit and pathD equal the mockup at every width 1-900', !bad, bad);
  let alongBad = null;
  for (const [w, h] of [[692, 56], [692, 64], [309, 64], [40, 64], [200, 64]]) {
    const pts = ref.circuit(w, h);
    for (let k = -50; k <= 1050 && !alongBad; k++) {
      const prog = k / 1000;
      if (!same(mod.along(pts, prog), ref.along(pts, prog))) alongBad = { w, h, prog };
    }
  }
  check(name + ': along equals the mockup for progress -0.05..1.05', !alongBad, alongBad);
  let offBad = null;
  for (let k = 0; k <= 1000; k++) {
    const prog = k / 1000;
    if (mod.traceOffset(prog) !== Math.round(1000 * (1 - prog))) offBad = { prog };
  }
  check(name + ': traceOffset is the mockup dash offset', !offBad && mod.traceOffset(-1) === 1000 && mod.traceOffset(2) === 0, offBad);
}

// The take wave: heights 3 + v * 50 and rise delays i / 230 * 240 ms, against barsFor.
let replayBad = null;
let takeBad = null;
for (let seed = 1; seed <= 80; seed++) {
  const mock = ref.barsFor(seed);
  const vs = barValues(seed);
  if (!same(vs.map((v) => Math.max(3, Math.round(3 + v * 50))), mock.map((b) => b.h))) replayBad = replayBad || { seed };
  const port = wave.waveBars(vs);
  for (let i = 0; i < port.length && !takeBad; i++) {
    if (port[i].h !== mock[i].h || port[i].d !== mock[i].d) takeBad = { seed, i, port: [port[i].h, port[i].d], mockup: [mock[i].h, mock[i].d] };
    if (!port[i].style.includes('width: 2px') || !port[i].style.includes('height: ' + port[i].h + 'px')) takeBad = { seed, i, style: port[i].style };
  }
}
check('barValues replays the mockup draws of barsFor', !replayBad, replayBad);
check('take bars: heights and rise delays equal barsFor for seeds 1-80', !takeBad, takeBad);
const flat = wave.waveBars([]);
check('take bars: a take without peaks is a flat 3 px row of 230', flat.length === 230 && flat.every((b) => b.h === 3));
check('take bars: v = 1 is 53 px, v = 0 is 3 px', wave.barHeight(1) === 53 && wave.barHeight(0) === 3);

// Forge heights: 3 + v * 56 (clipBarsFor).
let clipReplayBad = null;
let forgeBad = null;
for (let idx = 0; idx <= 80; idx++) {
  const mock = ref.clipBarsFor(idx);
  const vs = clipValues(idx);
  if (!same(vs.map((v) => Math.max(3, Math.round(3 + v * 56))), mock)) clipReplayBad = clipReplayBad || { idx };
  const port = bars.barModel('forge', vs.map((v) => [v, 'timbre']));
  for (let i = 0; i < port.length && !forgeBad; i++) if (port[i].h !== mock[i]) forgeBad = { idx, i, port: port[i].h, mockup: mock[i] };
}
check('clipValues replays the mockup draws of clipBarsFor', !clipReplayBad, clipReplayBad);
check('forge bars: heights equal clipBarsFor for clips 0-80', !forgeBad, forgeBad);

// Voices heights: the forge height x 0.72, at most 42 px (the mockup rounds twice: within 1 px).
let voicesBad = null;
let voicesExact = 0;
let voicesTotal = 0;
for (let idx = 0; idx <= 80; idx++) {
  const mock = ref.clipBarsFor(idx).slice(0, 138).map((h) => Math.max(3, Math.round(h * 0.72)));
  const vs = clipValues(idx).slice(0, 138);
  vs.forEach((v, i) => {
    const h = bars.barHeight('voices', v);
    voicesTotal += 1;
    if (h === mock[i]) voicesExact += 1;
    if (Math.abs(h - mock[i]) > 1 || h > 42 || h < 3) voicesBad = voicesBad || { idx, i, port: h, mockup: mock[i] };
  });
}
stats.voices_exact = voicesExact + '/' + voicesTotal;
check('voices bars: within 1 px of the mockup, never above 42 px', !voicesBad, voicesBad);
check('voices bars: v = 1 is 42 px', bars.barHeight('voices', 1) === 42 && bars.barHeight('voices', 0) === 3);

// Compare heights: max(3, round((3 + v * 50) * 0.7)) (section 6.5), at most 37 px; the mockup scaled the
// rounded take bars (every second one, the first 110), so it is within 1 px.
let compareBad = null;
let compareExact = 0;
let compareTotal = 0;
for (let seed = 1; seed <= 80; seed++) {
  const mock = ref.barsFor(seed).filter((br, i) => i % 2 === 0).slice(0, 110).map((br) => Math.max(3, Math.round(br.h * 0.7)));
  const vs = barValues(seed).filter((v, i) => i % 2 === 0).slice(0, 110);
  const port = bars.barModel('compare', vs);
  if (port.length !== 110) compareBad = compareBad || { seed, length: port.length };
  port.forEach((b, i) => {
    const spec = Math.max(3, Math.round((3 + vs[i] * 50) * 0.7));
    compareTotal += 1;
    if (b.h === mock[i]) compareExact += 1;
    if (b.h !== spec || Math.abs(b.h - mock[i]) > 1 || b.h > 37) compareBad = compareBad || { seed, i, port: b.h, spec, mockup: mock[i] };
  });
}
stats.compare_exact = compareExact + '/' + compareTotal;
check('compare bars: the section 6.5 formula, within 1 px of the mockup', !compareBad, compareBad);
check('compare bars: v = 1 is 37 px', bars.barHeight('compare', 1) === 37 && bars.barHeight('compare', 0) === 3);

// Colour classes: the zone each bar gets maps to the class that carries the mockup's colour.
let forgeClassBad = null;
for (const [dur, sel] of [[30, 13.42], [11.2, 11.2], [4.2, 4.2], [16, 15], [9, 9], [13.4, 13.4], [60, 14.9]]) {
  const pairs = clipValues(7).map((v, i) => [v, forgeZone(i, 230, dur, sel)]);
  const model = bars.barModel('forge', pairs);
  model.forEach((b, i) => {
    if (FORGE_CLASS_COLOUR[b.cls] !== mockupForgeColour(i, 230, dur, sel)) forgeClassBad = forgeClassBad || { dur, sel, i, cls: b.cls };
  });
}
check('forge bars: in-timbre, in-sel and out follow the mockup colours', !forgeClassBad, forgeClassBad);
let voicesClassBad = null;
for (const dur of [5.1, 9.9, 10, 11.2, 13.42, 15]) {
  const pairs = clipValues(3).slice(0, 138).map((v, i) => [v, voicesZone(i, dur)]);
  bars.barModel('voices', pairs).forEach((b, i) => {
    if (VOICES_CLASS_COLOUR[b.cls] !== mockupVoicesColour(i, dur)) voicesClassBad = voicesClassBad || { dur, i, cls: b.cls };
  });
}
check('voices bars: timbre and rest follow the mockup colours', !voicesClassBad, voicesClassBad);
check('zones map to classes', bars.zoneClass('timbre') === 'in-timbre' && bars.zoneClass('selection') === 'in-sel'
  && bars.zoneClass('rest') === 'in-sel' && bars.zoneClass('outside') === 'out' && bars.zoneClass('nope') === '');
check('compare bars: plain values carry no zone class', bars.barModel('compare', [0.5, 1]).every((b) => b.cls === 'emiya-bar'));
const flats = { forge: [230, 'emiya-bar out'], voices: [138, 'emiya-bar in-sel'], compare: [110, 'emiya-bar'] };
for (const [kind, [count, cls]] of Object.entries(flats)) {
  const model = bars.barModel(kind, []);
  check(kind + ' bars: no peaks draw a flat 3 px row of ' + count, model.length === count && model.every((b) => b.h === 3 && b.cls === cls));
}

// The Trace On overlay spans the selection: max(40, round(692 * sel / dur)) (M L2630).
let selBad = null;
for (const dur of [4.2, 6, 10, 11.2, 13.42, 15, 16, 30, 60]) {
  for (let k = 1; k <= Math.round(dur * 10); k++) {
    const sel = Math.min(dur, k / 10);
    const exact = (692 * sel) / dur;
    const mock = Math.max(40, Math.round(exact));
    const port = bars.selWidth((sel / dur) * 100);
    if (port !== mock && !nearTie(exact)) selBad = selBad || { dur, sel, port, mockup: mock };
  }
}
check('selWidth equals the mockup trace width', !selBad && bars.selWidth(1) === 40 && bars.selWidth(100) === 692, selBad);

// Time text: the mockup's m:ss.s. The port rounds to tenths first, which removes the mockup's two
// rounding quirks: 9.96 s read "0:010.0" and 59.96 s read "0:60.0" there; here they are 0:10.0 and 1:00.0.
let clockBad = null;
let quirks = 0;
for (let t = 0; t < 3600; t += 0.0137) {
  const sec = t - Math.floor(t / 60) * 60;
  if (nearTie(sec * 10)) continue;
  if ((sec >= 9.95 && sec < 10) || sec >= 59.95) {
    quirks += 1;
    continue;
  }
  if (wave.clock(t) !== ref.clock(t)) clockBad = clockBad || { t, port: wave.clock(t), mockup: ref.clock(t) };
}
stats.clock_quirks_skipped = quirks;
check('clock equals the mockup away from its two rounding quirks', !clockBad, clockBad);
check('clock: m:ss.s cases', wave.clock(1.9) === '0:01.9' && wave.clock(4.6) === '0:04.6' && wave.clock(0) === '0:00.0'
  && wave.clock(9.96) === '0:10.0' && wave.clock(59.96) === '1:00.0' && wave.clock(75.24) === '1:15.2'
  && wave.clock(600) === '10:00.0' && wave.clock(-1) === '0:00.0' && wave.clock(NaN) === '0:00.0'
  && ref.clock(9.96) === '0:010.0' && ref.clock(59.96) === '0:60.0');
check('timeText: "0:01.9 / 0:04.6"', wave.timeText(1.9, 4.6) === '0:01.9 / 0:04.6' && wave.timeText(0, 0) === '0:00.0 / 0:00.0');
check('playPct: clamped percent of the take', wave.playPct(1.15, 4.6) === 25 && wave.playPct(9, 4.6) === 100
  && wave.playPct(-1, 4.6) === 0 && wave.playPct(1, 0) === 0);
check('samePath: path only', wave.samePath('/media/take/7?v=3', '/media/take/7', 'file:///app/')
  && !wave.samePath('/media/take/7', '/media/take/70', 'file:///app/')
  && wave.samePath('file:///media/take/7', '/media/take/7', 'file:///app/'));

// The head glow and the playhead (M L357-358): geometry inline, colours and the pulse from classes.
const tpl = wave.default.template;
check('playhead: 1 px wide, the box high, centred on its position', wave.PLAYHEAD_STYLE.includes('width: 1px')
  && wave.PLAYHEAD_STYLE.includes('height: 56px') && wave.PLAYHEAD_STYLE.includes('margin-left: -0.5px'));
check('head glow: an 18 px round dot centred on the playhead at mid height', wave.HEAD_STYLE.includes('width: 18px; height: 18px')
  && wave.HEAD_STYLE.includes('margin: -9px 0 0 -9px') && wave.HEAD_STYLE.includes('top: 50%')
  && wave.GLOW_STYLE.includes('border-radius: 50%'));
check('head glow pulses only without Reduce Motion', tpl.includes("{ 'vt-head-glow': !reduced }") && tpl.includes("'emiya-wave-glow'"));
check('take wave template: scrub, bars, playhead, trace and spark parts',
  ['aria-label="Playback position"', 'class="vt-scrub"', "'emiya-wave-bar', 'is-played'", 'class="emiya-wave-playhead"',
    'aria-label="Generating the take"', 'class="emiya-trace-shadow"', 'class="emiya-trace-line"', 'class="emiya-trace-mid"',
    'class="emiya-trace-dot"', 'class="vt-spark"', "'vt-rise': !reduced", 'pathLength="1000"'].every((s) => tpl.includes(s)));
const btpl = bars.default.template;
check('bars template: kind scope, marks, selection edges, played row and trace',
  ["'emiya-bars--' + kindName", 'class="emiya-bars-mark-label"', 'class="emiya-bars-mark-line"', 'class="emiya-bars-sel"',
    'class="emiya-bar is-played"', 'class="emiya-trace-line"', 'stroke-width="1.2"', 'pathLength="1000"'].every((s) => btpl.includes(s)));

// The components' computed values, evaluated on stand-ins for the Vue instance.
function computedOf(component, props) {
  const self = { ...props };
  for (const [name, getter] of Object.entries(component.computed)) {
    Object.defineProperty(self, name, { get: () => getter.call(self), enumerable: true });
  }
  for (const [name, fn] of Object.entries(component.methods || {})) self[name] = fn.bind(self);
  return self;
}
const forgeView = computedOf(bars.default, {
  kind: 'forge', bars: [[0.5, 'timbre'], [0.2, 'outside']], marks: [{ label: 'Prompt 6 s', pct: 44.7 }],
  selPct: 44.7, progress: null, tracing: true, traceProgress: 0.25, aria: 'Waveform', shownTrace: 0.25,
  easeTrace: true, easeProgress: false,
});
const fPts = bars.circuit(309, 64);
const fHead = bars.along(fPts, 0.25);
check('bars component: the forge view', forgeView.kindName === 'forge' && forgeView.model.length === 2
  && forgeView.traceW === 309 && forgeView.showMarks && forgeView.markList[0].lineStyle.includes('left: 44.7%')
  && forgeView.selEndStyle.includes('left: 44.7%') && forgeView.selEndStyle.includes('margin-left: -2px')
  && forgeView.dashStyle.strokeDashoffset === 750 && forgeView.dashStyle.transition.startsWith('stroke-dashoffset')
  && forgeView.headStyle.transform === 'translate(' + fHead[0].toFixed(1) + 'px, ' + fHead[1].toFixed(1) + 'px)'
  && forgeView.rowStyle.includes('top: 20px') && !forgeView.hasProgress, forgeView.rowStyle);
const compareView = computedOf(bars.default, { kind: 'compare', bars: [0.1, 0.9], progress: 0.5, easeProgress: true, marks: [] });
check('bars component: the compare view', compareView.hasProgress && compareView.playedStyle.width === '50%'
  && compareView.playedStyle.transition.startsWith('width') && compareView.playedRowStyle.includes('width: 329px')
  && !compareView.showMarks && compareView.model.every((b) => b.cls === 'emiya-bar'));
const voicesView = computedOf(bars.default, { kind: 'voices', bars: [], marks: [] });
check('bars component: the voices view', voicesView.model.length === 138 && !voicesView.rowStyle.includes('absolute')
  && voicesView.spec.root.includes('flex: 1 1 auto'));
check('bars component: an unknown kind draws as forge', computedOf(bars.default, { kind: 'x', bars: [] }).kindName === 'forge');
const takeView = computedOf(wave.default, {
  take: { id: 7, bars: [0.5, 1], length_s: 4.6, src: '/media/take/7' }, generating: { active: false, fill: null }, reduce: true,
});
const genView = computedOf(wave.default, { take: { id: 7, bars: [], length_s: 4.6, src: '' }, generating: { active: true, fill: 0.4 } });
check('take wave component: the take and the generating views', !takeView.generatingNow && takeView.bars.length === 2
  && takeView.scrubMax === 4.6 && takeView.takeInfo().src === '/media/take/7' && takeView.isReduced()
  && genView.generatingNow && genView.takeInfo() === null && genView.bars.length === 230);

const report = { ok: failures.length === 0, checks, failures, stats };
console.log(JSON.stringify(report));
process.exit(report.ok ? 0 : 1);
