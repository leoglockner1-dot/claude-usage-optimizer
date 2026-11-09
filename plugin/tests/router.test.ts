import { expect, test, mock } from 'claude-code/testing'
import { classify, parseOverride } from '../hooks/classify'
import { decide, requestCost } from '../hooks/route'
import { POLICY } from './fixture-policy'

const CFG: any = {
  mode: 'auto', auto_models: ['haiku', 'sonnet', 'opus'], ceiling: 'fable', manual_model: 'sonnet', manual_effort: 'medium',
  haiku_main_max_context: 80000, min_switch_saving: 0.15, quota_guard: { five_hour: 80, seven_day: 90 },
  floors: { visual_design: 'opus:medium' },
}
const base = (over: any = {}) => ({
  cls: classify('rename the variable foo to bar in utils.ts', '/tmp', false), policy: POLICY, cfg: CFG,
  currentModel: 'opus', contextTokens: 20000, messageCount: 4, now: 0, marks: {}, escalationLevel: 0, quota: {}, ...over,
})

// ---------- classifier ----------

test('classifies a rename as a simple edit', () => {
  const c = classify('rename the variable foo to bar in utils.ts', '/tmp', false)
  expect(c.category).toBe('edit')
  expect(c.complexity <= 2).toBe(true)
})

test('classifies a Luau datastore bug as debugging', () => {
  const c = classify('My ModuleScript throws "attempt to index nil value" when the DataStore loads. Why does it crash?', '/x/ROLL A ROLLER', false)
  expect(c.category).toBe('debug')
})

test('classifies German UI request as Roblox UI', () => {
  const c = classify('Baue ein Shop-Menü mit ScreenGui, UIListLayout und TweenService Animationen', '/x', false)
  expect(c.category).toBe('roblox_ui')
})

test('design work and redesigns are visual_design, system design stays arch', () => {
  for (const p of ['Redesign the dashboard, it looks too generic', 'design a shop UI with a cleaner layout',
    'Mach das Menü schöner, neue Optik', 'make it look better', 'build a mockup for the landing page'])
    expect(classify(p, '/x', false).category).toBe('visual_design')
  expect(classify('system design for the save architecture, trade-offs?', '/x', false).category).toBe('arch')
  expect(classify('balance the game design economy and drop rates', '/x', false).category).toBe('game_design')
})

test('visual_design is floored at opus·medium, even in cost mode and under the quota guard', () => {
  const cls = classify('redesign the settings page, cleaner layout', '/x', false)
  const policy = { ...POLICY, table: { ...POLICY.table, visual_design: { low: { model: 'sonnet', effort: 'low' }, mid: { model: 'sonnet', effort: 'low' }, high: { model: 'sonnet', effort: 'low' } } } }
  for (const mode of ['auto', 'cost']) {
    const d = decide(base({ cls, policy, cfg: { ...CFG, mode }, currentModel: 'sonnet', quota: { fiveHour: 95 } }))
    expect([d.model, d.effort]).toEqual(['opus', 'medium'])
  }
  const d = decide(base({ cls, cfg: { ...CFG, auto_models: ['haiku', 'sonnet'] }, currentModel: 'sonnet' }))
  expect(d.model).toBe('opus')
  const o = decide(base({ cls, currentModel: 'opus', override: { model: 'sonnet' } }))
  expect(o.model).toBe('sonnet')
})

test('architecture prompts are at least complexity 3', () => {
  const c = classify('How should we structure the architecture of the save system?', '/x', false)
  expect(c.category).toBe('arch')
  expect(c.complexity >= 3).toBe(true)
})

test('follow-ups and corrections are detected only inside a task', () => {
  expect(classify('ok mach weiter', '/x', true).followUp).toBe(true)
  expect(classify('ok mach weiter', '/x', false).followUp).toBe(false)
  expect(classify('geht immer noch nicht, same error', '/x', true).correction).toBe(true)
})

test('high-risk words set risk high', () => {
  expect(classify('rotate the auth token and password storage', '/x', false).risk).toBe('high')
})

test('override prefix is parsed and stripped', () => {
  const o = parseOverride('[sonnet:high] fix the bug')
  expect(o.model).toBe('sonnet')
  expect(o.effort).toBe('high')
  expect(o.text).toBe('fix the bug')
  expect(parseOverride('haiku: write a poem').model).toBe(undefined)
})

// ---------- router ----------

