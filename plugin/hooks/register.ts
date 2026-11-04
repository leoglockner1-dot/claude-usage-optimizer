// AI Model Manager – Claude Code mod.
// Classifies each prompt locally, chooses model + effort per turn (turn.step), routes subagents
// (agent.spawn), watches tests/builds for escalation, and spools measurements for the Python core.
// Fails open: any hook error is skipped by Claude Code and the session keeps its own model.

import { classify, parseOverride, type Classification } from './classify'
import { aliasOf, decide, type CacheMark, type Config, type Decision, type Policy } from './route'

type Task = {
  id: string
  cls: Classification
  override?: { model?: string; effort?: string }
  level: number // escalation steps taken
  escalations: number
  turns: number
  window: { signals: string[]; score: number }[] // last two main turns
  verified: boolean // last test run passed
  lastFailedTests: boolean
  feedback?: 'good' | 'bad'
  pendingNote?: string
  lastError?: string
  exploration: boolean
}

type TurnStats = {
  turnId: string
  started: number
  toolCalls: number
  toolErrors: number
  testsRun: number
  testsFailed: number
  testsPassed: number
  buildsFailed: number
  errorText?: string
}

const DEFAULT_CFG: Config & Record<string, any> = {
  mode: 'observe',
  auto_models: ['haiku', 'sonnet', 'opus'],
  ceiling: 'fable',
  manual_model: 'sonnet',
  manual_effort: 'medium',
  route_subagents: true,
  haiku_main_max_context: 80000,
  min_switch_saving: 0.15,
  exploration: 0.05,
  escalation: { enabled: true, threshold: 1.5, max_auto: 2 },
  quota_guard: { five_hour: 80, seven_day: 90 },
  store_prompts: 'excerpt',
  test_patterns: ['\\bpytest\\b', '\\bnpm (run )?test\\b', '\\bcargo test\\b', '\\bjest\\b', '\\bvitest\\b'],
  build_patterns: ['\\btsc\\b', '\\bnpm run build\\b', '\\bcargo build\\b', '\\bluau-analyze\\b', '\\bselene\\b'],
}
const ROUTING_MODES = new Set(['auto', 'balanced', 'quality', 'cost', 'manual'])
const WEIGHT = { strong: 1, medium: 0.5, weak: 0.25 }

// ---------- module state (one session per module instance) ----------
let home = ''
let cfg: Config & Record<string, any> = DEFAULT_CFG
let policy: Policy | null = null
let disabled = false
let sessionId = ''
let cwd = ''
let task: Task | null = null
let turn: TurnStats | null = null
let turnDecision: { turnId: string; d: Decision } | null = null
let lastDecision: Decision | null = null
let marks: Record<string, CacheMark> = {}
let quota: { fiveHour?: number; sevenDay?: number } = {}
let buffer: any[] = []
let testRes: RegExp[] = []
let shellTurn = false // the next turn answers a `!` shell command typed by the user
const SHELL_ECHO = /^\s*<(bash-input|bash-stdout|bash-stderr|local-command-stdout)>/
let buildRes: RegExp[] = []

function ts(): string { return new Date().toISOString() }
function rid(): string { return crypto.randomUUID().slice(0, 12) }
function routing(): boolean { return !disabled && ROUTING_MODES.has(cfg.mode) && policy !== null }
function recording(): boolean { return !disabled && cfg.mode !== 'off' }
function emit(ev: Record<string, any>) { if (recording()) buffer.push({ ts: ts(), session_id: sessionId, ...ev }) }

async function flush($: any) {
  if (!buffer.length || !home) return
  const events = buffer
  buffer = []
  try {
    await $.fs.write(`${home}/.model-manager/spool/${Date.now()}-${rid()}.json`, JSON.stringify({ v: 1, events }))
  } catch {
    // spool unavailable (core not initialised): drop silently rather than disturb the session
  }
}

async function readJson($: any, path: string): Promise<any> {
  try { return JSON.parse(await $.fs.read(path)) } catch { return null }
}

