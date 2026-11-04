"use strict";

const { html } = Charts;
const REFRESH_MS = 60_000; // the server ingests every 30 s
const PAGES = ["overview", "context"];

// colour follows the model family, never its rank in the current period
const FAMILY_CLASS = { opus: "s1", sonnet: "s2", haiku: "s3", fable: "s4" };

const state = { page: "overview", days: "30", data: {} };

// ---------- formatting ----------

const usd = (v) => (v == null ? "—" : v >= 100 ? `$${Math.round(v).toLocaleString("en-US")}`
  : v >= 1 ? `$${v.toFixed(2)}` : v > 0 ? `$${v.toFixed(3)}` : "$0");
const usdTick = (v) => (v >= 1000 ? `$${+(v / 1000).toFixed(1)}K` : `$${+v.toFixed(2)}`);
const int = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`);
const tok = (v) => (v == null ? "—" : v >= 1e6 ? `${+(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}K` : `${v}`);
const dayLabel = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const dayTitle = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });

function modelInfo(id) {
  const m = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(id || "");
  if (!m) return { name: id || "unknown", cls: "s-other" };
  return { name: `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}`, cls: FAMILY_CLASS[m[1]] || "s-other" };
}

// every UTC day of the period, so days without requests show as zero rather than vanish
function dayRange(rows) {
  const today = new Date().toISOString().slice(0, 10);
  let start = state.days === "all" ? rows[0]?.date || today
    : new Date(Date.now() - (+state.days - 1) * 864e5).toISOString().slice(0, 10);
  if (rows[0]?.date && rows[0].date < start) start = rows[0].date;
  const out = [];
  for (let d = new Date(`${start}T00:00:00Z`); d.toISOString().slice(0, 10) <= today; d.setUTCDate(d.getUTCDate() + 1)) {
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

// ---------- data ----------

async function api(path) {
  const r = await fetch(`/api/${path}`, { credentials: "same-origin" });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.error || `${path}: HTTP ${r.status}`);
  }
  return r.json();
}

const LOADERS = {
  overview: (q) => Promise.all([api(`overview?${q}`), api(`daily?${q}`), api(`model-mix?${q}`)])
    .then(([overview, daily, mix]) => ({ overview, daily, mix })),
  context: (q) => Promise.all([api(`overview?${q}`), api(`context?${q}`), api(`context-daily?${q}`)])
    .then(([overview, hist, daily]) => ({ overview, hist, daily })),
};

async function load() {
  const err = document.getElementById("error");
  try {
    const [status, data] = await Promise.all([api("status"), LOADERS[state.page](`days=${state.days}`)]);
    state.data[state.page] = data;
    err.hidden = true;
    renderStatus(status);
    render();
  } catch (e) {
    // keep the last render on screen; say what failed
    err.textContent = `Could not load data: ${e.message}`;
    err.hidden = false;
  }
}

// ---------- rendering ----------

function renderStatus(s) {
  const parts = [`mode ${s.mode}`];
  if (s.disabled) parts.push("kill switch on");
  if (s.last_ingest_error) parts.push(`ingest failed: ${s.last_ingest_error}`);
  else if (s.last_ingest?.ts) parts.push(`updated ${new Date(s.last_ingest.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`);
  document.getElementById("status").textContent = parts.join(" · ");
}

function tiles(container, items) {
  container.replaceChildren();
  for (const t of items) {
    const tile = html("div", `tile${t.hero ? " hero" : ""}`, container);
    html("div", "tile-label", tile, t.label);
    html("div", "tile-value", tile, t.value);
    if (t.sub) html("div", "tile-sub", tile, t.sub);
  }
}

function renderOverview({ overview: o, daily, mix }) {
  const sv = o.savings;
  tiles(document.getElementById("ov-tiles"), [
    { label: "API-equivalent cost", value: usd(o.est_cost_usd), hero: true,
      sub: o.unpriced_requests ? `${int(o.unpriced_requests)} requests not priced` : "estimate from token counts" },
    { label: "Requests", value: int(o.requests), sub: `${int(o.sessions)} sessions` },
    sv.routed_requests
      ? { label: "Estimated savings", value: usd(sv.est_savings_usd),
          sub: `${int(sv.routed_requests)} routed requests vs all-${modelInfo(sv.baseline_model).name}` }
      : { label: "Estimated savings", value: "—", sub: "routing not active yet" },
    { label: "Cache-read share of cost", value: pct(o.cache_read_share) },
    { label: "Median context", value: tok(o.median_context), sub: "tokens read per request" },
  ]);

  const models = [...new Set(daily.flatMap((d) => Object.keys(d.by_model)))]
    .sort((a, b) => modelInfo(a).cls.localeCompare(modelInfo(b).cls));
  const series = models.map((id) => ({ key: id, ...modelInfo(id) }));
  const byDate = Object.fromEntries(daily.map((d) => [d.date, d]));
  const dates = dayRange(daily);
  const rows = dates.map((d) => ({ ...(byDate[d]?.by_model || {}) }));
  Charts.legend(document.getElementById("ov-daily-legend"), series);
  Charts.columns(document.getElementById("ov-daily"), {
    labels: dates.map(dayLabel), series, rows, fmt: usd, tickFmt: usdTick, aria: "Cost per day by model",
    title: (i) => dayTitle(dates[i]),
    extra: (i) => [{ value: int(byDate[dates[i]]?.requests || 0), label: "requests" }],
  });
  Charts.table(document.getElementById("ov-daily-table"), [
    { label: "Day", value: (d) => d },
    { label: "Requests", num: true, value: (d) => int(byDate[d]?.requests || 0) },
    ...series.map((s) => ({ label: s.name, num: true, value: (d) => usd(byDate[d]?.by_model[s.key] ?? 0) })),
    { label: "Total", num: true, value: (d) => usd(byDate[d]?.est_cost_usd ?? 0) },
  ], dates.slice().reverse());

  const k = o.cost_by_kind, t = o.tokens;
  Charts.hbars(document.getElementById("ov-kinds"), {
    fmt: usd, rows: [
      { label: "Cache read", value: k.cache_read, note: `${tok(t.cache_read)} tokens` },
      { label: "Cache write", value: k.cache_write, note: `${tok(t.cache_write)} tokens` },
      { label: "Output", value: k.output, note: `${tok(t.output)} tokens` },
      { label: "Input", value: k.input, note: `${tok(t.input)} tokens` },
    ],
  });

  const mixEl = document.getElementById("ov-mix");
  Charts.table(mixEl, [
    { label: "Model", value: (m) => modelInfo(m.model).name },
    { label: "Requests", num: true, value: (m) => int(m.requests) },
    { label: "Share", num: true, value: (m) => pct(m.request_share) },
    { label: "Cost", num: true, value: (m) => usd(m.est_cost_usd) },
    { label: "Share", num: true, value: (m) => pct(m.cost_share) },
  ], mix);
  // colour key in the first cell: identity is never colour alone, the name sits beside it
  mixEl.querySelectorAll("tbody tr").forEach((tr, i) => {
    const key = html("span", `key ${modelInfo(mix[i].model).cls}`);
    tr.firstChild.prepend(key);
  });
}

function renderContext({ overview: o, hist, daily }) {
  const measured = hist.filter((b) => b.lo != null);
  const notMeasured = hist.find((b) => b.lo == null);
  const big = measured.filter((b) => b.lo >= 200_000);
  const bigShare = big.reduce((s, b) => s + (b.cost_share || 0), 0);
  const bigReq = big.reduce((s, b) => s + b.requests, 0);
  const allReq = measured.reduce((s, b) => s + b.requests, 0);
  tiles(document.getElementById("cx-tiles"), [
    { label: "Median context", value: tok(o.median_context), hero: true, sub: "tokens read per request" },
    { label: "Cost above 200K context", value: allReq ? pct(bigShare) : "—",
      sub: allReq ? `${pct(bigReq / allReq)} of requests` : "no requests in period" },
    { label: "Cost above 400K context", value: allReq ? pct(measured.find((b) => b.lo === 400_000)?.cost_share) : "—" },
  ]);

  Charts.columns(document.getElementById("cx-hist"), {
    labels: measured.map((b) => b.label), series: [{ key: "c", name: "cost", cls: "s1" }],
    rows: measured.map((b) => ({ c: b.est_cost_usd })), fmt: usd, tickFmt: usdTick, height: 240,
    aria: "Cost by context size", capLabel: (i) => pct(measured[i].cost_share),
    title: (i) => `${measured[i].label} tokens`,
    extra: (i) => [{ value: int(measured[i].requests), label: "requests" },
                   { value: pct(measured[i].cost_share), label: "of cost" }],
  });
  document.getElementById("cx-hist-note").textContent = notMeasured
    ? `${int(notMeasured.requests)} requests without token counts are not measured and left out.` : "";

  const byDate = Object.fromEntries(daily.map((d) => [d.date, d]));
  const dates = dayRange(daily);
  const series = [
    { name: "Median", cls: "s1", values: dates.map((d) => byDate[d]?.median_context ?? null) },
    { name: "90th percentile", cls: "s2", values: dates.map((d) => byDate[d]?.p90_context ?? null) },
  ];
  Charts.legend(document.getElementById("cx-daily-legend"), series);
  Charts.lines(document.getElementById("cx-daily"), {
    labels: dates.map(dayLabel), series, fmt: tok, aria: "Context size per day", title: (i) => dayTitle(dates[i]),
    bridge: true,
  });
  Charts.table(document.getElementById("cx-daily-table"), [
    { label: "Day", value: (d) => d.date },
    { label: "Requests", num: true, value: (d) => int(d.requests) },
    { label: "Median", num: true, value: (d) => tok(d.median_context) },
    { label: "90th pct", num: true, value: (d) => tok(d.p90_context) },
    { label: "Cost > 200K", num: true, value: (d) => pct(d.over_200k_cost_share) },
  ], daily.slice().reverse());
}

const RENDER = { overview: renderOverview, context: renderContext };

function render() {
  Charts.hideTip();
  for (const p of PAGES) document.getElementById(`page-${p}`).hidden = p !== state.page;
  document.querySelectorAll("#tabs a").forEach((a) => a.setAttribute("aria-current", a.dataset.page === state.page ? "page" : "false"));
  document.querySelectorAll("#period button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.days === state.days));
  const data = state.data[state.page];
  if (data) RENDER[state.page](data);
}

// ---------- navigation: #page?days=N ----------

function readHash() {
  const [page, query] = location.hash.slice(1).split("?");
  state.page = PAGES.includes(page) ? page : "overview";
  const days = new URLSearchParams(query || "").get("days");
  if (["1", "7", "30", "all"].includes(days)) state.days = days;
}

function writeHash() {
  history.replaceState(null, "", `#${state.page}?days=${state.days}`);
}

window.addEventListener("hashchange", () => { readHash(); render(); load(); });
document.getElementById("period").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.days = b.dataset.days;
  state.data = {};
  writeHash();
  render();
  load();
});
document.getElementById("tabs").addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (!a) return;
  e.preventDefault();
  state.page = a.dataset.page;
  writeHash();
  render();
  load();
});

let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });

readHash();
writeHash();
render();
load();
setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