test('simple edit on a fresh session routes to haiku', () => {
  const d = decide(base({ currentModel: 'opus', contextTokens: 0 }))
  expect(d.model).toBe('haiku')
})

test('haiku is refused for a large main context', () => {
  const d = decide(base({ currentModel: 'sonnet', contextTokens: 150000, marks: { sonnet: { at: 0, messageCount: 4 } } }))
  expect(d.model === 'haiku').toBe(false)
})

test('escalation moves up the ladder', () => {
  const d0 = decide(base({ currentModel: 'haiku', contextTokens: 1000 }))
  const d1 = decide(base({ currentModel: 'haiku', contextTokens: 1000, escalationLevel: 2 }))
  expect(POLICY.tiers[d1.model] > POLICY.tiers[d0.model] || d1.effort !== d0.effort).toBe(true)
})

test('auto mode never picks fable without override or escalation', () => {
  const cls = classify('Redesign the entire architecture of the game from scratch, complex multi-file migration', '/x', false)
  const d = decide(base({ cls, currentModel: 'opus' }))
  expect(d.model === 'fable').toBe(false)
})

test('override wins over policy', () => {
  const d = decide(base({ override: { model: 'opus', effort: 'high' } }))
  expect(d.model).toBe('opus')
  expect(d.effort).toBe('high')
})

test('high risk forces at least sonnet with high effort', () => {
  const cls = classify('rename the password variable', '/x', false)
  const d = decide(base({ cls, currentModel: 'opus', contextTokens: 0 }))
  expect(POLICY.tiers[d.model] >= 2).toBe(true)
  expect(d.effort).toBe('high')
})

test('cache rewrite is priced: cold cache costs more than warm', () => {
  expect(requestCost(POLICY, 'sonnet', 100000, 0) > requestCost(POLICY, 'sonnet', 100000, 1)).toBe(true)
})

// ---------- hooks end to end ----------

function stubSession(on: any, files: Record<string, string>, written: string[]) {
  mock.env(on, { HOME: '/home/t' })
  mock.clock(on)
  on('session.id', () => ({ value: 'sess-1234567' }))
  on('session.cwd', () => ({ value: '/work' }))
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('session.version', () => ({ value: '2.1.309' }))
  on('session.usage', () => ({ value: { context: { tokens: 1000, window: 1000000 }, rateLimits: [{ kind: 'five_hour', percentUsed: 12 }], cost: { usd: 0 } } }))
  on('fs.read', (_$: any, e: any) => (e.path in files ? { value: files[e.path] } : { deny: 'ENOENT' }))
  on('fs.exists', () => ({ value: false }))
  on('fs.write', (_$: any, e: any) => { written.push(e.text ?? e.content ?? JSON.stringify(e)); return { value: undefined } })
  on('command.register', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('session.start', () => ({ cwd: '/work' }))
  on('prompt.submit', (_$: any, e: any) => e)
  on('turn.start', (_$: any, e: any) => e)
  on('turn.complete', () => ({ text: '' }))
}

async function step($: any, turnId: string) {
  const stream = $.turn.step({ turnId, index: 0, model: 'claude-opus-5-5', effort: 'medium', messageCount: 2 })
  let s = await stream.next()
  while (s.done !== true) s = await stream.next()
  return s.value
}

test('auto mode sends a simple edit to haiku and records it', async ($, on) => {
  const written: string[] = []
  const sentModels: string[] = []
  stubSession(on, {
    '/home/t/.model-manager/config.json': JSON.stringify({ mode: 'auto' }),
    '/home/t/.model-manager/policy.json': JSON.stringify(POLICY),
  }, written)
  on('turn.step', async function* (_$: any, e: any) {
    sentModels.push(e.model)
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn',
             usage: { model: e.model, input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 0, cache_creation_input_tokens: 100 } }
  })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await $.prompt.submit({ text: 'rename the variable foo to bar in utils.ts', origin: 'composer' })
  await $.turn.start({ turnId: 't1' })
  await step($, 't1')
  await $.turn.complete({ turnId: 't1', durationMs: 10, isAborted: false, answer: 'done' })
  expect(sentModels[0]).toBe('claude-haiku-5-5')
  const all = written.join('\n')
  expect(all.includes('"type":"route"')).toBe(true)
  expect(all.includes('"type":"request"')).toBe(true)
  expect(all.includes('"type":"task_open"')).toBe(true)
})

