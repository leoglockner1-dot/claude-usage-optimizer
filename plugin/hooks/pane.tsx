// Stats pane: draws the View the mod keeps in $.state. No I/O here; handlers come from register.ts.

import type { View } from '../types'

export const PANE_ID = 'model-manager'
const CONTEXT_WARN = 200_000
const STALE_MS = 15 * 60_000

export type PaneActions = {
  setMode: (mode: string) => void
  grade: (kind: 'good' | 'bad') => void
  pin: (model: string) => void
  openDashboard: () => void
}

const MODES = ['observe', 'auto', 'balanced', 'quality', 'cost', 'manual', 'off']
const FAMILY: Record<string, string> = { haiku: 'Haiku', sonnet: 'Sonnet', opus: 'Opus', fable: 'Fable' }

export function usd(v: number | null | undefined): string {
  if (v == null) return '—'
  return v >= 100 ? `$${Math.round(v).toLocaleString('en-US')}` : `$${v.toFixed(2)}`
}

export function tok(v: number | null | undefined): string {
  if (v == null) return '—'
  return v >= 1e6 ? `${+(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}K` : String(v)
}

function pct(v: number | null | undefined): string {
  return v == null ? '—' : `${Math.round(v)}%`
}

function family(model: string): string {
  const k = Object.keys(FAMILY).find(f => model.includes(f))
  return k ? FAMILY[k] : model
}

// ten cells, for surfaces without Svg and so the numbers line up everywhere
export function meter(percent: number | null | undefined): string {
  if (percent == null) return '░░░░░░░░░░'
  const n = Math.max(0, Math.min(10, Math.round(percent / 10)))
  return '█'.repeat(n) + '░'.repeat(10 - n)
}

const BLOCKS = '▁▂▃▄▅▆▇█'
export function sparkText(values: (number | null)[]): string {
  const max = Math.max(0, ...values.map(v => v ?? 0))
  return values.map(v => (v == null ? ' ' : BLOCKS[max ? Math.min(7, Math.floor((v / max) * 7.999)) : 0])).join('')
}

// daily cost bars; days with no data stay empty rather than drawn as zero
function sparkSvg(values: (number | null)[]): string {
  const w = 18, gap = 4, h = 36
  const max = Math.max(0, ...values.map(v => v ?? 0))
  const bars = values.map((v, i) => {
    const x = i * (w + gap)
    if (v == null || !max) return `<rect x="${x}" y="${h - 1}" width="${w}" height="1" fill="#898781"/>`
    const bh = Math.max(2, (v / max) * (h - 2))
    return `<rect x="${x}" y="${h - bh}" width="${w}" height="${bh}" rx="2" fill="#3987e5"/>`
  })
  const width = values.length * (w + gap) - gap
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${h}" width="${width}" height="${h}">${bars.join('')}</svg>`
}