async function reload($: any) {
  const c = await readJson($, `${home}/.model-manager/config.json`)
  cfg = { ...DEFAULT_CFG, ...(c ?? {}), escalation: { ...DEFAULT_CFG.escalation, ...(c?.escalation ?? {}) } }
  policy = await readJson($, `${home}/.model-manager/policy.json`)
  try { disabled = await $.fs.exists(`${home}/.model-manager/DISABLED`) } catch { disabled = false }
  testRes = (cfg.test_patterns as string[]).map(p => new RegExp(p, 'i'))
  buildRes = (cfg.build_patterns as string[]).map(p => new RegExp(p, 'i'))
}

async function excerpt(text: string): Promise<{ excerpt: string | null; hash: string }> {
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  const hash = Array.from(new Uint8Array(bytes).slice(0, 8), b => b.toString(16).padStart(2, '0')).join('')
  return { excerpt: cfg.store_prompts === 'hash' ? null : text.replace(/\s+/g, ' ').slice(0, 200), hash }
}

function closeTask(reason: string) {
  if (!task) return
  let outcome = 'unverified_success'
  let source = 'heuristic'
  const last = task.window[task.window.length - 1]
  if (task.feedback === 'bad') { outcome = 'failure'; source = 'user' }
  else if (task.feedback === 'good') { outcome = 'verified_success'; source = 'user' }
  else if (task.verified && !task.lastFailedTests) { outcome = 'verified_success'; source = 'tests' }
  else if (last && last.score >= WEIGHT.strong) { outcome = 'failure'; source = 'heuristic' }
  else if (task.turns === 0 || reason === 'aborted') { outcome = 'unknown' }
  emit({ type: 'task_close', task_id: task.id, outcome, outcome_source: source, turns: task.turns, escalations: task.escalations, reason })
  task = null
}

function quotaFrom(usage: any) {
  for (const r of usage?.rateLimits ?? []) {
    const k = String(r.kind ?? '')
    if (/five|5h|session/i.test(k)) quota.fiveHour = r.percentUsed
    else if (/seven|7d|week/i.test(k)) quota.sevenDay = r.percentUsed
  }
}

function status(): string {
  const pol = policy ? `policy v${policy.version}` : 'no policy (run: model-manager init)'
  const t = task ? `task ${task.cls.category}/c${task.cls.complexity} risk ${task.cls.risk}${task.level ? ` · escalated +${task.level}` : ''}` : 'no open task'
  const d = lastDecision ? `last route ${lastDecision.model}·${lastDecision.effort}${routing() ? '' : ' (not applied)'} — ${lastDecision.reason}` : 'no route yet'
  const q = quota.fiveHour !== undefined ? ` · quota 5h ${quota.fiveHour}% 7d ${quota.sevenDay ?? '?'}%` : ''
  return `Model Manager · mode ${disabled ? 'OFF (kill switch)' : cfg.mode} · ${pol}${q}\n${t}\n${d}`
}

async function writeConfig($: any, patch: Record<string, any>) {
  const c = (await readJson($, `${home}/.model-manager/config.json`)) ?? {}
  await $.fs.write(`${home}/.model-manager/config.json`, JSON.stringify({ ...c, ...patch }, null, 2) + '\n')
  await reload($)
}

function maybeEscalate($: any, where: string) {
  if (!task || !cfg.escalation?.enabled || !policy) return
  const score = task.window.reduce((s, w) => s + w.score, 0)
  if (score < cfg.escalation.threshold) return
  if (task.escalations >= cfg.escalation.max_auto) {
    if (routing()) $.ui.log(`struggling on this task (score ${score}); auto-escalation limit reached — /mm use opus to go further`)
    return
  }
  const from = lastDecision
  task.level++
  task.escalations++
  const signals = task.window.flatMap(w => w.signals)
  task.window = []
  emit({ type: 'escalation', task_id: task.id, turn_id: turnDecision?.turnId ?? null, from_model: from?.model ?? null,
         from_effort: from?.effort ?? null, to_model: null, to_effort: null, signals, where })
  if (routing()) {
    task.pendingNote = `Model Manager: this task was escalated to a stronger model after: ${signals.join(', ')}. ` +
      'Re-check the earlier approach before repeating it.'
    $.ui.log(`escalating next turn (+${task.level}) after ${signals.join(', ')}`)
  }
}

