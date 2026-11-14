// VERBATIM copy of the Emiya Voice mockup canvas engine (fxInit ... fxDraw), for the parity test only.
// Source: Main.dc.html, lines 2068-2345, sha256 a26e1d8cf5899ec55de68dfc23db36e98f8b8cfc112924826e2dba63a77eb3e2.
// Not shipped: this file holds the mockup's colour literals and fixed coordinates on purpose.
export class MockupEngine {
  fxInit(W, H) {
    const fx = { W: W, H: H, t: 0, embers: [], traces: [], sparks: [], nextTrace: 0.8, nextSpark: 4, heat: 1, heatTarget: 1, focus: null };
    for (let i = 0; i < 40; i++) {
      const e = this.fxEmber(fx);
      e.life = Math.random() * e.max * 0.8;
      e.y -= e.vy * e.life;
      fx.embers.push(e);
    }
    return fx;
  }

  fxEmber(fx) {
    const r = Math.random;
    const hot = r() < 0.12;
    const palette = [[158, 32, 40], [196, 62, 42], [222, 112, 48]];
    return {
      x: r() * fx.W, y: fx.H * (0.5 + r() * 0.55), vy: 6 + r() * 12,
      sway: 3 + r() * 9, freq: 0.12 + r() * 0.35, phase: r() * Math.PI * 2,
      size: hot ? 1.1 + r() * 0.6 : 0.5 + r() * 1.0,
      life: 0, max: 8 + r() * 8, flick: 1.2 + r() * 2.4,
      rgb: hot ? [252, 219, 127] : palette[Math.floor(r() * palette.length)],
      base: hot ? 0.8 : 0.4 + r() * 0.25
    };
  }

  fxMeasure(p) {
    p.cum = [0];
    let len = 0;
    for (let i = 1; i < p.pts.length; i++) {
      len += Math.hypot(p.pts[i][0] - p.pts[i - 1][0], p.pts[i][1] - p.pts[i - 1][1]);
      p.cum.push(len);
    }
    p.len = Math.max(len, 0.001);
    return p;
  }

