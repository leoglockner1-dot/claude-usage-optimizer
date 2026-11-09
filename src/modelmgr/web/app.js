"use strict";

const { html } = Charts;
const REFRESH_MS = 60_000; // the server ingests every 30 s
const PAGES = ["overview", "context", "projects", "session", "routing", "quota", "models", "settings"];
const NO_PERIOD = new Set(["session", "models", "settings"]); // pages that ignore the period filter
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

// the only writes: JSON to the same origin, so the browser adds the Origin the server checks
async function post(path, value) {
  const r = await fetch(`/api/${path}`, {
    method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `${path}: HTTP ${r.status}`);
  return body;
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
  routing: (q) => Promise.all([api("routes?limit=5000"), api(`categories?${q}`), api("escalations?limit=1000"), api("grades")])
    .then(([routes, cats, esc, grades]) => {
      const since = sinceIso();
      return { grades, routes: routes.filter((r) => r.ts >= since), cats, esc: esc.filter((r) => (r.ts || "") >= since) };
    }),
  quota: (q) => api(`quota?${q}`).then((rows) => ({ rows })),
  models: () => api("models").then((models) => ({ models })),
  settings: () => Promise.all([api("config"), api("status"), api("models")])
    .then(([cfg, status, models]) => ({ cfg, status, models })),
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

// ---------- judging routing decisions ----------

const VERDICTS = [["", "—"], ["right", "right"], ["too_weak", "too weak"], ["too_strong", "too strong"], ["wrong_category", "wrong category"]];
const VERDICT_LABEL = Object.fromEntries(VERDICTS);
const BETTER = ["", "haiku:low", "haiku:medium", "sonnet:low", "sonnet:medium", "sonnet:high",
  "opus:medium", "opus:high", "opus:xhigh", "fable:high"];

async function saveGrade(r, patch, status) {
  const next = { verdict: r.verdict || null, better: r.better || null, note: r.grade_note || null, ...patch };
  status.textContent = "saving…";
  try {
    await post("grade", { route_id: r.route_id, ...next });
    Object.assign(r, { verdict: next.verdict, better: next.better, grade_note: next.note });
    status.textContent = next.verdict ? "saved" : "cleared";
  } catch (e) {
    status.textContent = e.message;
  }
}

function judgeCell(r) {
  const box = html("div", "judge");
  if (r.route_id == null) { box.textContent = "—"; return box; }
  const sel = html("select", null, box);
  for (const [v, l] of VERDICTS) { const o = html("option", null, sel, l); o.value = v; }
  sel.value = r.verdict || "";
  sel.title = "Was this model the right choice?";
  const better = html("select", null, box);
  for (const v of BETTER) { const o = html("option", null, better, v || "better: —"); o.value = v; }
  better.value = r.better || "";
  better.title = "Which model and effort would have been right";
  const note = html("input", null, box);
  note.placeholder = "note";
  note.maxLength = 500;
  note.value = r.grade_note || "";
  const status = html("span", "muted judge-status", box);
  const toggle = () => { better.hidden = !(sel.value === "too_weak" || sel.value === "too_strong"); };
  toggle();
  sel.addEventListener("change", () => {
    toggle();
    if (!better.hidden && !better.value) better.value = "";
    saveGrade(r, { verdict: sel.value || null, better: better.hidden ? null : better.value || null }, status);
  });
  better.addEventListener("change", () => saveGrade(r, { better: better.value || null }, status));
  note.addEventListener("change", () => saveGrade(r, { note: note.value.trim() || null }, status));
  return box;
}

function renderRouting({ routes, cats, esc, grades }) {
  const applied = routes.filter((r) => r.applied).length;
  const tasks = cats.reduce((s, c) => s + c.tasks, 0);
  const modes = [...new Set(routes.map((r) => r.mode).filter(Boolean))];
  tiles(document.getElementById("rt-tiles"), [
    { label: "Routing decisions", value: int(routes.length), hero: true, sub: modes.length ? `mode ${modes.join(", ")}` : "none in this period" },
    { label: "Applied", value: int(applied), sub: routes.length ? `${pct(applied / routes.length)} of decisions` : "—" },
    { label: "Tasks classified", value: int(tasks) },
    { label: "Escalations", value: int(esc.length), sub: esc.length ? "model raised mid-task" : "none yet" },
    (() => {
      const judged = routes.filter((r) => r.verdict);
      const right = judged.filter((r) => r.verdict === "right").length;
      return { label: "Judged by you", value: int(judged.length),
        sub: judged.length ? `${pct(right / judged.length)} right` : "pick a verdict in the log" };
    })(),
  ]);

  Charts.table(document.getElementById("rt-grades"), [
    { label: "Category", value: (g) => g.category || "unknown" },
    { label: "Chosen", value: (g) => modelInfo(g.chosen_model).name, key: (g) => modelInfo(g.chosen_model).cls },
    { label: "Verdict", value: (g) => VERDICT_LABEL[g.verdict] || g.verdict },
    { label: "Count", num: true, value: (g) => int(g.n) },
  ], grades || [], "No verdicts yet. Judge decisions in the routing log below.");

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
    { label: "Your verdict", value: judgeCell },
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

// ---------- settings: the one page that writes ----------

const MODE_HELP = {
  observe: "Record usage and what the router would pick. The model is never changed. Start here.",
  auto: "Route every turn by the learned policy.",
  balanced: "Same as auto today: the policy's pick, no shift.",
  quality: "One step up the ladder from the policy's pick.",
  cost: "One step down the ladder, except for high-risk tasks.",
  manual: "Always the manual model and effort below.",
  off: "No routing and no recording. Same effect as the kill switch, but saved as a mode.",
};

// field kinds: select, models (checkboxes), bool, int, num, pct (stored 0–1, shown 0–100)
const SETTINGS = [
  { title: "Routing", fields: [
    { key: "mode", label: "Mode", kind: "select", options: () => Object.keys(MODE_HELP), help: (v) => MODE_HELP[v] },
  ] },
  { title: "Models", sub: "Which models the router may choose", fields: [
    { key: "auto_models", label: "Automatic models", kind: "models",
      help: "The router picks only from these on its own. Fable is never picked automatically." },
    { key: "ceiling", label: "Ceiling", kind: "select", options: (m) => m.map((x) => x.alias),
      help: "Highest model reachable at all, also by escalation and /mm use." },
    { key: "manual_model", label: "Manual model", kind: "select", options: (m) => m.map((x) => x.alias),
      help: "Used in manual mode." },
    { key: "manual_effort", label: "Manual effort", kind: "select", options: () => ["low", "medium", "high", "xhigh", "max"] },
  ] },
  { title: "Guards", sub: "Limits the router keeps to", fields: [
    { key: "quota_guard.five_hour", label: "5-hour quota guard", kind: "int", unit: "%", min: 0, max: 100,
      help: "Above this share of the 5-hour limit the router stops upgrading past Sonnet. Escalation, overrides and high-risk tasks still upgrade." },
    { key: "quota_guard.seven_day", label: "7-day quota guard", kind: "int", unit: "%", min: 0, max: 100,
      help: "The same for the 7-day limit." },
    { key: "haiku_main_max_context", label: "Haiku context limit", kind: "int", unit: "tokens", min: 0, max: 1000000, step: 1000,
      help: "Above this context the main session gets Sonnet instead of Haiku." },
    { key: "min_switch_saving", label: "Minimum saving to switch down", kind: "pct", unit: "%", min: 0, max: 100,
      help: "A cheaper model is taken only if it saves at least this share over about three turns. A switch rewrites the cache." },
  ] },
  { title: "Escalation", sub: "Moving a task up after it goes wrong", fields: [
    { key: "escalation.enabled", label: "Escalate", kind: "bool", text: "Move a task one step up after repeated failures" },
    { key: "escalation.threshold", label: "Failure threshold", kind: "num", min: 0.5, max: 10, step: 0.5,
      help: "Failure signals (failed tests, corrections) a task collects before it moves up." },
    { key: "escalation.max_auto", label: "Steps per task", kind: "int", min: 0, max: 5,
      help: "Most automatic steps up within one task." },
  ] },
  { title: "Subagents and privacy", fields: [
    { key: "route_subagents", label: "Explore subagents", kind: "bool", text: "Pick the model for Explore subagents that set none" },
    { key: "store_prompts", label: "Stored prompt text", kind: "select", options: () => ["excerpt", "hash"],
      names: { excerpt: "First 200 characters", hash: "Hash only" }, help: "What the local database keeps of each prompt." },
  ] },
];

// errors: per-field input errors that block Save; server: the last refusal, shown until the next edit
const st = { saved: null, draft: null, built: false, errors: {}, server: null, busy: false };
const getPath = (o, k) => k.split(".").reduce((n, p) => (n == null ? n : n[p]), o);
function setPath(o, k, v) {
  const parts = k.split(".");
  let n = o;
  for (const p of parts.slice(0, -1)) n = n[p] ??= {};
  n[parts[parts.length - 1]] = v;
}
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const allFields = () => SETTINGS.flatMap((c) => c.fields);
const changedKeys = () => allFields().filter((f) => !same(getPath(st.draft, f.key), getPath(st.saved, f.key))).map((f) => f.key);

function renderSettings({ cfg, status, models }) {
  renderKill(status);
  const dirty = st.draft && changedKeys().length > 0;
  // an auto-refresh never overwrites edits; it only refreshes a clean form when the file changed
  if (!st.saved || (!dirty && !same(cfg, st.saved))) { st.saved = cfg; st.draft = structuredClone(cfg); st.built = false; }
  if (!st.built) buildForm(models);
  syncForm();
}

function renderKill(status) {
  const box = document.getElementById("st-kill");
  box.replaceChildren();
  const row = html("div", "kill", box);
  const left = html("div", "", row);
  html("h2", "", left, "Kill switch");
  const line = html("p", "sub", left);
  const state = html("span", `state${status.disabled ? " off" : ""}`, line, status.disabled ? "On: nothing is routed or recorded" : "Off: Model Manager runs");
  line.append(status.disabled ? ". Sessions keep their own model." : ` in ${status.mode} mode.`);
  state.setAttribute("aria-live", "polite");
  const b = html("button", status.disabled ? "" : "danger", row, status.disabled ? "Turn Model Manager back on" : "Stop everything");
  b.type = "button";
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await post("killswitch", { disabled: !status.disabled }); await load(); } catch (e) { setMsg(e.message, true); b.disabled = false; }
  });
}

