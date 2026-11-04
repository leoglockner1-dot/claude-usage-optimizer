// Routing decision: pure function of the classification, the active policy, config and session state.
// Design notes: vault "01 - AI Model Manager/Architecture/Routing Logic.md".
// Cache-aware switching follows nixfred/claude-router (MIT).

import type { Classification } from './classify'

export type Cell = { model: string; effort: string; p?: number; n?: number; source?: string; reason?: string }

export type Policy = {
  version: number
  model_ids: Record<string, string>
  tiers: Record<string, number>
  prices: Record<string, { input: number; cache_write_1h: number; cache_read: number; output: number; long_ctx_threshold?: number | null; long_ctx_multiplier?: number | null }>
  ladder: [string, string][]
  table: Record<string, Record<string, Cell>>
}

export type Config = {
  mode: string
  auto_models: string[]
  ceiling: string
  manual_model: string
  manual_effort: string
  haiku_main_max_context: number
  min_switch_saving: number
  quota_guard: { five_hour: number; seven_day: number }
}

export type CacheMark = { at: number; messageCount: number }

export type RouteInput = {
  cls: Classification
  policy: Policy
  cfg: Config
  currentModel: string // alias of the model the conversation is on now
  contextTokens: number
  messageCount: number
  now: number
  marks: Record<string, CacheMark>
  escalationLevel: number
  override?: { model?: string; effort?: string }
  quota: { fiveHour?: number; sevenDay?: number }
}

export type Decision = {
  model: string
  effort: string
  apply: boolean
  reason: string
  candidates: { model: string; effort: string; estUsd: number; note?: string }[]
}

const EFFORT_RANK: Record<string, number> = { low: 0, medium: 1, high: 2, xhigh: 3, max: 4 }
const CACHE_TTL_MS = 55 * 60 * 1000 // Claude Code uses 1h cache writes; keep a margin
const TYPICAL_OUTPUT = 1500

export function band(complexity: number): string {
  return complexity <= 2 ? 'low' : complexity === 3 ? 'mid' : 'high'
}

export function aliasOf(model: string | undefined, policy: Policy): string {
  if (!model) return 'unknown'
  for (const [alias, id] of Object.entries(policy.model_ids)) if (model === id || model.startsWith(id) || model === alias) return alias
  for (const fam of ['haiku', 'sonnet', 'opus', 'fable']) if (model.includes(fam)) return fam
  return model
}

function ladderIndex(policy: Policy, model: string, effort: string): number {
  // position of the closest ladder rung at or below (model, effort)
  const tier = policy.tiers[model] ?? 2
  let idx = 0
  policy.ladder.forEach(([m, e], i) => {
    const t = policy.tiers[m] ?? 2
    if (t < tier || (t === tier && EFFORT_RANK[e] <= EFFORT_RANK[effort])) idx = i
  })
  return idx
}

// API-equivalent USD of one request on `model` given the conversation size, with or without a warm cache
export function requestCost(policy: Policy, model: string, contextTokens: number, warmShare: number): number {
  const p = policy.prices[model]
  if (!p) return 0
  const warm = contextTokens * warmShare
  const cold = contextTokens - warm
  let usd = (warm * p.cache_read + cold * p.cache_write_1h + TYPICAL_OUTPUT * p.output) / 1e6
  if (p.long_ctx_threshold && contextTokens > p.long_ctx_threshold) usd *= p.long_ctx_multiplier ?? 1
  return usd
}

function warmShare(mark: CacheMark | undefined, now: number, messageCount: number): number {
  if (!mark || now - mark.at > CACHE_TTL_MS || messageCount <= 0) return 0
  return Math.max(0, Math.min(1, mark.messageCount / messageCount))
}

