# Usage Optimizer

See where your Claude Code usage goes and how to cut it. Usage Optimizer reads your local Claude Code
data and advises: long contexts, wasted tool output, reply length, and more. It never edits your settings.
Model Manager is one of its tools: a Claude Code mod that picks the model and effort for every turn and
records what each request cost. Everything runs on your machine and nothing leaves it.

Model Manager starts in **observe** mode: it records and shows what it would pick, but
never changes the model until you switch the mode.

Requirements: Python 3.11 or later, [uv](https://docs.astral.sh/uv/), and Claude Code 2.1.287 or later for the mod.
The dashboard and the optimizer also work without the mod, on your existing Claude Code transcripts.

## Parts

| Part | Where | What it does |
|---|---|---|
| Mod | `plugin/` | Classifies each prompt, sets model and effort per turn, escalates, writes events to `~/.model-manager/spool/`. Adds `/mm`, a stats pane (`/mm pane`) and a status line. |
| Core | `src/modelmgr/` | `model-manager` CLI: ingest, prices, stats, config, kill switch, web dashboard. Python standard library only. |
| Data | `~/.model-manager/` | `config.json`, `mm.db`, `policy.json`, `summary.json`, `dashboard.json`, `DISABLED` (kill switch). |

## Install

With Claude Code, say: *Install https://github.com/leoglockner1-dot/ai-model-manager and show me how to use it.*
Claude follows [INSTALL.md](INSTALL.md), then explains the features. Or do it by hand:

```bash
git clone https://github.com/leoglockner1-dot/ai-model-manager
cd ai-model-manager
uv tool install .
model-manager init
```

To update later, `git pull` and run
`uv tool install . --force`.

Open the dashboard (keep the terminal open; Ctrl+C stops it):

```bash
usage-optimizer --open
```

Optional, the Claude Code mod (model routing, `/mm`, stats pane, status line):

```bash
claude plugin marketplace add leoglockner1-dot/ai-model-manager
claude plugin install model-manager@ai-model-manager
```

Restart Claude Code after installing or updating the mod. Check the setup with `model-manager doctor`.
For development, use `uv tool install --editable .` from the clone and `claude plugin marketplace add "$PWD"`.

## Dashboard

```bash
usage-optimizer --open
```

It serves
`http://127.0.0.1:8765` and ingests new events every 30 seconds.

- **Overview, Context, Projects, Routing, Quota, Models**: cost, requests,
  cache share, context size, model mix, routing log, 5-hour and 7-day usage.
  Costs are API-equivalent estimates.
- **Settings**: mode, automatic models, ceiling, manual model, quota guards,
  escalation, prompt storage, and the kill switch. Saves to `config.json`;
  the mod uses it from its next prompt.

Security: binds to 127.0.0.1 only. Each start makes a new token; the URL that
`--open` opens sets it as a cookie. Writes need that cookie, a same-origin
`Origin` header and JSON, and are validated like `model-manager config`.

## Usage Optimizer

Model Manager is one tool inside the Usage Optimizer. The other tools read the same database
(`~/.model-manager/mm.db`) and **advise only**: nothing edits your settings or files.

```bash
usage-optimizer            # dashboard on Home
model-manager optimize     # run the analyzers, list open findings
model-manager status-line  # "UO: health 73 · 8 open · ≈$296/mo"
```

| Tool | Rules |
| --- | --- |
| Context & Cache | long contexts, low cache reuse, context rewritten after pauses |
| Output Lab | high output share, thinking-heavy, and a catalog of tools that shorten replies (caveman, RTK, output style, lower effort, CLAUDE.md reply rule) with detection of what is already active |
| Waste Radar | repeated file reads, oversized tool results, subagent overuse |

**Usage Health** is a 0-100 score: `100 × (1 − avoidable ÷ spend)` over the last 30 days, where avoidable is the
sum of the estimated savings of open findings, capped at 60% of spend. The Home page shows the score,
a 30-day history (one point a day) and the headline "you could save about X a month".
All savings are estimates in API-equivalent dollars; a finding without a measured number says so.

**Findings lifecycle.** Each finding has a stable id (`tool:rule:scope`) and a status: new, seen, applied or
ignored. Ignored and applied findings stay hidden. They come back as new only when the estimated saving grew by
1.5x and at least $5. A reply-shortening recommendation closes itself as applied once its change is detected,
and after 7 days shows the measured change in output tokens per request.

**Privacy.** Waste Radar reads your Claude Code transcripts locally and read-only, and caches only extracted events (file paths read, tool names, result sizes, no message text)
under `~/.model-manager/optimizer_cache/`. Nothing leaves your machine.

**Status line.** `model-manager status-line` prints one line and exits. Call it from your own status line
script, for example `echo "$(model-manager status-line)"`, or from a pane that shells out.

## Modes

| Mode | Effect |
|---|---|
| `observe` | Record only. The model is never changed. Default. |
| `auto` / `balanced` | Route by the policy. |
| `quality` | One step up from the policy's pick. |
| `cost` | One step down, except for high-risk tasks. |
| `manual` | Always `manual_model` and `manual_effort`. |
| `off` | No routing, no recording. |

Change it in the dashboard, with `/mm mode <mode>` or `model-manager mode <mode>`.
One task on a fixed model: start the prompt with `[haiku]`, `[sonnet:high]` or
`[opus]`, or use `/mm use opus`.

## Stop it

- Kill switch: dashboard Settings, `/mm off` or `model-manager off`. Undo with `model-manager on`.
- Uninstall: `claude plugin uninstall model-manager@ai-model-manager`,
  `claude plugin marketplace remove ai-model-manager`,
  `uv tool uninstall ai-model-manager`. Data stays in `~/.model-manager/`.

## Develop

```bash
python -m unittest discover -s tests
cd plugin && claude plugin validate . && claude plugin test .
```

After changing mod code, bump `plugin/.claude-plugin/plugin.json` and run
`claude plugin marketplace update ai-model-manager` and
`claude plugin update model-manager@ai-model-manager`.

## License

MIT
