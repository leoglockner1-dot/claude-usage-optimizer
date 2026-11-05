// What the stats pane draws from, kept in $.state so it outlives a hot reload.

export type Route = {
  model: string
  effort: string
  category: string | null
  complexity: number | null
  reason: string
  applied: boolean
}

// ~/.model-manager/summary.json, written by the Python core after every ingest (fields the pane reads)
export type Summary = {
  generated_at: string
  today: { requests: number; est_cost_usd: number | null }
  week: {
    requests: number
    est_cost_usd: number | null
    savings: { routed_requests: number; est_savings_usd: number | null } | null
    model_mix: { model: string; request_share: number; cost_share: number | null }[]
  }
  daily: { date: string; est_cost_usd: number | null; requests: number }[]
}

export type View = {
  mode: string
  disabled: boolean
  route: Route | null
  task: { category: string; complexity: number; pinned: string | null } | null
  session: { requests: number; costUsd: number | null; contextTokens: number | null; contextWindow: number | null }
  quota: { fiveHour: number | null; sevenDay: number | null; fiveHourResets: string | null }
  summary: Summary | null
  dashboardUrl: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'model-manager': { view: Shaped<View> }
  }
}