export function decide(i: RouteInput): Decision {
  const { cls, policy, cfg } = i
  const tierOf = (m: string) => policy.tiers[m] ?? 2
  const ceilingTier = tierOf(cfg.ceiling)
  const autoMax = Math.max(...cfg.auto_models.map(tierOf))
  const notes: string[] = []

  if (cfg.mode === 'manual') {
    return { model: cfg.manual_model, effort: cfg.manual_effort, apply: true, reason: 'manual mode', candidates: [] }
  }

  // 1. base cell from the policy
  const cell = policy.table[cls.category]?.[band(cls.complexity)] ?? { model: 'sonnet', effort: 'medium', source: 'fallback' }
  let model = cell.model
  let effort = cell.effort
  const src = cell.n ? `p=${(cell.p ?? 0).toFixed(2)} n=${cell.n}` : 'prior'
  notes.push(`${cls.category}/${band(cls.complexity)} → ${model}·${effort} (${src})`)

  // 2. mode shifts along the ladder
  // (the cell itself need not be a ladder rung; it is only replaced when we actually move)
  const base = ladderIndex(policy, model, effort)
  let idx = base
  if (cfg.mode === 'quality') { idx += 1; notes.push('quality +1') }
  if (cfg.mode === 'cost' && cls.risk !== 'high') { idx -= 1; notes.push('cost −1') }
  // 3. escalation inside the task
  if (i.escalationLevel > 0) { idx += i.escalationLevel; notes.push(`escalated +${i.escalationLevel}`) }
  idx = Math.max(0, Math.min(policy.ladder.length - 1, idx))
  if (idx !== base) [model, effort] = policy.ladder[idx]

  // 4. explicit override wins
  if (i.override?.model) {
    model = i.override.model
    effort = i.override.effort ?? effort
    notes.length = 0
    notes.push(`override → ${model}·${effort}`)
  }

  // 5. guards
  const capTier = i.override?.model || i.escalationLevel > 0 ? ceilingTier : Math.min(autoMax, ceilingTier)
  if (tierOf(model) > capTier) {
    const capped = Object.keys(policy.tiers).filter(m => tierOf(m) <= capTier).sort((a, b) => tierOf(b) - tierOf(a))[0] ?? 'sonnet'
    notes.push(`cap ${model}→${capped}`)
    model = capped
  }
  if (cls.risk === 'high' && !i.override?.model) {
    if (tierOf(model) < 2) { model = 'sonnet'; notes.push('high risk → ≥sonnet') }
    if (EFFORT_RANK[effort] < EFFORT_RANK.high) effort = 'high'
  }
  if (model === 'haiku' && i.contextTokens > cfg.haiku_main_max_context && !i.override?.model) {
    model = 'sonnet'
    notes.push(`context ${Math.round(i.contextTokens / 1000)}k > haiku limit`)
  }

  // 6. cache-aware switch decision (downgrades only; upgrades are taken when needed)
  const cur = i.currentModel
  const candidates: Decision['candidates'] = []
  if (model !== cur && policy.prices[cur] && policy.prices[model]) {
    const stay = requestCost(policy, cur, i.contextTokens, warmShare(i.marks[cur], i.now, i.messageCount) || 1)
    const go = requestCost(policy, model, i.contextTokens, warmShare(i.marks[model], i.now, i.messageCount))
    candidates.push({ model: cur, effort, estUsd: round(stay), note: 'stay' }, { model, effort, estUsd: round(go), note: 'switch' })
    const downgrade = tierOf(model) < tierOf(cur)
    // a switch pays the cache rewrite once; compare against roughly three turns of savings
    const saving = (stay - go) * 3
    if (downgrade && !i.override?.model && saving < cfg.min_switch_saving * stay * 3) {
      notes.push(`hold ${cur}: switch to ${model} not worth the cache rewrite`)
      model = cur
    }
    const q5 = i.quota.fiveHour ?? 0
    const q7 = i.quota.sevenDay ?? 0
    if (!downgrade && i.escalationLevel === 0 && !i.override?.model && cls.risk !== 'high' &&
        (q5 >= cfg.quota_guard.five_hour || q7 >= cfg.quota_guard.seven_day) && tierOf(model) > 2) {
      notes.push(`quota guard (5h ${q5}%, 7d ${q7}%)`)
      model = cur
    }
  }

  return { model, effort, apply: true, reason: notes.join('; '), candidates }
}

function round(x: number): number {
  return Math.round(x * 1e5) / 1e5
}