function buildForm(models) {
  const root = document.getElementById("st-cards");
  root.replaceChildren();
  for (const card of SETTINGS) {
    const c = html("div", "card", root);
    html("h2", "", c, card.title);
    if (card.sub) html("p", "sub", c, card.sub);
    for (const f of card.fields) buildField(c, f, models);
  }
  st.built = true;
}

function buildField(card, f, models) {
  const row = html("div", "field", card);
  row.dataset.key = f.key;
  const id = `st-${f.key.replace(".", "-")}`;
  const label = html(f.kind === "models" ? "div" : "label", "field-label", row, f.label);
  const body = html("div", "", row);
  if (f.kind !== "models") label.htmlFor = id;
  if (f.kind === "select") {
    const sel = html("select", "", body);
    sel.id = id;
    for (const v of f.options(models)) {
      const o = html("option", "", sel, f.names?.[v] || v);
      o.value = v;
    }
    sel.addEventListener("change", () => edit(f, sel.value));
  } else if (f.kind === "models") {
    const box = html("div", "choices", body);
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", f.label);
    for (const m of models.filter((x) => x.auto)) {
      const l = html("label", "", box);
      const cb = html("input", "", l);
      cb.type = "checkbox";
      cb.value = m.alias;
      l.append(modelInfo(m.model_id).name);
      cb.addEventListener("change", () => {
        const on = [...box.querySelectorAll("input:checked")].map((x) => x.value);
        edit(f, models.filter((x) => on.includes(x.alias)).map((x) => x.alias)); // keep registry order
      });
    }
  } else if (f.kind === "bool") {
    const l = html("label", "check", body);
    const cb = html("input", "", l);
    cb.type = "checkbox";
    cb.id = id;
    l.append(f.text);
    cb.addEventListener("change", () => edit(f, cb.checked));
  } else {
    const inp = html("input", "", body);
    Object.assign(inp, { id, type: "number", min: f.min, max: f.max, step: f.step || (f.kind === "num" ? "any" : 1), inputMode: "decimal" });
    if (f.unit) html("span", "unit", body, f.unit);
    inp.addEventListener("input", () => {
      const n = inp.value.trim() === "" ? NaN : Number(inp.value);
      const bad = !Number.isFinite(n) || n < f.min || n > f.max || (f.kind !== "num" && !Number.isInteger(n));
      st.errors[f.key] = bad ? `Enter ${f.kind === "num" ? "a number" : "a whole number"} from ${int(f.min)} to ${int(f.max)}.` : null;
      if (!bad) edit(f, f.kind === "pct" ? Math.round(n * 100) / 10000 : n);
      else syncForm();
    });
  }
  html("div", "field-help", body).hidden = true;
  html("div", "field-error", body).hidden = true;
}

