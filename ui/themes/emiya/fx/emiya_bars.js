// Emiya theme - static waveform bars, a NiceGUI custom component.
//
// Three kinds, ported from the Emiya Voice mockup (Main.dc.html):
//   * forge:   the Reference Clip waveform (M L448-478): 230 bars, 3 + v * 56 px high in a 692 x 64 row
//              (M L1506), one class per zone, the Prompt and Timbre marks, the selection edges, and the
//              "Trace On" overlay circuit(selW, 64) revealed to the bake progress (M L464-477, L2643);
//   * voices:  the reference clip on the Voices details card: 138 bars at 0.72 of that height, at most
//              42 px (M L649-653, L2731-2733);
//   * compare: one side of the Takes compare card: 110 bars of max(3, round((3 + v * 50) * 0.7)) px, at
//              most 37 px, in a 329 x 40 box, with the played part as a second row clipped to the
//              progress (M L787-799, L2773).
// Bars arrive as [value, zone] pairs (or plain values) and get the classes emiya-bar plus in-timbre,
// in-sel or out. This file holds no colour values: every colour comes from a class of the theme stylesheet.
//
// circuit, pathD and along are also in emiya_take_wave.js: NiceGUI serves each component file on its own,
// so the two cannot share a module; tests/wave_logic.mjs checks both copies against the mockup.

const CLIP_W = 692;             // forge: the waveform's width; the trace overlay spans the selection
const TRACE_H = 64;
const EASE_S = 0.2;             // a growing progress eases over one core progress tick
const REDUCED_UPDATE_MS = 250;  // under Reduce Motion the trace overlay moves at most 4 times a second

export const KINDS = {
  forge: {
    count: 230, gain: 56, scale: 1, flat: 'out', rowTop: 20, rowH: 64, rowW: CLIP_W,
    root: 'position: relative; width: 692px; height: 86px',
  },
  voices: {
    count: 138, gain: 56, scale: 0.72, flat: 'in-sel', rowTop: 0, rowH: 48, rowW: 0,
    root: 'position: relative; flex: 1 1 auto; min-width: 0; height: 48px; overflow: hidden',
  },
  compare: {
    count: 110, gain: 50, scale: 0.7, flat: '', rowTop: 0, rowH: 40, rowW: 329,
    root: 'position: relative; width: 329px; height: 40px',
  },
};
const ZONE_CLASS = { timbre: 'in-timbre', selection: 'in-sel', rest: 'in-sel', outside: 'out' };

const clamp01 = (v) => Math.max(0, Math.min(1, v));
const kindOf = (kind) => KINDS[kind] || KINDS.forge;

// ---------------------------------------------------------------------------------------------
// Pure functions (checked by tests/wave_logic.mjs).
// ---------------------------------------------------------------------------------------------

// A bar's height for a peak v in 0..1: max(3, round((3 + v * gain) * scale)).
export function barHeight(kind, v) {
  const k = kindOf(kind);
  return Math.max(3, Math.round((3 + v * k.gain) * k.scale));
}

// The class of a zone the behaviours decided (viewmodels.Bars); '' for none or an unknown zone.
export function zoneClass(zone) {
  return ZONE_CLASS[zone] || '';
}

function barClass(cls) {
  return cls ? 'emiya-bar ' + cls : 'emiya-bar';
}

// [value, zone] pairs or plain values -> [{ h, cls, style }]; a flat 3 px row of the kind's count when empty.
export function barModel(kind, bars) {
  const k = kindOf(kind);
  const rows = bars && bars.length ? bars.map((b) => (Array.isArray(b) ? b : [b, ''])) : null;
  const model = rows
    ? rows.map(([v, zone]) => ({ h: barHeight(kind, Number(v) || 0), cls: barClass(zoneClass(zone)) }))
    : Array.from({ length: k.count }, () => ({ h: 3, cls: barClass(k.flat) }));
  for (const b of model) b.style = 'display: block; flex: none; width: 2px; height: ' + b.h + 'px; border-radius: 1px';
  return model;
}

// The trace overlay's width for a selection end in percent of the clip (M L2630).
export function selWidth(selPct) {
  return Math.max(40, Math.round((CLIP_W * selPct) / 100));
}

