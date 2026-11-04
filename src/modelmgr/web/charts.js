"use strict";
// Hand-written SVG charts: no dependencies, no network. Colours come from CSS classes (s1..s8,
// s-other) so light and dark mode swap in style.css only. All labels go in via textContent.

(() => {
  const NS = "http://www.w3.org/2000/svg";

  function svg(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  }

  function html(tag, cls, parent, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  // ---------- tooltip (one per page) ----------

  const tip = html("div", "tip");
  tip.hidden = true;
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(tip));

  function showTip(evt, title, rows) {
    tip.replaceChildren();
    html("div", "tip-title", tip, title);
    for (const r of rows) {
      const row = html("div", "tip-row", tip);
      if (r.cls) html("span", `key ${r.cls}`, row);
      html("strong", null, row, r.value);
      html("span", "tip-label", row, r.label);
    }
    tip.hidden = false;
    moveTip(evt);
  }

  function moveTip(evt) {
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
    tip.style.left = `${Math.max(8, x)}px`;
    tip.style.top = `${Math.max(8, y)}px`;
  }

  function hideTip() { tip.hidden = true; }

  // ---------- scales ----------

  function niceMax(max, ticks = 4) {
    if (!(max > 0)) return { max: 1, step: 0.25 };
    const raw = max / ticks;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    return { max: step * Math.ceil(max / step), step };
  }

  function yAxis(g, { left, right, top, h, max, step, fmt }) {
    for (let v = 0; v <= max + step / 2; v += step) {
      const y = top + h - (v / max) * h;
      svg("line", { x1: left, x2: right, y1: y, y2: y, class: v === 0 ? "baseline" : "grid" }, g);
      svg("text", { x: left - 8, y: y + 4, class: "tick", "text-anchor": "end" }, g).textContent = fmt(v);
    }
  }

  function xLabels(g, labels, xOf, y, plotW) {
    const every = Math.max(1, Math.ceil(labels.length / Math.max(1, Math.floor(plotW / 56))));
    labels.forEach((lab, i) => {
      if (i % every && i !== labels.length - 1) return;
      if (i !== labels.length - 1 && labels.length - 1 - i < every) return; // keep the last one clear
      svg("text", { x: xOf(i), y, class: "tick", "text-anchor": "middle" }, g).textContent = lab;
    });
  }

  function frame(container, height) {
    container.replaceChildren();
    const width = Math.max(280, container.clientWidth);
    const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, width, height, class: "chart", role: "img" });
    container.appendChild(root);
    return { root, width };
  }

  // path of a bar with a rounded data end (top) and a square baseline end
  function topRounded(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  // ---------- columns (single or stacked) ----------
  // opts: { labels, series:[{key,name,cls}], rows:[{key:value|null}], fmt, tickFmt, title(i),
  //         capLabel(i), height, aria }
  function columns(container, opts) {
    const H = opts.height || 220;
    const { root, width } = frame(container, H);
    root.setAttribute("aria-label", opts.aria || "column chart");
    const m = { l: 52, r: 8, t: opts.capLabel ? 22 : 10, b: 26 };
    const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
    const totals = opts.rows.map((r) => opts.series.reduce((s, se) => s + (r[se.key] || 0), 0));
    const { max, step } = niceMax(Math.max(0, ...totals));
    const g = svg("g", {}, root);
    yAxis(g, { left: m.l, right: width - m.r, top: m.t, h: plotH, max, step, fmt: opts.tickFmt || opts.fmt });
    const n = opts.rows.length, band = plotW / Math.max(1, n);
    const bw = Math.max(2, Math.min(24, band * 0.72));
    const xOf = (i) => m.l + band * i + band / 2;
    const yOf = (v) => m.t + plotH - (v / max) * plotH;
    opts.rows.forEach((row, i) => {
      const x = xOf(i) - bw / 2;
      const segs = opts.series.filter((se) => row[se.key] > 0);
      let acc = 0;
      segs.forEach((se, j) => {
        const v = row[se.key];
        const y0 = yOf(acc), y1 = yOf(acc + v);
        acc += v;
        const gap = j > 0 ? 2 : 0; // surface gap between stacked segments
        const h = Math.max(0, y0 - y1 - gap);
        if (h <= 0) return;
        if (j === segs.length - 1) svg("path", { d: topRounded(x, y1, bw, h, 4), class: `fill ${se.cls}` }, g);
        else svg("rect", { x, y: y1, width: bw, height: h, class: `fill ${se.cls}` }, g);
      });
      if (opts.capLabel && totals[i] > 0) {
        svg("text", { x: xOf(i), y: yOf(totals[i]) - 6, class: "cap", "text-anchor": "middle" }, g)
          .textContent = opts.capLabel(i);
      }
    });
    xLabels(g, opts.labels, xOf, H - 8, plotW);
    // hit targets: the whole band, taller than the mark
    const hits = svg("g", {}, root);
    opts.rows.forEach((row, i) => {
      const hit = svg("rect", { x: m.l + band * i, y: m.t, width: band, height: plotH, class: "hit" }, hits);
      hit.addEventListener("mousemove", (e) => {
        const rows = opts.series.map((se) => ({ cls: opts.series.length > 1 ? se.cls : null,
          value: row[se.key] == null ? "not measured" : opts.fmt(row[se.key]), label: se.name }));
        if (opts.series.length > 1) rows.push({ value: opts.fmt(totals[i]), label: "total" });
        if (opts.extra) rows.push(...opts.extra(i));
        showTip(e, opts.title ? opts.title(i) : opts.labels[i], rows);
      });
      hit.addEventListener("mouseleave", hideTip);
    });
  }

  // ---------- horizontal bars (one series) ----------
  // opts: { rows:[{label, value, note}], fmt, cls }
  function hbars(container, opts) {
    container.replaceChildren();
    const max = Math.max(0, ...opts.rows.map((r) => r.value || 0)) || 1;
    const list = html("div", "hbars", container);
    for (const r of opts.rows) {
      const row = html("div", "hbar", list);
      html("span", "hbar-label", row, r.label);
      const track = html("span", "hbar-track", row);
      const bar = html("span", `hbar-fill ${opts.cls || "s1"}`, track);
      bar.style.width = `${((r.value || 0) / max) * 100}%`;
      html("span", "hbar-value", row, opts.fmt(r.value));
      row.addEventListener("mousemove", (e) => showTip(e, r.label,
        [{ value: opts.fmt(r.value), label: "cost" }, ...(r.note ? [{ value: r.note, label: "" }] : [])]));
      row.addEventListener("mouseleave", hideTip);
    }
  }

  // ---------- lines (shared y axis, same unit) ----------
  // opts: { labels, series:[{name, cls, values:[]}], fmt, tickFmt, title(i), height, aria,
  //         bridge (draw across nulls: for days with no data rather than unmeasured ones) }
  function lines(container, opts) {
    const H = opts.height || 220;
    const { root, width } = frame(container, H);
    root.setAttribute("aria-label", opts.aria || "line chart");
    const m = { l: 52, r: 16, t: 12, b: 26 };
    const plotW = width - m.l - m.r, plotH = H - m.t - m.b;
    const all = opts.series.flatMap((s) => s.values.filter((v) => v != null));
    const { max, step } = niceMax(Math.max(0, ...all));
    const g = svg("g", {}, root);
    yAxis(g, { left: m.l, right: width - m.r, top: m.t, h: plotH, max, step, fmt: opts.tickFmt || opts.fmt });
    const n = opts.labels.length;
    const xOf = (i) => (n === 1 ? m.l + plotW / 2 : m.l + (plotW * i) / (n - 1));
    const yOf = (v) => m.t + plotH - (v / max) * plotH;
    for (const s of opts.series) {
      let d = "", pen = false;
      s.values.forEach((v, i) => {
        if (v == null) { if (!opts.bridge) pen = false; return; } // a gap means "not measured"
        d += `${pen ? "L" : "M"}${xOf(i)},${yOf(v)}`;
        pen = true;
      });
      if (d) svg("path", { d, class: `line ${s.cls}` }, g);
      const last = s.values.map((v, i) => [v, i]).filter(([v]) => v != null).pop();
      if (last) svg("circle", { cx: xOf(last[1]), cy: yOf(last[0]), r: 4, class: `dot ${s.cls}` }, g);
    }
    xLabels(g, opts.labels, xOf, H - 8, plotW);
    const cross = svg("line", { y1: m.t, y2: m.t + plotH, class: "crosshair", visibility: "hidden" }, root);
    const hit = svg("rect", { x: m.l, y: m.t, width: plotW, height: plotH, class: "hit" }, root);
    hit.addEventListener("mousemove", (e) => {
      const box = root.getBoundingClientRect();
      const px = ((e.clientX - box.left) / box.width) * width;
      const i = n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / plotW) * (n - 1))));
      cross.setAttribute("x1", xOf(i));
      cross.setAttribute("x2", xOf(i));
      cross.setAttribute("visibility", "visible");
      showTip(e, opts.title ? opts.title(i) : opts.labels[i], opts.series.map((s) => ({
        cls: s.cls, value: s.values[i] == null ? "not measured" : opts.fmt(s.values[i]), label: s.name })));
    });
    hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  }

  function legend(container, series) {
    container.replaceChildren();
    for (const s of series) {
      const item = html("span", "legend-item", container);
      html("span", `key ${s.cls}`, item);
      html("span", null, item, s.name);
    }
  }

  // columns: [{label, value(row) -> string, num}]
  function table(container, cols, rows) {
    container.replaceChildren();
    const t = html("table", "data", container);
    const head = html("tr", null, html("thead", null, t));
    for (const c of cols) html("th", c.num ? "num" : null, head, c.label);
    const body = html("tbody", null, t);
    for (const r of rows) {
      const tr = html("tr", null, body);
      for (const c of cols) html("td", c.num ? "num" : null, tr, c.value(r));
    }
  }

  window.Charts = { columns, hbars, lines, legend, table, html, hideTip };
})();
