# AI Model Manager

Model routing and usage tracking for Claude Code. A Claude Code mod picks the
model and effort for every turn, and moves a task up one step when it keeps
failing. It records what each request cost. A Python core stores that in
SQLite and shows it in a local dashboard and a CLI. Nothing leaves the machine.

It starts in **observe** mode: it records and shows what it would pick, but
never changes the model until you switch the mode.

## Parts

| Part | Where | What it does |
|---|---|---|
| Mod | `plugin/` | Classifies each prompt, sets model and effort per turn, escalates, writes events to `~/.model-manager/spool/`. Adds `/mm`, a stats pane (`/mm pane`) and a status line. |
| Core | `src/modelmgr/` | `model-manager` CLI: ingest, prices, stats, config, kill switch, web dashboard. Python standard library only. |
| Data | `~/.model-manager/` | `config.json`, `mm.db`, `policy.json`, `summary.json`, `dashboard.json`, `DISABLED` (kill switch). |

## Install

```bash
uv tool install --editable .
model-manager init
claude plugin marketplace add "$PWD"
claude plugin install model-manager@ai-model-manager
```

Mods need Claude Code 2.1.287 or later. Restart Claude Code after installing or updating the mod. Check the setup with
`model-manager doctor`.

## Dashboard

```bash
model-manager dashboard --open
```

Run it in a terminal window and keep it open; Ctrl+C stops it. It serves
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