// circuit(w, h) (M L1434-1456): a gate-shaped trace along the mid line, statement for statement.
export function circuit(w, h) {
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

// pathD (M L1458-1460)
export function pathD(pts) {
  return 'M' + pts.map((p) => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' L');
}

// along (M L1462-1473): the point at fraction prog of the polyline's length.
export function along(pts, prog) {
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

// The dash offset that reveals fraction prog of a path drawn with pathLength 1000 (M L3047).
export function traceOffset(prog) {
  return Math.round(1000 * (1 - clamp01(prog)));
}

// ---------------------------------------------------------------------------------------------
// The Vue component (props from bars.py).
// ---------------------------------------------------------------------------------------------
let instances = 0;

const TEMPLATE = `
<div :class="['emiya-bars', 'emiya-bars--' + kindName]" :style="spec.root" :role="aria ? 'img' : null"
     :aria-label="aria || null" :aria-hidden="aria ? null : 'true'">
  <div v-if="showMarks" aria-hidden="true" style="position: absolute; left: 0; top: 0; width: 100%; height: 18px">
    <span v-for="(m, i) in markList" :key="'label-' + i" class="emiya-bars-mark-label" :style="m.labelStyle">{{ m.label }}</span>
  </div>
  <div class="emiya-bars-row" :style="rowStyle">
    <span v-for="(b, i) in model" :key="i" :class="b.cls" :style="b.style"></span>
  </div>
  <div v-if="hasProgress" aria-hidden="true" class="emiya-bars-played" :style="playedStyle">
    <div :style="playedRowStyle">
      <span v-for="(b, i) in model" :key="i" class="emiya-bar is-played" :style="b.style"></span>
    </div>
  </div>
  <template v-if="showMarks">
    <span v-for="(m, i) in markList" :key="'line-' + i" aria-hidden="true" class="emiya-bars-mark-line" :style="m.lineStyle"></span>
    <span aria-hidden="true" class="emiya-bars-sel" style="position: absolute; left: 0; top: 18px; width: 2px; height: 68px; border-radius: 1px"></span>
    <span aria-hidden="true" class="emiya-bars-sel" :style="selEndStyle"></span>
  </template>
  <svg v-if="kindName === 'forge' && tracing" :width="traceW" height="${TRACE_H}" :viewBox="'0 0 ' + traceW + ' ${TRACE_H}'"
       aria-hidden="true" class="emiya-trace" style="position: absolute; left: 0; top: 20px; overflow: visible">
    <defs>
      <radialGradient :id="gradId">
        <stop offset="0%" class="emiya-trace-glow-in"></stop>
        <stop offset="100%" class="emiya-trace-glow-out"></stop>
      </radialGradient>
    </defs>
    <path :d="traceD" pathLength="1000" class="emiya-trace-shadow" fill="none" stroke-width="3.2" stroke-linecap="round"
          stroke-linejoin="round" stroke-dasharray="1000" :style="dashStyle"></path>
    <path :d="traceD" pathLength="1000" class="emiya-trace-line" fill="none" stroke-width="1.2" stroke-linecap="round"
          stroke-linejoin="round" stroke-dasharray="1000" :style="dashStyle"></path>
    <g :style="headStyle">
      <circle cx="0" cy="0" r="9" :fill="'url(#' + gradId + ')'"></circle>
      <circle cx="0" cy="0" r="1.6" class="emiya-trace-dot"></circle>
    </g>
  </svg>
</div>`;

export default {
  template: TEMPLATE,
  props: {
    kind: String,                 // "forge" | "voices" | "compare"
    bars: Array,                  // [[value, zone], ...] or [value, ...]
    marks: Array,                 // forge: [{ label, pct }]
    selPct: Number,               // forge: the selection end, percent of the clip; null = no edges
    progress: Number,             // the played part 0..1 as a clipped second row; null = none
    tracing: Boolean,             // forge: the Trace On overlay
    traceProgress: Number,        // its reveal 0..1
    aria: String,                 // role img with this label; '' = aria-hidden
    reduce: [Boolean, String],    // true (Reduce Motion "Always") or "system" (follow Windows)
  },
  data() {
    instances += 1;
    return { gradId: 'emiya-bars-head-' + instances, reduced: false, shownTrace: 0, easeTrace: false, easeProgress: false, lastTraceAt: -Infinity };
  },
  computed: {
    kindName() {
      return KINDS[this.kind] ? this.kind : 'forge';
    },
    spec() {
      return kindOf(this.kind);
    },
    model() {
      return barModel(this.kindName, this.bars);
    },
    rowStyle() {
      const k = this.spec;
      const box = 'display: flex; align-items: center; gap: 1px; height: ' + k.rowH + 'px';
      if (!k.rowW) return box;
      return 'position: absolute; left: 0; top: ' + k.rowTop + 'px; width: ' + k.rowW + 'px; ' + box;
    },
    hasProgress() {
      return typeof this.progress === 'number';
    },
    playedStyle() {
      const k = this.spec;
      return {
        position: 'absolute', left: '0', top: k.rowTop + 'px', height: k.rowH + 'px', overflow: 'hidden',
        width: clamp01(this.progress || 0) * 100 + '%',
        transition: this.easeProgress ? 'width ' + EASE_S + 's linear' : 'none',
      };
    },
    playedRowStyle() {
      const k = this.spec;
      return 'display: flex; align-items: center; gap: 1px; height: ' + k.rowH + 'px; width: ' + (k.rowW ? k.rowW + 'px' : '100%');
    },
    showMarks() {
      return this.kindName === 'forge' && !!this.bars && this.bars.length > 0;
    },
    markList() {
      return (this.marks || []).map((m) => ({
        label: m.label,
        labelStyle: 'position: absolute; left: ' + m.pct + '%; top: 0; transform: translateX(-50%); font-size: 12px; line-height: 16px; white-space: nowrap',
        lineStyle: 'position: absolute; left: ' + m.pct + '%; top: 18px; width: 1px; height: 68px',
      }));
    },
    selEndStyle() {
      const pct = typeof this.selPct === 'number' ? this.selPct : 100;
      return 'position: absolute; left: ' + pct + '%; top: 18px; width: 2px; height: 68px; margin-left: -2px; border-radius: 1px';
    },
    traceW() {
      return selWidth(typeof this.selPct === 'number' ? this.selPct : 100);
    },
    tracePts() {
      return circuit(this.traceW, TRACE_H);
    },
    traceD() {
      return pathD(this.tracePts);
    },
    dashStyle() {
      return {
        strokeDashoffset: traceOffset(this.shownTrace),
        transition: this.easeTrace ? 'stroke-dashoffset ' + EASE_S + 's linear' : 'none',
      };
    },
    headStyle() {
      const xy = along(this.tracePts, this.shownTrace);
      return {
        transform: 'translate(' + xy[0].toFixed(1) + 'px, ' + xy[1].toFixed(1) + 'px)',
        transition: this.easeTrace ? 'transform ' + EASE_S + 's linear' : 'none',
      };
    },
  },
  watch: {
    traceProgress() {
      this.followTrace();
    },
    tracing(on) {
      if (on) this.applyTrace(0, false);
      this.followTrace();
    },
    progress(next, prev) {
      this.easeProgress = !this.reduced && typeof prev === 'number' && typeof next === 'number' && next > prev;
    },
    reduce() {
      this.reduced = this.isReduced();
    },
  },
  mounted() {
    this.motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    this.onMotion = () => { this.reduced = this.isReduced(); };
    this.motionQuery.addEventListener('change', this.onMotion);
    this.reduced = this.isReduced();
    this.applyTrace(clamp01(this.traceProgress || 0), false);
  },
  unmounted() {
    this.motionQuery.removeEventListener('change', this.onMotion);
    clearTimeout(this.traceTimer);
  },
  methods: {
    isReduced() {
      return this.reduce === true || (this.reduce === 'system' && !!this.motionQuery && this.motionQuery.matches);
    },
    // Follow the bake progress: eased while it grows; under Reduce Motion at most 4 updates a second.
    followTrace() {
      const target = clamp01(this.traceProgress || 0);
      if (!this.reduced) {
        this.applyTrace(target, target > this.shownTrace);
        return;
      }
      const wait = this.lastTraceAt + REDUCED_UPDATE_MS - performance.now();
      if (wait <= 0) {
        this.applyTrace(target, false);
      } else if (!this.traceTimer) {
        this.traceTimer = setTimeout(() => {
          this.traceTimer = 0;
          this.applyTrace(clamp01(this.traceProgress || 0), false);
        }, wait);
      }
    },
    applyTrace(value, ease) {
      this.easeTrace = ease;
      this.shownTrace = value;
      this.lastTraceAt = performance.now();
    },
  },
};