function ago(iso: string, now: number): string {
  const m = Math.round((now - Date.parse(iso)) / 60_000)
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`
}

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
}

// one line for the status bar: `mm opus·med · ctx 84K · 5h 37%`
export function statusLine(v: View): string | undefined {
  if (v.disabled) return 'mm off'
  if (v.mode === 'off') return undefined
  const parts = [v.route ? `mm ${v.route.model}·${v.route.effort.slice(0, 3)}${v.route.applied ? '' : ' (obs)'}` : `mm ${v.mode}`]
  if (v.session.contextTokens != null) parts.push(`ctx ${tok(v.session.contextTokens)}`)
  if (v.quota.fiveHour != null) parts.push(`5h ${pct(v.quota.fiveHour)}`)
  return parts.join(' · ')
}

export function drawPane($: any, e: any, v: View, act: PaneActions, now: number) {
  const ui = $.ui.resolve(e)
  const { Box, Text, Button } = ui
  // the terminal has no Svg and mobile no Select (Elements in the engine types)
  const Svg = e.surface === 'terminal' ? null : ui.Svg
  const Select = e.surface === 'mobile' ? null : ui.Select
  const s = v.summary
  const head = (t: string) => <Text weight="semibold">{t}</Text>
  const ctx = v.session.contextTokens

  const modeText = v.disabled ? 'OFF (kill switch)' : v.mode
  const route = v.route
  const daily = s?.daily.map(d => d.est_cost_usd) ?? []
  const mix = (s?.week.model_mix ?? []).filter(m => m.request_share >= 0.005)
    .map(m => `${family(m.model)} ${Math.round(m.request_share * 100)}%`).join(' · ')
  const stale = s ? now - Date.parse(s.generated_at) > STALE_MS : true

  return (
    <Box flexDirection="column" gap={1}>
      <Box flexDirection="column">
        <Text>Mode <Text weight="semibold">{modeText}</Text></Text>
        {route
          ? <Text>Last turn {route.model}·{route.effort}{route.category ? ` · ${route.category} c${route.complexity}` : ''}
              {route.applied ? '' : <Text dimColor> (observe: not applied)</Text>}</Text>
          : <Text dimColor>No routing decision in this session yet.</Text>}
        {route && <Text dimColor>{route.reason}</Text>}
        {v.task?.pinned && <Text>Task pinned to {v.task.pinned}</Text>}
      </Box>

      <Box flexDirection="column">
        {head('This session')}
        <Text tabular>{usd(v.session.costUsd)} · {v.session.requests} requests</Text>
        <Text tabular>Context {tok(ctx)}{v.session.contextWindow ? ` of ${tok(v.session.contextWindow)}` : ''}</Text>
        {ctx != null && ctx > CONTEXT_WARN &&
          <Text color="warning">Over 200K: every request re-reads it. /compact or a new session is cheaper.</Text>}
      </Box>

      <Box flexDirection="column">
        {head('Plan usage')}
        <Text mono tabular>5-hour {meter(v.quota.fiveHour)} {pct(v.quota.fiveHour)}
          {v.quota.fiveHourResets ? <Text dimColor> resets {clock(v.quota.fiveHourResets)}</Text> : ''}</Text>
        <Text mono tabular>7-day  {meter(v.quota.sevenDay)} {pct(v.quota.sevenDay)}</Text>
      </Box>

      <Box flexDirection="column">
        {head('Last 7 days')}
        {s
          ? <Box flexDirection="column">
              <Text tabular>{usd(s.week.est_cost_usd)} · {s.week.requests.toLocaleString('en-US')} requests · today {usd(s.today.est_cost_usd)}</Text>
              {Svg
                ? <Svg source={sparkSvg(daily)} alt={`Cost per day, last 7 days: ${daily.map(usd).join(', ')}`} />
                : <Text mono>{sparkText(daily)}</Text>}
              <Text dimColor>Cost per day (API-equivalent estimate)</Text>
              {mix && <Text>{mix}</Text>}
              {s.week.savings && s.week.savings.routed_requests > 0 &&
                <Text>Saved {usd(s.week.savings.est_savings_usd)} on {s.week.savings.routed_requests} routed requests</Text>}
              <Text dimColor>Updated {ago(s.generated_at, now)}{stale ? ' · run model-manager ingest or open the dashboard to refresh' : ''}</Text>
            </Box>
          : <Text dimColor>No summary yet. Run model-manager ingest.</Text>}
      </Box>

      <Box flexDirection="column" gap={1}>
        {Select &&
          <Select key="mode" label="Mode" value={v.mode} options={MODES.map(m => ({ value: m, label: m }))}
            onSelect={(m: string) => act.setMode(m)} />}
        {v.task &&
          <Box flexDirection="row" gap={1}>
            <Button key="good" label="Good" onPress={() => act.grade('good')} />
            <Button key="bad" label="Bad" onPress={() => act.grade('bad')} />
            {['haiku', 'sonnet', 'opus'].map(m =>
              <Button key={`pin-${m}`} label={FAMILY[m]} onPress={() => act.pin(m)} />)}
          </Box>}
        {v.task && <Text dimColor>Grade the current task, or pin it to a model.</Text>}
        <Button key="dash" label={v.dashboardUrl ? 'Open dashboard' : 'Dashboard not running'} onPress={() => act.openDashboard()} />
      </Box>
    </Box>
  )
}