  fxSmooth(pts, passes) {
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

  fxTrace(fx) {
    let tr = this.fxTraceOnce(fx);
    for (let i = 0; i < 6 && this.fxMinX(tr) < 190; i++) tr = this.fxTraceOnce(fx);
    return tr;
  }

  fxMinX(tr) {
    let m = Infinity;
    tr.paths.forEach((p) => p.pts.forEach((q) => { if (q[0] < m) m = q[0]; }));
    return m;
  }

  fxTraceOnce(fx) {
    const r = Math.random;
    const blade = r() < 0.15;
    let x0 = 240 + r() * (fx.W - 300);
    let y0 = 80 + r() * (fx.H - 160);
    if (fx.focus && fx.heat > 1.1) {
      x0 = Math.max(240, Math.min(fx.W - 60, fx.focus.x + (r() - 0.5) * 320));
      y0 = Math.max(80, Math.min(fx.H - 80, fx.focus.y + (r() - 0.5) * 320));
    }
    const paths = [];
    let speed;
    if (blade) {
      const a = -Math.PI / 2 + (r() - 0.5) * 0.9;
      const len = 70 + r() * 70;
      const main = this.fxMeasure({ pts: [[x0, y0], [x0 + Math.cos(a) * len, y0 + Math.sin(a) * len]], delay: 0 });
      speed = main.len / 1.1;
      const gx = x0 + Math.cos(a) * len * 0.2;
      const gy = y0 + Math.sin(a) * len * 0.2;
      const px = Math.cos(a + Math.PI / 2) * 9;
      const py = Math.sin(a + Math.PI / 2) * 9;
      paths.push(main);
      paths.push(this.fxMeasure({ pts: [[gx - px, gy - py], [gx + px, gy + py]], delay: (len * 0.2) / speed + 0.08 }));
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
      const main = this.fxMeasure({ pts: this.fxSmooth(pts, 2), delay: 0 });
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
          paths.push(this.fxMeasure({ pts: this.fxSmooth(bpts, 1), delay: main.cum[near] / speed + 0.1 + k * 0.15 }));
        }
      });
    }
    let end = 0;
    paths.forEach((p) => { end = Math.max(end, p.delay + p.len / speed); });
    return { paths: paths, blade: blade, speed: speed, end: end, age: 0, hold: 0.6 + r() * 0.8, fade: 1.6 + r() * 1.2, sparked: false, force: false };
  }

  fxForgeBlade(x0, y0) {
    if (!this.fx) return;
    const a = -Math.PI / 2 - 0.18;
    const len = 130;
    const main = this.fxMeasure({ pts: [[x0, y0], [x0 + Math.cos(a) * len, y0 + Math.sin(a) * len]], delay: 0 });
    const speed = main.len / 1.2;
    const gx = x0 + Math.cos(a) * len * 0.2;
    const gy = y0 + Math.sin(a) * len * 0.2;
    const px = Math.cos(a + Math.PI / 2) * 9;
    const py = Math.sin(a + Math.PI / 2) * 9;
    const guard = this.fxMeasure({ pts: [[gx - px, gy - py], [gx + px, gy + py]], delay: (len * 0.2) / speed + 0.08 });
    let end = 0;
    [main, guard].forEach((p) => { end = Math.max(end, p.delay + p.len / speed); });
    this.fx.traces.push({ paths: [main, guard], blade: true, speed: speed, end: end, age: 0, hold: 1.4, fade: 2.2, sparked: false, force: true });
  }

  fxStep(dt) {
    const fx = this.fx;
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
      if (e.life >= e.max || e.y < -12) fx.embers[i] = this.fxEmber(fx);
    }
    fx.nextTrace -= dt;
    if (fx.nextTrace <= 0) {
      if (fx.traces.length < (hot > 0.5 ? 4 : 3)) fx.traces.push(this.fxTrace(fx));
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
      fx.sparks.push({ x: 200 + r() * (fx.W - 240), y: fx.H * (0.35 + r() * 0.6), age: 0, life: 0.9 + r() * 0.6 });
      fx.nextSpark = hot > 0.5 ? 2 + r() * 2 : 5 + r() * 5;
    }
    fx.sparks.forEach((sp) => { sp.age += dt; sp.y -= 8 * dt; });
    fx.sparks = fx.sparks.filter((sp) => sp.age < sp.life);
  }

  fxDot(ctx, x, y, radius, r, g, b, a) {
    if (a <= 0.003) return;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 6.2832);
    ctx.fillStyle = 'rgba(' + r + ', ' + g + ', ' + b + ', ' + a.toFixed(3) + ')';
    ctx.fill();
  }

  fxGlow(ctx, x, y, radius, r, g, b, a) {
    if (a <= 0.003 || radius <= 0.5) return;
    const grad = ctx.createRadialGradient(x, y, 0, x, y, radius);
    grad.addColorStop(0, 'rgba(' + r + ', ' + g + ', ' + b + ', ' + a.toFixed(3) + ')');
    grad.addColorStop(1, 'rgba(' + r + ', ' + g + ', ' + b + ', 0)');
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, 6.2832);
    ctx.fillStyle = grad;
    ctx.fill();
  }

  fxPathTo(ctx, p, prog) {
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

  fxDraw(ctx) {
    const fx = this.fx;
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
        const head = this.fxPathTo(ctx, p, prog);
        ctx.strokeStyle = 'rgba(150, 30, 34, ' + (0.14 * alpha).toFixed(3) + ')';
        ctx.lineWidth = 3.2;
        ctx.stroke();
        ctx.strokeStyle = (tr.blade ? 'rgba(200, 74, 50, ' : 'rgba(172, 40, 44, ') + (0.55 * alpha).toFixed(3) + ')';
        ctx.lineWidth = 1;
        ctx.stroke();
        if (prog < 1) {
          this.fxGlow(ctx, head[0], head[1], 9, 244, 150, 60, 0.35);
          this.fxDot(ctx, head[0], head[1], 1.4, 252, 219, 127, 0.95);
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
      this.fxGlow(ctx, x, e.y, e.size * 5, e.rgb[0], e.rgb[1], e.rgb[2], a * 0.3);
      this.fxDot(ctx, x, e.y, e.size, e.rgb[0], e.rgb[1], e.rgb[2], a);
    }
    fx.sparks.forEach((sp) => {
      const k = sp.age / sp.life;
      const a = k < 0.18 ? k / 0.18 : Math.max(0, 1 - (k - 0.18) / 0.82);
      const grow = k < 0.18 ? k / 0.18 : 1;
      this.fxGlow(ctx, sp.x, sp.y, 14 * grow, 244, 150, 60, 0.4 * a);
      this.fxDot(ctx, sp.x, sp.y, 1.2 + 1.2 * grow, 252, 219, 127, a);
    });
  }
}