test('observe mode records but does not change the model', async ($, on) => {
  const written: string[] = []
  const sentModels: string[] = []
  stubSession(on, {
    '/home/t/.model-manager/config.json': JSON.stringify({ mode: 'observe' }),
    '/home/t/.model-manager/policy.json': JSON.stringify(POLICY),
  }, written)
  on('turn.step', async function* (_$: any, e: any) {
    sentModels.push(e.model)
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await $.prompt.submit({ text: 'rename the variable foo to bar', origin: 'composer' })
  await $.turn.start({ turnId: 't1' })
  await step($, 't1')
  await $.turn.complete({ turnId: 't1', durationMs: 10, isAborted: false, answer: 'done' })
  expect(sentModels[0]).toBe('claude-opus-5-5')
  expect(written.join('\n').includes('"applied":false')).toBe(true)
})

test('an explicit override applies even in observe mode', async ($, on) => {
  const written: string[] = []
  const sentModels: string[] = []
  stubSession(on, {
    '/home/t/.model-manager/config.json': JSON.stringify({ mode: 'observe' }),
    '/home/t/.model-manager/policy.json': JSON.stringify(POLICY),
  }, written)
  on('turn.step', async function* (_$: any, e: any) {
    sentModels.push(e.model)
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const sub = await $.prompt.submit({ text: '[haiku] say ok', origin: 'composer' })
  await $.turn.start({ turnId: 't1' })
  await step($, 't1')
  expect(sentModels[0]).toBe('claude-haiku-5-5')
  expect(sub.text).toBe('say ok')
})

test('a shell command echo is neither a task nor routed by the open override', async ($, on) => {
  const written: string[] = []
  const sentModels: string[] = []
  stubSession(on, {
    '/home/t/.model-manager/config.json': JSON.stringify({ mode: 'observe' }),
    '/home/t/.model-manager/policy.json': JSON.stringify(POLICY),
  }, written)
  on('turn.step', async function* (_$: any, e: any) {
    sentModels.push(e.model)
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await $.prompt.submit({ text: '[haiku] say ok', origin: 'composer' })
  await $.turn.start({ turnId: 't1' })
  await step($, 't1')
  await $.turn.complete({ turnId: 't1', durationMs: 10, isAborted: false, answer: 'ok' })
  await $.prompt.submit({ text: '<bash-input>model-manager last</bash-input><bash-stdout>x</bash-stdout>', origin: 'composer' })
  await $.turn.start({ turnId: 't2' })
  await step($, 't2')
  await $.turn.complete({ turnId: 't2', durationMs: 10, isAborted: false, answer: '?' })
  expect(sentModels[0]).toBe('claude-haiku-5-5')
  expect(sentModels[1]).toBe('claude-opus-5-5')
  const opens = written.join('\n').split('"type":"task_open"').length - 1
  expect(opens).toBe(1)
})

test('failing tests twice escalate the next turn', async ($, on) => {
  const written: string[] = []
  const sentModels: string[] = []
  stubSession(on, {
    '/home/t/.model-manager/config.json': JSON.stringify({ mode: 'auto' }),
    '/home/t/.model-manager/policy.json': JSON.stringify(POLICY),
  }, written)
  on('turn.step', async function* (_$: any, e: any) {
    sentModels.push(e.model)
    return { turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  on('tool.call', () => ({ result: 'Exit code 1\n2 failed', isError: true }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await $.prompt.submit({ text: 'write a test for the parser and make it pass', origin: 'composer' })
  for (const id of ['t1', 't2']) {
    await $.turn.start({ turnId: id })
    await step($, id)
    await $.tool.call({ tool: 'Bash', command: 'pytest -q' })
    await $.turn.complete({ turnId: id, durationMs: 10, isAborted: false, answer: 'tried' })
  }
  await $.prompt.submit({ text: 'still failing', origin: 'composer' })
  await $.turn.start({ turnId: 't3' })
  await step($, 't3')
  const all = written.join('\n')
  expect(all.includes('"type":"escalation"')).toBe(true)
  expect(sentModels[2] !== sentModels[0]).toBe(true)
})

test('/mm off is a kill switch', async ($, on) => {
  const written: string[] = []
  stubSession(on, { '/home/t/.model-manager/policy.json': JSON.stringify(POLICY) }, written)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  const r = await $.command.run({ command: 'mm', args: 'off' })
  expect(String(r.text).includes('off')).toBe(true)
  expect(written.some(w => w.includes('20'))).toBe(true)
})
