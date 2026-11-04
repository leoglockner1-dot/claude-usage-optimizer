"use strict";

const { html } = Charts;
const REFRESH_MS = 60_000; // the server ingests every 30 s
const PAGES = ["overview", "context", "projects", "session", "routing", "quota", "models"];
const NO_PERIOD = new Set(["session", "models"]); // pages that ignore the period filter
const TAB_OF = { session: "projects" };

// colour follows the model family, never its rank in the current period
const FAMILY_CLASS = { opus: "s1", sonnet: "s2", haiku: "s3", fable: "s4" };

// state.project filters the sessions table; state.session is the open session id
const state = { page: "overview", days: "30", project: null, session: null, data: {} };

// ---------- formatting ----------

const usd = (v) => (v == null ? "—" : v >= 100 ? `$${Math.round(v).toLocaleString("en-US")}`
  : v >= 1 ? `$${v.toFixed(2)}` : v > 0 ? `$${v.toFixed(3)}` : "$0");
const usdTick = (v) => (v >= 1000 ? `$${+(v / 1000).toFixed(1)}K` : `$${+v.toFixed(2)}`);
const int = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`);
const tok = (v) => (v == null ? "—" : v >= 1e6 ? `${+(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}K` : `${v}`);
const dayLabel = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const dayTitle = (d) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
// timestamps show in local time: they are moments, not UTC day buckets
const when = (ts) => (ts ? new Date(ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }) : "—");
const clock = (ts) => new Date(ts).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
const price = (v) => (v == null ? "—" : `$${v.toFixed(v >= 0.1 || v === 0 ? 2 : 3)}`);
const share = (v) => (v == null ? "—" : `${Math.round(v)}%`); // quota values are already percent
const yesNo = (v) => (v ? "yes" : "no");
const count = (n, word) => `${int(n)} ${word}${n === 1 ? "" : "s"}`; // "1 request", "3 requests"
function duration(a, b) {
  const min = Math.round((new Date(b) - new Date(a)) / 60_000);
  return min < 60 ? `${min} min` : min < 2880 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${Math.round(min / 1440)} days`;
}

// model ids ("claude-opus-5-5") and router aliases ("opus") both map to a name and a family colour
function modelInfo(id) {
  const m = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(id || "");
  if (m) return { name: `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}`, cls: FAMILY_CLASS[m[1]] || "s-other" };
  if (FAMILY_CLASS[id]) return { name: `${id[0].toUpperCase()}${id.slice(1)}`, cls: FAMILY_CLASS[id] };
  return { name: id || "unknown", cls: "s-other" };
}

function sinceIso() {
  return state.days === "all" ? "" : new Date(Date.now() - +state.days * 864e5).toISOString();
}

function hashFor(page, extra = {}) {
  const q = new URLSearchParams({ days: state.days, ...extra });
  return `#${page}?${q}`;
}

// about six readable ticks across a time span, aligned to local hours or days
function timeTicks(x0, x1) {
  const H = 36e5, steps = [H, 2 * H, 3 * H, 6 * H, 12 * H, 24 * H, 48 * H, 7 * 24 * H];
  const step = steps.find((s) => (x1 - x0) / s <= 6) || steps[steps.length - 1];
  const tz = new Date(x0).getTimezoneOffset() * 60_000;
  const out = [];
  for (let t = Math.ceil((x0 - tz) / step) * step + tz; t <= x1; t += step) {
    out.push({ x: t, label: step < 24 * H ? clock(t) : new Date(t).toLocaleDateString("en-US", { month: "short", day: "numeric" }) });
  }
  return out;
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
  projects: (q) => Promise.all([api(`projects?${q}`), api(`sessions?${q}&limit=1000`)])
    .then(([projects, sessions]) => ({ projects, sessions })),
  session: () => (state.session
    ? Promise.all([api(`sessions/${encodeURIComponent(state.session)}`), api("sessions?days=all&limit=5000")])
      .then(([timeline, sessions]) => ({ id: state.session, timeline, info: sessions.find((s) => s.session_id === state.session) }))
    : Promise.reject(new Error("no session selected"))),
  // routes and escalations are limit-based on the server; the period is applied here
  routing: (q) => Promise.all([api("routes?limit=5000"), api(`categories?${q}`), api("escalations?limit=1000")])
    .then(([routes, cats, esc]) => {
      const since = sinceIso();
      return { routes: routes.filter((r) => r.ts >= since), cats, esc: esc.filter((r) => (r.ts || "") >= since) };
    }),
  quota: (q) => api(`quota?${q}`).then((rows) => ({ rows })),
  models: () => api("models").then((models) => ({ models })),
};

