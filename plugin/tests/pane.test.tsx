import { expect, test } from 'claude-code/testing'
import { meter, sparkText, statusLine, tok, usd } from '../hooks/pane'
import { POLICY } from './fixture-policy'

const PANE = {
  plugin: 'model-manager',
  component: 'Pane',
  requestId: 'model-manager',
  props: {
    title: 'Model Manager',
    isFocused: false,
    bodyColumns: 48,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

const SUMMARY = {
  v: 1, generated_at: new Date().toISOString(), mode: 'observe', disabled: false,
  today: { date: '2026-11-04', requests: 120, est_cost_usd: 14.2 },
  week: {
    requests: 2400, est_cost_usd: 310.5, cache_read_share: 0.54, median_context: 90000,
    savings: { routed_requests: 12, est_cost_usd: 3, baseline_cost_usd: 5, est_savings_usd: 2, baseline_model: 'opus' },
    model_mix: [
      { model: 'claude-opus-5-5', request_share: 0.7, cost_share: 0.9 },
      { model: 'claude-haiku-5-5', request_share: 0.3, cost_share: 0.1 },
    ],
  },
  daily: [10, 20, null, 40, 50, 60, 14.2].map((c, i) => ({ date: `2026-10-${29 + i}`, est_cost_usd: c, requests: 10 })),
  quota: null, last_route: null,
}

// stands in for the engine: files under ~/.model-manager, usage and the calls the mod makes
function engine(on: any, files: Record<string, string>, livePids: number[] = []) {
  on('process.run', (_: any, e: any) => ({ value: { exitCode: livePids.includes(Number(e.argv?.[2])) ? 0 : 1, stdout: '', stderr: '' } }))
  on('env.get', () => ({ value: '/home/t' }))
  on('session.id', () => ({ value: 'sess-0001' }))
  on('session.cwd', () => ({ value: '/work/proj' }))
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('session.version', () => ({ value: '2.1.309' }))
  on('session.usage', () => ({ value: {
    startedAt: new Date().toISOString(), cost: { usd: 3.4 },
    context: { tokens: 84000, window: 1000000, percent: 8.4 },
    rateLimits: [{ kind: 'five_hour', percentUsed: 37 }, { kind: 'seven_day', percentUsed: 54 }],
  } }))
  on('fs.read', (_: any, e: any) => {
    const name = String(e.path).split('/').pop() as string
    return name in files ? { value: files[name] } : { deny: 'ENOENT' }
  })
  on('fs.exists', () => ({ value: false }))
  on('fs.write', () => ({ value: undefined }))
  on('clock.every', () => ({ value: undefined }))
  on('command.register', () => ({ value: { command: 'mm' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.status', () => ({ value: undefined }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
}

const BASE_FILES = { 'config.json': JSON.stringify({ mode: 'observe' }), 'policy.json': JSON.stringify(POLICY) }

test('the pane shows the 7-day summary when summary.json exists, on every surface', async ($, on) => {
  engine(on, { ...BASE_FILES, 'summary.json': JSON.stringify(SUMMARY) })
  await $.session.start({ cwd: '/work/proj', surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /2,400 requests/ }))?.text).toBe('$311 · 2,400 requests · today $14.20')
    expect((await ui.find({ type: 'Text', text: /Opus 70%/ }))?.text).toContain('Haiku 30%')
    expect((await ui.find({ type: 'Text', text: /^5-hour/ }))?.text).toContain('37%')
    expect((await ui.find({ type: 'Text', text: /^Context/ }))?.text).toContain('84K')
    expect(!!(await ui.find({ type: 'Svg' }))).toBe(surface !== 'terminal')
    if (surface === 'terminal') expect((await ui.find({ type: 'Text', text: /^[▁-█ ]{7}$/ }))?.text).toBe('▂▃ ▆▇█▂')
    await ui.unmount()
  }
})

test('without summary.json the pane says so and still shows the session', async ($, on) => {
  engine(on, BASE_FILES)
  await $.session.start({ cwd: '/work/proj', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /No summary yet/ }))?.text).toBe('No summary yet. Run model-manager ingest.')
  expect((await ui.find({ type: 'Text', text: /^\$3\.40/ }))?.text).toBe('$3.40 · 0 requests')
  expect((await ui.find({ type: 'Text', text: /Last 7 days/ }))?.text).toBe('Last 7 days')
  await ui.unmount()
})

test('/mm pane opens the pane', async ($, on) => {
  engine(on, BASE_FILES)
  await $.session.start({ cwd: '/work/proj', surface: 'terminal', isInteractive: true })
  const r: any = await $.command.run({ command: 'mm', args: 'pane' })
  expect(r.text).toBe('Model Manager pane opened.')
})

const HANDLE = JSON.stringify({ url: 'http://127.0.0.1:8765/?token=t', port: 8765, pid: 4242 })
async function dashButton($: any, on: any, livePids: number[]) {
  engine(on, { ...BASE_FILES, 'dashboard.json': HANDLE }, livePids)
  await $.session.start({ cwd: '/work/proj', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  const open = !!(await ui.find({ key: 'dash', text: 'Open dashboard' }))
  const dead = !!(await ui.find({ key: 'dash', text: 'Dashboard not running' }))
  await ui.unmount()
  return { open, dead }
}

test('the dashboard button opens a dashboard whose pid runs', async ($, on) => {
  expect(await dashButton($, on, [4242])).toEqual({ open: true, dead: false })
})

test('a dashboard.json left by a killed server reads as not running', async ($, on) => {
  expect(await dashButton($, on, [])).toEqual({ open: false, dead: true })
})

test('formatters and the status line', () => {
  expect(usd(null)).toBe('—')
  expect(usd(3.456)).toBe('$3.46')
  expect(usd(1264.2)).toBe('$1,264')
  expect(tok(84213)).toBe('84K')
  expect(tok(1_250_000)).toBe('1.3M')
  expect(meter(37)).toBe('████░░░░░░')
  expect(meter(null)).toBe('░░░░░░░░░░')
  expect(sparkText([0, 5, null, 10])).toBe('▁▄ █')
  const v: any = {
    mode: 'observe', disabled: false, task: null, summary: null, dashboardUrl: null,
    route: { model: 'sonnet', effort: 'medium', applied: true, reason: '', category: 'edit', complexity: 2 },
    session: { requests: 3, costUsd: 1, contextTokens: 84000, contextWindow: 1e6 },
    quota: { fiveHour: 37, sevenDay: 54, fiveHourResets: null },
  }
  expect(statusLine(v)).toBe('mm sonnet·med · ctx 84K · 5h 37%')
  expect(statusLine({ ...v, disabled: true })).toBe('mm off')
  expect(statusLine({ ...v, mode: 'off' })).toBe(undefined)
})
