# model-manager (Claude Code mod)

Part of the AI Model Manager. On every prompt it classifies the task locally (no model call), chooses a
model and effort for the turn from `~/.model-manager/policy.json`, and applies it through the `turn.step`
event. It routes read-only `Explore` subagents to Haiku, watches test and build commands to escalate when a
task struggles, and writes measurements to `~/.model-manager/spool/` for the Python core.

Modes (`/mm mode <m>` or `model-manager mode <m>`): observe (record only, default), auto, balanced,
quality, cost, manual, off. Kill switch: `/mm off` or `model-manager off`. Override one task by starting
a prompt with `[haiku]`, `[sonnet:high]`, `[opus]`, or with `/mm use <model[:effort]>`. Grade a task with
`/mm good` or `/mm bad`. If anything in the mod fails, Claude Code skips the hook and keeps its own model.
