# model-manager (Claude Code mod)

Part of Claude Usage Optimizer. On every prompt it classifies the task locally (no model call), chooses a
model and effort for the turn from `~/.model-manager/policy.json`, and applies it through the `turn.step`
event. It routes read-only `Explore` subagents to Haiku, watches test and build commands to escalate when a
task struggles, and writes measurements to `~/.model-manager/spool/` for the Python core.

Modes (`/mm mode <m>` or `model-manager mode <m>`): observe (record only, default), auto, balanced,
quality, cost, manual, off. Kill switch: `/mm off` or `model-manager off`. Override one task by starting
a prompt with `[haiku]`, `[sonnet:high]`, `[opus]`, or with `/mm use <model[:effort]>`. Grade a task with
`/mm good` or `/mm bad`.

`/mm pane` opens a stats pane: this session's cost and context, 5-hour and 7-day plan usage, the last 7
days from `~/.model-manager/summary.json` (written by `model-manager ingest`), and buttons for mode, grading
and pinning. The status line shows `mm <model>·<effort> · ctx <tokens> · 5h <percent>`.
When it is time to leave the session it adds a hint after `⚠`:

- `start a new session`: context is at 250K or more.
- `task done · new session is cheap`: the last task passed its tests (or you marked it good) and context is 60K or more.
- `/compact soon`: context is at 150K or more.
- `cache cold · /compact or new session`: more than 5 minutes since the last request and context is 100K or more, so the next request rewrites the whole context.

If anything in the mod fails, Claude Code skips the hook and keeps its own model.