function edit(f, value) {
  setPath(st.draft, f.key, value);
  st.server = null;
  setMsg("");
  syncForm();
}

// writes the draft into the controls and the dirty/error marks; never rebuilds the controls
function syncForm() {
  for (const f of allFields()) {
    const row = document.querySelector(`.field[data-key="${f.key}"]`);
    if (!row) continue;
    const v = getPath(st.draft, f.key);
    if (f.kind === "select") row.querySelector("select").value = v;
    else if (f.kind === "models") row.querySelectorAll("input").forEach((cb) => { cb.checked = v.includes(cb.value); });
    else if (f.kind === "bool") row.querySelector("input").checked = !!v;
    else {
      const inp = row.querySelector("input");
      const shown = f.kind === "pct" ? +(v * 100).toFixed(2) : v;
      if (document.activeElement !== inp) inp.value = shown;
    }
    row.classList.toggle("changed", !same(v, getPath(st.saved, f.key)));
    const help = row.querySelector(".field-help");
    const text = typeof f.help === "function" ? f.help(v) : f.help;
    help.textContent = text || "";
    help.hidden = !text;
    const err = row.querySelector(".field-error");
    const text2 = st.errors[f.key] || (st.server?.key === f.key ? st.server.text : "");
    err.textContent = text2;
    err.hidden = !text2;
  }
  const n = changedKeys().length;
  const invalid = Object.values(st.errors).some(Boolean);
  document.getElementById("st-save").disabled = st.busy || !n || invalid;
  document.getElementById("st-discard").disabled = st.busy || !n;
  document.getElementById("st-save").textContent = n ? `Save ${count(n, "change")}` : "Save changes";
}