async function load() {
  const err = document.getElementById("error");
  const page = state.page;
  try {
    const [status, data] = await Promise.all([api("status"), LOADERS[page](`days=${state.days}`)]);
    if (page !== state.page) return; // the user moved on while this was loading
    state.data[page] = data;
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
      sub: o.unpriced_requests ? `${count(o.unpriced_requests, "request")} not priced` : "estimate from token counts" },
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

  // colour key in the first cell: identity is never colour alone, the name sits beside it
  Charts.table(document.getElementById("ov-mix"), [
    { label: "Model", value: (m) => modelInfo(m.model).name, key: (m) => modelInfo(m.model).cls },
    { label: "Requests", num: true, value: (m) => int(m.requests) },
    { label: "Share", num: true, value: (m) => pct(m.request_share) },
    { label: "Cost", num: true, value: (m) => usd(m.est_cost_usd) },
    { label: "Share", num: true, value: (m) => pct(m.cost_share) },
  ], mix, "No requests in this period.");
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
    ? `${count(notMeasured.requests, "request")} without token counts ${notMeasured.requests === 1 ? "is" : "are"} not measured and left out.` : "";

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

function renderProjects({ projects, sessions }) {
  const total = projects.reduce((s, p) => s + (p.est_cost_usd || 0), 0);
  Charts.hbars(document.getElementById("pj-bars"), {
    fmt: usd, empty: "No requests in this period.",
    rows: projects.slice(0, 10).map((p) => ({ label: p.project, value: p.est_cost_usd, note: count(p.requests, "request") })),
  });
  Charts.table(document.getElementById("pj-table"), [
    { label: "Project", value: (p) => p.project, href: (p) => hashFor("projects", { project: p.project }) },
    { label: "Sessions", num: true, value: (p) => int(p.sessions) },
    { label: "Requests", num: true, value: (p) => int(p.requests) },
    { label: "Cost", num: true, value: (p) => usd(p.est_cost_usd) },
    { label: "Share", num: true, value: (p) => (total ? pct((p.est_cost_usd || 0) / total) : "—") },
    { label: "Main model", value: (p) => modelInfo(p.main_model).name, key: (p) => modelInfo(p.main_model).cls },
    { label: "Last active", value: (p) => when(p.last) },
  ], projects, "No requests in this period.");

  const shown = state.project ? sessions.filter((s) => s.project === state.project) : sessions;
  document.getElementById("pj-sessions-title").textContent = state.project ? `Sessions · ${state.project}` : "Sessions";
  const sub = document.getElementById("pj-sessions-sub");
  sub.replaceChildren("Most recently active first. Click a session for its request timeline.");
  if (state.project) {
    sub.append(" ");
    html("a", null, sub, "Show all projects").href = hashFor("projects");
  }
  Charts.table(document.getElementById("pj-sessions"), [
    { label: "Started", value: (s) => when(s.first), href: (s) => hashFor("session", { id: s.session_id }) },
    { label: "Project", value: (s) => s.project },
    { label: "Length", num: true, value: (s) => duration(s.first, s.last) },
    { label: "Requests", num: true, value: (s) => int(s.requests) },
    { label: "Cost", num: true, value: (s) => usd(s.est_cost_usd) },
    { label: "Peak context", num: true, value: (s) => tok(s.peak_context) },
  ], shown, "No sessions in this period.");
}

function renderSession({ id, timeline: tl, info }) {
  document.getElementById("ss-back").href = hashFor("projects", state.project ? { project: state.project } : {});
  document.getElementById("ss-title").textContent = info ? `${info.project} · ${when(info.first)}` : "Session";
  document.getElementById("ss-sub").textContent = `Session ${id}`;
  if (!tl.length) {
    tiles(document.getElementById("ss-tiles"), [{ label: "Requests", value: "0", sub: "no requests recorded" }]);
    for (const el of ["ss-context", "ss-cost", "ss-models", "ss-table"]) document.getElementById(el).replaceChildren();
    return;
  }
  const priced = tl.filter((r) => r.est_cost_usd != null);
  const cost = priced.reduce((s, r) => s + r.est_cost_usd, 0);
  const ctx = tl.map((r) => r.context).filter((v) => v != null);
  tiles(document.getElementById("ss-tiles"), [
    { label: "API-equivalent cost", value: priced.length ? usd(cost) : "—", hero: true,
      sub: priced.length < tl.length ? `${count(tl.length - priced.length, "request")} not priced` : "estimate from token counts" },
    { label: "Requests", value: int(tl.length), sub: duration(tl[0].ts, tl[tl.length - 1].ts) },
    { label: "Peak context", value: ctx.length ? tok(Math.max(...ctx)) : "—", sub: "tokens read in one request" },
    { label: "Over 200K context", value: ctx.length ? pct(ctx.filter((v) => v > 200_000).length / ctx.length) : "—", sub: "of requests" },
  ]);

  const labels = tl.map((r) => when(r.ts));
  const x = tl.map((r) => new Date(r.ts).getTime());
  const axis = { x, xTicks: timeTicks(x[0], x[x.length - 1]) };
  const title = (i) => `${when(tl[i].ts)} · ${modelInfo(tl[i].model).name}${tl[i].effort ? ` ${tl[i].effort}` : ""}`;
  Charts.lines(document.getElementById("ss-context"), {
    ...axis, labels, title, fmt: tok, aria: "Context per request",
    series: [{ name: "context", cls: "s1", values: tl.map((r) => r.context) }],
  });
  let acc = 0;
  Charts.lines(document.getElementById("ss-cost"), {
    ...axis, labels, title, fmt: usd, tickFmt: usdTick, aria: "Cumulative cost", bridge: true,
    series: [{ name: "so far", cls: "s1", values: tl.map((r) => (r.est_cost_usd == null ? null : (acc += r.est_cost_usd))) }],
  });

  const byModel = {};
  for (const r of tl) {
    const m = (byModel[r.model] ||= { model: r.model, requests: 0, cost: 0 });
    m.requests += 1;
    m.cost += r.est_cost_usd || 0;
  }
  const models = Object.values(byModel).sort((a, b) => b.requests - a.requests);
  Charts.table(document.getElementById("ss-models"), [
    { label: "Model", value: (m) => modelInfo(m.model).name, key: (m) => modelInfo(m.model).cls },
    { label: "Requests", num: true, value: (m) => int(m.requests) },
    { label: "Share", num: true, value: (m) => pct(m.requests / tl.length) },
    { label: "Cost", num: true, value: (m) => usd(m.cost) },
  ], models);
  Charts.table(document.getElementById("ss-table"), [
    { label: "Time", value: (r) => when(r.ts) },
    { label: "Model", value: (r) => modelInfo(r.model).name, key: (r) => modelInfo(r.model).cls },
    { label: "Effort", value: (r) => r.effort || "—" },
    { label: "Context", num: true, value: (r) => tok(r.context) },
    { label: "Output", num: true, value: (r) => tok(r.output_tokens) },
    { label: "Cost", num: true, value: (r) => usd(r.est_cost_usd) },
    { label: "Source", value: (r) => r.source || "—" },
  ], tl.slice().reverse());
}

function renderRouting({ routes, cats, esc }) {
  const applied = routes.filter((r) => r.applied).length;
  const tasks = cats.reduce((s, c) => s + c.tasks, 0);
  const modes = [...new Set(routes.map((r) => r.mode).filter(Boolean))];
  tiles(document.getElementById("rt-tiles"), [
    { label: "Routing decisions", value: int(routes.length), hero: true, sub: modes.length ? `mode ${modes.join(", ")}` : "none in this period" },
    { label: "Applied", value: int(applied), sub: routes.length ? `${pct(applied / routes.length)} of decisions` : "—" },
    { label: "Tasks classified", value: int(tasks) },
    { label: "Escalations", value: int(esc.length), sub: esc.length ? "model raised mid-task" : "none yet" },
  ]);

  Charts.hbars(document.getElementById("rt-cats"), {
    fmt: int, unit: "tasks", empty: "No tasks in this period.",
    rows: cats.map((c) => ({ label: c.category || "unknown", value: c.tasks, note: `confidence ${c.avg_confidence ?? "—"}` })),
  });
  const chosen = {};
  for (const r of routes) {
    const k = r.chosen_model || "none";
    chosen[k] = (chosen[k] || 0) + 1;
  }
  Charts.hbars(document.getElementById("rt-chosen"), {
    fmt: int, unit: "decisions", empty: "No routing decisions in this period.",
    rows: Object.entries(chosen).sort((a, b) => b[1] - a[1])
      .map(([m, n]) => ({ label: modelInfo(m).name, value: n, cls: modelInfo(m).cls })),
  });
  Charts.table(document.getElementById("rt-cat-table"), [
    { label: "Category", value: (c) => c.category || "unknown" },
    { label: "Tasks", num: true, value: (c) => int(c.tasks) },
    { label: "Avg confidence", num: true, value: (c) => (c.avg_confidence == null ? "—" : c.avg_confidence.toFixed(2)) },
    { label: "Avg complexity", num: true, value: (c) => (c.avg_complexity == null ? "—" : c.avg_complexity.toFixed(1)) },
    { label: "Succeeded", num: true, value: (c) => int(c.succeeded) },
    { label: "Failed", num: true, value: (c) => int(c.failed) },
  ], cats, "No tasks in this period.");

  document.getElementById("rt-log-sub").textContent = `Newest first · ${int(routes.length)} decisions`;
  Charts.table(document.getElementById("rt-log"), [
    { label: "Time", value: (r) => when(r.ts) },
    { label: "Prompt", wide: true, value: (r) => r.prompt_excerpt || "—" },
    { label: "Category", value: (r) => r.category || "—" },
    { label: "Cx", num: true, value: (r) => r.complexity ?? "—" },
    { label: "Chosen", value: (r) => `${modelInfo(r.chosen_model).name}${r.chosen_effort ? ` · ${r.chosen_effort}` : ""}`,
      key: (r) => modelInfo(r.chosen_model).cls },
    { label: "Applied", value: (r) => yesNo(r.applied) },
    { label: "Override", value: (r) => r.user_override || "—" },
    { label: "Reason", wide: true, value: (r) => r.reason || "—" },
  ], routes, "No routing decisions in this period.");

  Charts.table(document.getElementById("rt-esc"), [
    { label: "Time", value: (e) => when(e.ts) },
    { label: "From", value: (e) => `${modelInfo(e.from_model).name}${e.from_effort ? ` · ${e.from_effort}` : ""}` },
    { label: "To", value: (e) => `${modelInfo(e.to_model).name}${e.to_effort ? ` · ${e.to_effort}` : ""}` },
    { label: "Signals", wide: true, value: (e) => (e.signals == null ? "—" : typeof e.signals === "string" ? e.signals : JSON.stringify(e.signals)) },
  ], esc, "No escalations yet.");
}

function renderQuota({ rows }) {
  const chart = document.getElementById("qt-chart");
  if (!rows.length) {
    tiles(document.getElementById("qt-tiles"), [{ label: "Plan usage", value: "—", sub: "no readings in this period" }]);
    for (const el of ["qt-legend", "qt-table"]) document.getElementById(el).replaceChildren();
    chart.replaceChildren();
    html("p", "muted", chart, "No quota readings in this period. They are recorded by the mod at each turn.");
    return;
  }
  const last = rows[rows.length - 1];
  const peak = (k) => rows.reduce((m, r) => (r[k] != null && (m == null || r[k] > m) ? r[k] : m), null);
  tiles(document.getElementById("qt-tiles"), [
    { label: "5-hour limit", value: share(last.quota_5h), hero: true, sub: `as of ${when(last.ts)}` },
    { label: "7-day limit", value: share(last.quota_7d), sub: `as of ${when(last.ts)}` },
    { label: "Peak 5-hour", value: share(peak("quota_5h")), sub: "in this period" },
    { label: "Readings", value: int(rows.length) },
  ]);
  // readings more than an hour apart: break the line, the usage in between is unknown
  const pts = [];
  rows.forEach((r, i) => {
    const t = new Date(r.ts).getTime();
    if (i && t - pts[pts.length - 1].t > 3600e3) pts.push({ t: t - 1, gap: true });
    pts.push({ t, r });
  });
  const x = pts.map((p) => p.t);
  const val = (k) => pts.map((p) => (p.gap ? null : p.r[k]));
  const series = [
    { name: "5-hour", cls: "s1", values: val("quota_5h") },
    { name: "7-day", cls: "s2", values: val("quota_7d") },
  ];
  Charts.legend(document.getElementById("qt-legend"), series);
  Charts.lines(chart, {
    labels: pts.map((p) => (p.gap ? "no readings" : when(p.r.ts))), x, xTicks: timeTicks(x[0], x[x.length - 1]), series,
    fmt: share, max: 100, aria: "Plan usage over time",
  });
  Charts.table(document.getElementById("qt-table"), [
    { label: "Time", value: (r) => when(r.ts) },
    { label: "5-hour", num: true, value: (r) => share(r.quota_5h) },
    { label: "7-day", num: true, value: (r) => share(r.quota_7d) },
  ], rows.slice().reverse());
}

function renderModels({ models }) {
  const name = { label: "Model", value: (m) => modelInfo(m.model_id).name, key: (m) => modelInfo(m.model_id).cls };
  Charts.table(document.getElementById("md-models"), [
    name,
    { label: "ID", value: (m) => m.model_id },
    { label: "Tier", num: true, value: (m) => m.tier },
    { label: "Context", num: true, value: (m) => tok(m.context_window) },
    { label: "Default effort", value: (m) => m.default_effort },
    { label: "Auto", value: (m) => yesNo(m.auto) },
    { label: "Requests", num: true, value: (m) => int(m.requests) },
    { label: "Last used", value: (m) => when(m.last_used) },
  ], models);
  Charts.table(document.getElementById("md-prices"), [
    name,
    { label: "Input", num: true, value: (m) => price(m.price.input) },
    { label: "Cache write 5m", num: true, value: (m) => price(m.price.cache_write_5m) },
    { label: "Cache write 1h", num: true, value: (m) => price(m.price.cache_write_1h) },
    { label: "Cache read", num: true, value: (m) => price(m.price.cache_read) },
    { label: "Output", num: true, value: (m) => price(m.price.output) },
    { label: "Long context", value: (m) => (m.price.long_ctx_threshold
      ? `over ${tok(m.price.long_ctx_threshold)}: ${m.price.long_ctx_multiplier}×` : "—") },
  ], models);
}

const RENDER = {
  overview: renderOverview, context: renderContext, projects: renderProjects, session: renderSession,
  routing: renderRouting, quota: renderQuota, models: renderModels,
};

function render() {
  Charts.hideTip();
  for (const p of PAGES) document.getElementById(`page-${p}`).hidden = p !== state.page;
  const tab = TAB_OF[state.page] || state.page;
  document.querySelectorAll("#tabs a").forEach((a) => {
    a.setAttribute("aria-current", a.dataset.page === tab ? "page" : "false");
    a.href = hashFor(a.dataset.page);
  });
  document.getElementById("period").hidden = NO_PERIOD.has(state.page);
  document.querySelectorAll("#period button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.days === state.days));
  const data = state.data[state.page];
  // a different session or project filter than the cached render: wait for fresh data
  if (data && !(state.page === "session" && data.id !== state.session)) RENDER[state.page](data);
}

// ---------- navigation: #page?days=N[&project=…][&id=…] ----------

function readHash() {
  const [page, query] = location.hash.slice(1).split("?");
  state.page = PAGES.includes(page) ? page : "overview";
  const q = new URLSearchParams(query || "");
  const days = q.get("days");
  if (["1", "7", "30", "all"].includes(days)) state.days = days;
  if (state.page === "projects") state.project = q.get("project");
  if (state.page === "session") state.session = q.get("id");
}

function writeHash() {
  const extra = {};
  if (state.page === "projects" && state.project) extra.project = state.project;
  if (state.page === "session" && state.session) extra.id = state.session;
  history.replaceState(null, "", hashFor(state.page, extra));
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
  state.project = null;
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
