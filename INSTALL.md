# Install guide for Claude

You are Claude Code. A user asked you to install Usage Optimizer from this repository. Follow these steps,
then explain the tool to the user. Ask before running anything outside this list.

## 1. Check requirements

- macOS, Linux or Windows. On Windows, run the commands below in PowerShell.
- `git --version` must work.
- `python3 --version` must be 3.11 or later (on Windows: `python --version` or `py --version`).
- `uv --version` must work. If not, tell the user to install uv (https://docs.astral.sh/uv/) and stop.

## 2. Install

```bash
git clone https://github.com/leoglockner1-dot/claude-usage-optimizer
cd claude-usage-optimizer
uv tool install .
model-manager init
model-manager doctor
```

`doctor` should show only ticks. If a line fails, read its hint, fix it, and run `doctor` again.

## 3. Optional: the Claude Code mod

Run `claude --version` first. If it is older than 2.1.287, skip this step and tell the user the mod needs
2.1.287 or later.

Ask the user first: "Do you also want the Model Manager mod? It picks the model and effort per turn. It starts
in observe mode and changes nothing until you switch the mode." If yes:

```bash
claude plugin marketplace add leoglockner1-dot/claude-usage-optimizer
claude plugin install model-manager@claude-usage-optimizer
```

Needs Claude Code 2.1.287 or later. Tell the user to restart Claude Code afterwards.

## 4. Fill the first data

```bash
model-manager backfill
model-manager optimize
```

`backfill` imports existing Claude Code transcripts. `optimize` runs the analyzers and lists findings.
If there is no data yet, say so: the optimizer needs some Claude Code usage first.

## 5. Tell the user what they got

Explain in plain words, short:

- **Usage Optimizer** reads local Claude Code data and advises how to cut usage. It never edits settings or files.
  Nothing leaves the machine.
- **Feedback:** the *Send feedback* button in the dashboard sidebar opens a GitHub issue.
- **Dashboard:** run `usage-optimizer --open` in a terminal and keep it open (Ctrl+C stops it). Home shows a
  Usage Health score from 0 to 100, a 30-day history, and the estimated monthly saving.
- **Three tools:** Context & Cache (long contexts, low cache reuse, rewrites after pauses), Output Lab (long or
  thinking-heavy replies, and tools that shorten them), Waste Radar (repeated file reads, oversized tool
  results, subagent overuse).
- **Findings:** each has evidence, a fix to copy, and an estimated saving. Mark it seen, applied or ignored.
  Ignored findings stay hidden unless the saving grows a lot. All figures are estimates.
- **CLI:** `model-manager optimize` lists findings, `model-manager status-line` prints one summary line.
- **Model Manager** (if the mod is installed): `/mm` shows state, `/mm pane` opens a stats pane,
  `/mm mode auto` turns routing on, `/mm off` is the kill switch. Observe mode is the default.
- **Update later:** `git pull` in the clone, then `uv tool install . --force`.
- **Uninstall:** `uv tool uninstall claude-usage-optimizer`. Data stays in `~/.model-manager/`.

Then offer to run `usage-optimizer --open` for them and read out their top findings.
