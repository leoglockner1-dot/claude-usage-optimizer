"use strict";
// Usage Optimizer pages: Home, Findings, one page per optimizer tool. Loaded before app.js, which
// merges LOADERS and RENDER; helpers such as api(), post(), usd() come from app.js and are only
// called after it has loaded. All text goes in via textContent.

const Optimizer = (() => {
  const h = (...a) => Charts.html(...a);
  const NS = "http://www.w3.org/2000/svg";
  const TOOL_IDS = ["context", "output", "waste"];
  const PHASE = { context: 2, output: 3, waste: 4 }; // build phase of each analyzer, from the concept
  const TITLES = {
    home: "Home", findings: "Findings", overview: "Overview", context: "Context", projects: "Projects",
    session: "Session", routing: "Routing", policy: "Policy", quota: "Quota", models: "Models", settings: "Settings",
  };
  const fstate = { filter: "open" };
  let tools = []; // last /api/tools answer, for titles and badges

  function title(page, tool) {
    if (page === "tool") return tools.find((t) => t.tool === tool)?.name || "Optimizer";
    return TITLES[page] || "Usage Optimizer";
  }

  function svgEl(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  }

  // ---------- loaders ----------

  const LOADERS = {
    home: () => api("home").then((home) => { tools = home.tools; return { home }; }),
    findings: () => api("findings?all=1").then((findings) => ({ findings })),
    tool: () => {
      const tool = state.tool;
      return Promise.all([api("tools"), api("findings"), tool === "context" || tool === "output" ? api(tool + "?days=30") : null]).then(([t, f, hist]) => {
        tools = t;
        return { tool, info: t.find((x) => x.tool === tool), findings: f.filter((x) => x.tool === tool), hist };
      });
    },
  };

  // nav: tool badges and the Model Manager mode; never blocks a page load
  function renderNav(status) {
    document.getElementById("nav-mode").textContent = status.disabled ? "off" : status.mode || "";
    api("tools").then((t) => {
      tools = t;
      let all = 0;
      for (const x of t) {
        const b = document.getElementById(`badge-${x.tool}`);
        if (!b) continue;
        b.hidden = !x.open;
        b.textContent = x.open;
        all += x.open;
      }
      const b = document.getElementById("badge-all");
      b.hidden = !all;
      b.textContent = all;
      document.getElementById("page-title").textContent = title(state.page, state.tool);
    }).catch(() => {});
  }

  // ---------- pieces ----------

  function ring(container, score) {
    container.replaceChildren();
    const s = svgEl("svg", { viewBox: "0 0 132 132", class: "ring", role: "img",
      "aria-label": score == null ? "Usage health not measured yet" : `Usage health ${score} of 100` }, container);
    const r = 56, c = 2 * Math.PI * r;
    svgEl("circle", { cx: 66, cy: 66, r, class: "track" }, s);
    const arc = svgEl("circle", { cx: 66, cy: 66, r, class: "arc", transform: "rotate(-90 66 66)",
      "stroke-dasharray": c, "stroke-dashoffset": c }, s);
    const colour = score == null ? "var(--axis)" : score >= 80 ? "var(--good)" : score >= 55 ? "var(--warn)" : "var(--bad)";
    arc.style.setProperty("--ring", colour);
    const t = svgEl("text", { x: 66, y: 62, class: "score" }, s);
    t.textContent = score == null ? "—" : String(score);
    const o = svgEl("text", { x: 66, y: 90, class: "of" }, s);
    o.textContent = score == null ? "not measured" : "of 100";
    // animate from empty on the next frame
    requestAnimationFrame(() => requestAnimationFrame(() => {
      arc.setAttribute("stroke-dashoffset", score == null ? c : c * (1 - score / 100));
    }));
  }

  function spark(container, rows) {
    container.replaceChildren();
    const vals = rows.map((d) => d.est_cost_usd || 0);
    if (vals.length < 2) return;
    const W = 300, H = 56, max = Math.max(...vals) || 1;
    const s = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", class: "spark", "aria-hidden": "true" }, container);
    const pts = vals.map((v, i) => [(i / (vals.length - 1)) * W, H - 4 - (v / max) * (H - 8)]);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
    svgEl("path", { d: `${line}L${W},${H}L0,${H}Z`, class: "area" }, s);
    svgEl("path", { d: line, class: "line", "vector-effect": "non-scaling-stroke" }, s);
  }

  function empty(container, head, text) {
    const e = h("div", "card glass empty fade-in", container);
    h("strong", null, e, head);
    h("span", null, e, text);
  }

  function findingCard(container, f, onChange) {
    const toolName = tools.find((t) => t.tool === f.tool)?.name || f.tool;
    const card = h("article", `card glass finding t-${f.tool} fade-in`, container);
    const top = h("div", "finding-top", card);
    h("h3", null, top, f.title);
    h("span", "chip tool", top, toolName);
    h("span", `chip sev-${f.severity}`, top, `${f.severity} impact`);
    h("span", "chip", top, `${f.confidence} confidence`);
    if (f.status !== "new") h("span", "chip", top, f.status);
    const meta = h("p", "sub", card);
    if (f.est_saving_usd != null) {
      h("span", "saving", meta, `≈ ${usd(f.est_saving_usd)} / 30 days`);
      meta.append(" estimated saving");
      if (f.est_saving_tokens != null) meta.append(` · ${tok(f.est_saving_tokens)} tokens`);
    } else meta.append("Saving not estimated");
    if (f.evidence?.summary) { meta.append(" · "); meta.append(f.evidence.summary); }
    const fix = h("div", "fix", card);
    h("span", null, fix, f.fix);
    const copy = h("button", null, fix, "Copy");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(f.fix); copy.textContent = "Copied"; }
      catch { copy.textContent = "Select and copy"; }
      setTimeout(() => { copy.textContent = "Copy"; }, 1500);
    });
    const act = h("div", "finding-actions", card);
    const msg = h("span", "muted", act);
    const choices = f.status === "applied" || f.status === "ignored"
      ? [["new", "Reopen"]] : [["applied", "Mark applied"], ["ignored", "Ignore"]];
    for (const [status, label] of choices) {
      const b = h("button", null, act, label);
      b.type = "button";
      b.addEventListener("click", async () => {
        b.disabled = true;
        try { await post("finding-status", { id: f.id, status }); onChange(); }
        catch (e) { msg.textContent = e.message; b.disabled = false; }
      });
      act.insertBefore(b, msg);
    }
  }

  // ---------- pages ----------

  function renderSaved(s) {
    const box = document.getElementById("hm-saved");
    box.replaceChildren();
    const tile = (label, value, sub) => {
      const d = h("div", "tile glass", box);
      h("div", "tile-label", d, label);
      h("div", "tile-value", d, value);
      h("div", "tile-sub", d, sub);
    };
    const q = s.mm_week_quota_points;
    tile("Model Manager · this week", usd(s.mm_week_usd),
      s.mm_week_routed ? `${pct(s.mm_week_share)} of the week's usage${q != null ? ` · about ${+q.toFixed(1)} quota points` : ""}`
                       : "no routed requests this week");
    tile("Model Manager · all time", usd(s.mm_total_usd), `${int(s.mm_total_routed)} routed requests`);
    tile("Optimizer · applied", usd(s.uo_applied_usd_30d), `${int(s.uo_applied_count)} findings, per 30 days`);
    tile("Optimizer · still open", usd(s.uo_open_usd_30d), "potential per 30 days");
    tile("Overall estimate", usd(s.overall_usd), "MM all time + applied findings");
    document.getElementById("hm-saved-note").textContent =
      "All figures are estimates in API-equivalent dollars. MM compares routed requests with the baseline model; quota points are rough.";
  }

  function renderHome({ home }) {
    const hl = home.health;
    ring(document.getElementById("hm-ring"), hl.score);
    const head = document.getElementById("hm-head");
    const built = home.tools.filter((t) => t.built).length;
    if (hl.score == null) {
      head.textContent = built ? "Waiting for the first analyzer run" : "Analyzers arrive next";
      document.getElementById("hm-rule").textContent = built
        ? "Runs with every ingest (every 30 s while the dashboard is open)."
        : `The shell is ready. Context & Cache comes in phase ${PHASE.context}, Output Lab in ${PHASE.output}, Waste Radar in ${PHASE.waste}.`;
    } else {
      head.textContent = hl.avoidable_usd ? `You could save about ${usd(hl.avoidable_usd)} a month` : "No avoidable spend found";
      document.getElementById("hm-rule").textContent = `Estimate. Score = ${hl.rule}.`;
    }
    const sp = home.spend;
    document.getElementById("hm-spend").textContent = usd(sp.est_cost_usd);
    spark(document.getElementById("hm-spark"), home.daily);
    document.getElementById("hm-spend-sub").textContent =
      `${int(sp.requests)} requests · ${int(sp.sessions)} sessions · ${tok(sp.tokens?.output)} output tokens · cache read ${pct(sp.cache_read_share)} of cost`;

    renderSaved(home.savings);

    const top = document.getElementById("hm-top");
    top.replaceChildren();
    if (!home.top.length) {
      empty(top, "No open findings", built ? "Nothing to fix right now." : "Findings appear here once the first analyzer is built.");
    } else for (const f of home.top) findingCard(top, f, load);

    const grid = document.getElementById("hm-tools");
    grid.replaceChildren();
    for (const t of home.tools) {
      const a = h("a", `card glass tool-card t-${t.tool} fade-in`, grid);
      a.href = hashFor("tool", { t: t.tool });
      const h3 = h("h3", null, a);
      h("span", "dot", h3);
      h3.append(t.name);
      h("p", null, a, t.about);
      const st = h("div", "tool-state", a);
      if (!t.built) st.textContent = `Coming in phase ${PHASE[t.tool]}`;
      else if (t.error) st.textContent = `Last run failed: ${t.error}`;
      else { h("strong", null, st, String(t.open)); st.append(` open · ran ${when(t.last_run)}`); }
    }
    const mm = h("a", "card glass tool-card fade-in", grid);
    mm.href = hashFor("overview");
    const h3 = h("h3", null, mm);
    h("span", "dot", h3);
    h3.append("Model Manager");
    h("p", null, mm, "Routes each turn to the right model and effort, and learns from the results.");
    const st = h("div", "tool-state", mm);
    h("strong", null, st, home.mm.disabled ? "off" : home.mm.mode);
    const sv = home.mm.savings;
    st.append(sv.est_savings_usd != null ? ` · saved ≈ ${usd(sv.est_savings_usd)} in 30 days` : " · routing not active yet");
  }

  function renderFindings({ findings }) {
    const filters = [["open", "Open"], ["applied", "Applied"], ["ignored", "Ignored"], ["all", "All"]];
    const bar = document.getElementById("fd-filter");
    bar.replaceChildren();
    for (const [k, label] of filters) {
      const b = h("button", null, bar, label);
      b.type = "button";
      b.setAttribute("aria-pressed", fstate.filter === k);
      b.addEventListener("click", () => { fstate.filter = k; render(); });
    }
    const keep = (f) => (fstate.filter === "all" ? true
      : fstate.filter === "open" ? f.active && (f.status === "new" || f.status === "seen") : f.status === fstate.filter);
    const list = document.getElementById("fd-list");
    list.replaceChildren();
    const shown = findings.filter(keep);
    if (!shown.length) empty(list, "Nothing here", findings.length ? "No findings with this status." : "No analyzer has produced a finding yet.");
    for (const f of shown) findingCard(list, f, load);
  }

  function renderCostBySize(hist) {
    const box = document.getElementById("tl-extra");
    const measured = (hist || []).filter((b) => b.lo != null);
    box.hidden = !measured.some((b) => b.requests);
    if (box.hidden) return;
    Charts.columns(document.getElementById("tl-hist"), {
      labels: measured.map((b) => b.label), series: [{ key: "c", name: "cost", cls: "s1" }],
      rows: measured.map((b) => ({ c: b.est_cost_usd })), fmt: usd, tickFmt: usdTick, height: 220,
      aria: "Cost by context size", capLabel: (i) => pct(measured[i].cost_share),
      title: (i) => `${measured[i].label} tokens`,
      extra: (i) => [{ value: int(measured[i].requests), label: "requests" },
                     { value: pct(measured[i].cost_share), label: "of cost" }],
    });
  }

  function renderOutputExtras(data) {
    const box = document.getElementById("tl-extra");
    const rows = (data?.by_model || []).filter((g) => g.output_cost_usd);
    box.hidden = !rows.length;
    if (rows.length) {
      document.getElementById("tl-extra-title").textContent = "Output cost by model and effort";
      document.getElementById("tl-extra-sub").textContent =
        "API-equivalent estimate, last 30 days. Thinking tokens count as output.";
      Charts.hbars(document.getElementById("tl-hist"), {
        rows: rows.slice(0, 8).map((g) => ({
          label: `${g.model} · ${g.effort}`, value: g.output_cost_usd,
          note: `${int(g.avg_output)} output tokens per request, ${int(g.requests)} requests`,
        })), fmt: usd, unit: "output cost",
      });
    }
    const tools = document.getElementById("tl-tools");
    tools.hidden = !(data?.tools || []).length;
    const list = document.getElementById("tl-tools-list");
    list.replaceChildren();
    for (const t of data?.tools || []) {
      const row = h("div", "tool-row", list);
      h("span", `chip st-${t.state}`, row, t.state === "active" ? "active" : t.state === "installed" ? "installed, off" : "not set up");
      h("strong", null, row, " " + t.name);
      h("span", "muted", row, ` · reduces ${t.reduces}`);
    }
  }

  function renderTool({ tool, info, findings, hist }) {
    const hero = document.getElementById("tl-hero");
    hero.className = `card glass tool-hero t-${tool}`;
    hero.replaceChildren();
    const h2 = h("h2", null, hero);
    h("span", "dot", h2);
    h2.append(info?.name || tool);
    h("p", null, hero, info?.about || "");
    const list = document.getElementById("tl-list");
    list.replaceChildren();
    document.getElementById("tl-tools").hidden = true;
    if (tool === "output") renderOutputExtras(hist);
    else {
      document.getElementById("tl-extra-title").textContent = "Cost by context size";
      document.getElementById("tl-extra-sub").textContent =
        "API-equivalent estimate, last 30 days. Every token above the limit is read again on each following request.";
      renderCostBySize(hist);
    }
    if (!info?.built) {
      empty(list, `Analyzer comes in phase ${PHASE[tool]}`,
        tool === "context" ? "Until then, the Model Manager Context page shows the raw context data."
          : "This page fills with findings once its analyzer is built.");
      if (tool === "context") {
        const a = h("a", null, list.lastChild, "Open the Context page");
        a.href = hashFor("context");
        a.style.display = "block";
        a.style.marginTop = "8px";
      }
      return;
    }
    if (!findings.length) empty(list, "No findings", "This tool found nothing to fix in your current data.");
    for (const f of findings) findingCard(list, f, load);
  }

  return { LOADERS, RENDER: { home: renderHome, findings: renderFindings, tool: renderTool }, TOOL_IDS, title, renderNav };
})();

document.getElementById("theme-toggle").addEventListener("click", (e) => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("uo-theme", next); } catch { /* not stored: fine for this visit */ }
  e.currentTarget.textContent = next === "dark" ? "Light theme" : "Dark theme";
  render(); // charts read colours at draw time
});
document.getElementById("theme-toggle").textContent =
  document.documentElement.dataset.theme === "dark" ? "Light theme" : "Dark theme";