function setMsg(text, isError = false) {
  const m = document.getElementById("st-msg");
  m.textContent = text;
  m.classList.toggle("error", isError);
}

document.getElementById("st-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const keys = changedKeys();
  if (!keys.length) return;
  const patch = Object.fromEntries(keys.map((k) => [k, getPath(st.draft, k)]));
  st.busy = true;
  syncForm();
  try {
    const cfg = await post("config", patch);
    st.saved = cfg;
    st.draft = structuredClone(cfg);
    st.errors = {};
    st.server = null;
    setMsg(`Saved. The mod uses it from the next prompt.`);
  } catch (err) {
    // the server names the key first ("quota_guard.five_hour must be …"): show it at that field too
    const key = allFields().map((f) => f.key).find((k) => err.message.startsWith(k));
    if (key) st.server = { key, text: err.message.slice(key.length).replace(/^[:\s]+/, "") };
    setMsg(`Not saved: ${err.message}`, true);
  } finally {
    st.busy = false;
    syncForm();
    if (!changedKeys().length) load();
  }
});
document.getElementById("st-discard").addEventListener("click", () => {
  st.draft = structuredClone(st.saved);
  st.errors = {};
  st.server = null;
  st.built = false;
  setMsg("");
  load();
});
window.addEventListener("beforeunload", (e) => { if (st.draft && changedKeys().length) e.preventDefault(); });

const RENDER = {
  overview: renderOverview, context: renderContext, projects: renderProjects, session: renderSession,
  routing: renderRouting, quota: renderQuota, models: renderModels, settings: renderSettings,
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
// skip a refresh while a verdict control has focus: re-rendering the log would drop the edit
setInterval(() => { if (!document.hidden && !document.activeElement?.closest?.(".judge")) load(); }, REFRESH_MS);