export function register(on: any) {
  on('session.start', async ($: any, e: any, next: any) => {
    home = (await $.env.get('HOME')) ?? ''
    sessionId = await $.session.id()
    cwd = await $.session.cwd()
    await reload($)
    let model = ''
    let version = ''
    try { const m: any = await $.session.model(); model = typeof m === 'string' ? m : String(m?.id ?? m?.model ?? '') } catch {}
    try { const v: any = await $.session.version(); version = typeof v === 'string' ? v : String(v?.version ?? '') } catch {}
    emit({ type: 'session', cwd, cc_version: version, model, project: cwd.split('/').pop() })
    await flush($)
    $.clock.every(30_000, async () => { await flush($) })
    try {
      await $.command.register({ name: 'mm', description: 'AI Model Manager: status, mode, good/bad, use <model>, why, off/on', argumentHint: '[status|mode <m>|good|bad|use <model[:effort]>|why|off|on]', immediate: true })
    } catch {}
    return next(e)
  })

  on('session.end', async ($: any, e: any, next: any) => {
    closeTask('session_end')
    emit({ type: 'session_end' })
    await flush($)
    return next(e)
  })

  on('prompt.submit', async ($: any, e: any, next: any) => {
    await reload($)
    if (disabled || cfg.mode === 'off') return next(e)
    // a `!` shell command arrives as <bash-input>…: not a task, not classified, never routed
    if (SHELL_ECHO.test(String(e.text ?? ''))) { shellTurn = true; return next(e) }
    const typed = !/notif|peer|agent|channel|hook|system|task|schedul/i.test(String(e.origin ?? ''))
    const ov = parseOverride(String(e.text ?? ''))
    const text = ov.text
    const cls = classify(text, cwd, task !== null)
    const sameTask = task !== null && (!typed || cls.followUp || cls.correction || (cls.positive && text.length < 120))
    if (sameTask && task) {
      if (cls.correction) task.window.push({ signals: ['user_correction'], score: WEIGHT.medium })
      if (ov.model) task.override = { model: ov.model, effort: ov.effort }
      maybeEscalate($, 'prompt')
    } else {
      closeTask('new_task')
      const ex = await excerpt(text)
      task = {
        id: `${sessionId.slice(0, 8)}-${rid()}`, cls, override: ov.model ? { model: ov.model, effort: ov.effort } : undefined,
        level: 0, escalations: 0, turns: 0, window: [], verified: false, lastFailedTests: false, exploration: false,
      }
      emit({ type: 'task_open', task_id: task.id, prompt_excerpt: ex.excerpt, prompt_hash: ex.hash, category: cls.category,
             complexity: cls.complexity, risk: cls.risk, flags: cls.flags, confidence: cls.confidence,
             override: ov.model ? `${ov.model}:${ov.effort ?? ''}` : null, origin: e.origin ?? null, cwd })
    }
    let out = ov.model ? { ...e, text } : e
    if (task?.pendingNote && routing()) {
      out = { ...out, context: [...(out.context ?? []), task.pendingNote] }
      task.pendingNote = undefined
    }
    return next(out)
  })

  on('turn.step', async function* ($: any, e: any, next: any) {
    if (disabled || cfg.mode === 'off' || !policy) return yield* next(e)
    const sub = e.agentId !== undefined && e.agentId !== null
    let sent = e
    if (!sub && task) {
      if (!turnDecision || turnDecision.turnId !== e.turnId) {
        let ctx = 0
        try { const u = await $.session.usage(); ctx = u?.context?.tokens ?? 0; quotaFrom(u) } catch {}
        const d: Decision = shellTurn
          ? { model: aliasOf(e.model, policy), effort: e.effort ?? 'medium', apply: false, reason: 'shell command echo: not routed', candidates: [] }
          : decide({
          cls: task.cls, policy, cfg, currentModel: aliasOf(e.model, policy), contextTokens: ctx,
          messageCount: e.messageCount ?? 0, now: Date.now(), marks, escalationLevel: task.level,
          override: task.override, quota,
        })
        // observe mode never routes on its own, but an explicit !model override is the user's instruction
        if (!shellTurn) d.apply = routing() || (!!task.override?.model && !disabled && cfg.mode !== 'off')
        shellTurn = false
        turnDecision = { turnId: e.turnId, d }
        lastDecision = d
        emit({ type: 'route', task_id: task.id, turn_id: e.turnId, mode: cfg.mode, policy_version: policy.version,
               session_model: e.model, chosen_model: d.model, chosen_effort: d.effort, applied: d.apply,
               reason: d.reason, candidates: d.candidates })
        if (d.apply && aliasOf(e.model, policy) !== d.model) $.ui.log(`model → ${d.model}·${d.effort} (${task.cls.category}) · ${d.reason}`)
      }
      const d = turnDecision.d
      if (d.apply) sent = { ...e, model: policy.model_ids[d.model] ?? d.model, effort: d.effort }
    }
    const r = yield* next(sent)
    const answered = aliasOf(r?.usage?.model ?? sent.model, policy)
    marks[answered] = { at: Date.now(), messageCount: e.messageCount ?? 0 }
    emit({ type: 'request', request_key: `${e.turnId}:${e.agentId ?? 'main'}:${e.index}`, task_id: task?.id ?? null,
           turn_id: e.turnId, agent_id: e.agentId ?? null, model: r?.usage?.model ?? sent.model, effort: sent.effort ?? null,
           usage: r?.usage ?? null })
    return r
  })

  on('tool.call', async ($: any, e: any, next: any) => {
    const result = await next(e)
    if (!recording() || !turn) return result
    turn.toolCalls++
    const failed = !!(result?.isError || result?.deny)
    let text = ''
    try { text = JSON.stringify(result?.result ?? result ?? '').slice(0, 4000) } catch {}
    const exitFail = failed || /exit code [1-9]|exited with code [1-9]|"exitCode":\s*[1-9]/i.test(text)
    if (failed) turn.toolErrors++
    if (e.tool === 'Bash' && typeof e.command === 'string') {
      if (testRes.some(re => re.test(e.command))) {
        turn.testsRun++
        if (exitFail || /\b\d+ (failed|failing)\b|FAILED|✘|AssertionError/.test(text)) {
          turn.testsFailed++
          if (task) { task.verified = false; task.lastFailedTests = true }
        } else {
          turn.testsPassed++
          if (task) { task.verified = true; task.lastFailedTests = false }
        }
      } else if (buildRes.some(re => re.test(e.command)) && exitFail) {
        turn.buildsFailed++
      }
      if (exitFail) {
        const line = text.split(/\\n/).find(l => /error|fail/i.test(l))
        if (line) turn.errorText = line.replace(/\d+/g, '#').slice(0, 160)
      }
    }
    return result
  })

  on('turn.start', async ($: any, e: any, next: any) => {
    if (recording() && e.agentId === undefined) {
      turn = { turnId: e.turnId, started: Date.now(), toolCalls: 0, toolErrors: 0, testsRun: 0, testsFailed: 0, testsPassed: 0, buildsFailed: 0 }
    }
    return next(e)
  })

  on('turn.complete', async ($: any, e: any, next: any) => {
    if (!recording()) return next(e)
    if (e.agentId !== undefined && e.agentId !== null) {
      emit({ type: 'turn', turn_id: `${e.turnId}:${e.agentId}`, task_id: task?.id ?? null, agent_id: e.agentId,
             duration_ms: e.durationMs, aborted: e.isAborted })
      return next(e)
    }
    try { quotaFrom(await $.session.usage()) } catch {}
    const t = turn
    if (task && t) {
      task.turns++
      const signals: string[] = []
      let score = 0
      if (t.testsFailed) { signals.push('tests_failed'); score += WEIGHT.strong }
      if (t.buildsFailed) { signals.push('build_failed'); score += WEIGHT.strong }
      if (t.errorText && task.lastError === t.errorText) { signals.push('same_error_twice'); score += WEIGHT.strong }
      if (t.toolErrors >= 3) { signals.push('tool_errors'); score += WEIGHT.medium }
      if (e.isAborted) { signals.push('aborted'); score += WEIGHT.weak }
      if (t.errorText) task.lastError = t.errorText
      task.window.push({ signals, score })
      task.window = task.window.slice(-2)
      maybeEscalate($, 'turn')
    }
    emit({ type: 'turn', turn_id: e.turnId, task_id: task?.id ?? null, duration_ms: e.durationMs, aborted: e.isAborted,
           tool_calls: t?.toolCalls ?? null, tool_errors: t?.toolErrors ?? null, tests_run: t?.testsRun ?? null,
           tests_failed: t?.testsFailed ?? null, tests_passed: t?.testsPassed ?? null, builds_failed: t?.buildsFailed ?? null,
           quota_5h: quota.fiveHour ?? null, quota_7d: quota.sevenDay ?? null })
    turn = null
    shellTurn = false
    await flush($)
    return next(e)
  })

  on('agent.spawn', async ($: any, e: any, next: any) => {
    if (!recording() || e.resume || e.isTeammate || !policy) return next(e)
    const type = String(e.agentType ?? e.subagentType ?? e.type ?? e.name ?? '')
    let chosen: string | null = null
    let reason = 'left as requested'
    if (!e.model && cfg.route_subagents && /^explore$/i.test(type)) {
      chosen = 'haiku'
      reason = 'read-only search agent → haiku'
    }
    const apply = routing() && chosen !== null
    emit({ type: 'spawn', task_id: task?.id ?? null, agent_type: type || null, requested_model: e.model ?? null,
           chosen_model: chosen, applied: apply, reason, keys: Object.keys(e) })
    return apply ? next({ ...e, model: chosen }) : next(e)
  })

  on('ui.render', { component: 'Spinner' }, async ($: any, e: any, next: any) => {
    if (disabled || !turnDecision || cfg.mode === 'off') return next(e)
    const d = turnDecision.d
    const tag = ` · ${d.model}·${d.effort}${d.apply ? '' : ' (observe)'}`
    return next({ ...e, props: { ...e.props, suffix: (e.props?.suffix ?? '') + tag } })
  })

  on('command.run', { command: 'mm' }, async ($: any, e: any) => {
    await reload($)
    const [sub, ...rest] = String(e.args ?? '').trim().split(/\s+/)
    const arg = rest.join(' ')
    switch ((sub || 'status').toLowerCase()) {
      case 'status': return { text: status() }
      case 'why': return { text: lastDecision ? `${lastDecision.model}·${lastDecision.effort}: ${lastDecision.reason}\n${JSON.stringify(lastDecision.candidates)}` : 'No routing decision yet in this session.' }
      case 'mode': {
        const m = rest[0]
        if (!['observe', 'auto', 'balanced', 'quality', 'cost', 'manual', 'off'].includes(m)) return { text: 'Usage: /mm mode observe|auto|balanced|quality|cost|manual|off' }
        await writeConfig($, { mode: m })
        return { text: `Mode set to ${m}.` }
      }
      case 'good': case 'bad': {
        if (!task) return { text: 'No open task to grade.' }
        task.feedback = sub.toLowerCase() as 'good' | 'bad'
        emit({ type: 'feedback', task_id: task.id, kind: task.feedback, note: arg || null })
        if (task.feedback === 'bad') { task.window.push({ signals: ['user_bad'], score: WEIGHT.strong }); maybeEscalate($, 'feedback') }
        await flush($)
        return { text: `Recorded: ${task.feedback}.` }
      }
      case 'use': {
        const m = /^(haiku|sonnet|opus|fable)(?::(low|medium|high|xhigh|max))?$/i.exec(arg)
        if (!m || !task) return { text: 'Usage: /mm use haiku|sonnet|opus|fable[:effort]  (applies to the current task)' }
        task.override = { model: m[1].toLowerCase(), effort: m[2]?.toLowerCase() }
        return { text: `Current task pinned to ${task.override.model}${task.override.effort ? '·' + task.override.effort : ''}.` }
      }
      case 'off': {
        await $.fs.write(`${home}/.model-manager/DISABLED`, ts() + '\n')
        disabled = true
        return { text: 'Model Manager off (kill switch). Run /mm on to re-enable.' }
      }
      case 'on': {
        try { await $.process.run(['rm', '-f', `${home}/.model-manager/DISABLED`]) } catch {}
        await reload($)
        return { text: `Model Manager on · mode ${cfg.mode}.` }
      }
      default: return { text: 'Usage: /mm [status|mode <m>|good|bad|use <model[:effort]>|why|off|on]' }
    }
  })
}
