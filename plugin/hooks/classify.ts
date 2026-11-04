// Local task classifier: pure functions, no model call, well under a millisecond.
// English and German keywords, because Leo writes prompts in both.

export type Category =
  | 'edit' | 'docs' | 'luau' | 'roblox_ui' | 'code' | 'debug' | 'arch'
  | 'research' | 'game_design' | 'blender' | 'refactor' | 'test' | 'chat'

export type Classification = {
  category: Category
  complexity: number // 1..5
  risk: 'low' | 'medium' | 'high'
  flags: string[]
  confidence: number // 0..1
  followUp: boolean
  correction: boolean
  positive: boolean
}

type Rule = [Category, RegExp, number]

const RULES: Rule[] = [
  ['edit', /\b(rename|typo|umbenenn\w*|tippfehler|change .{1,40} to |ersetze|replace .{1,40} with)\b/i, 3],
  ['edit', /\b(one[- ]liner|small change|kleine änderung|just change|nur .{0,20}ändern)\b/i, 2],
  ['docs', /\b(readme|docs?|documentation|dokumentation|docstring|comment|kommentar|changelog|\.md\b)/i, 2.5],
  ['luau', /\b(luau|\.luau|\.lua\b|modulescript|localscript|serverscript|remoteevent|remotefunction|datastore|roblox|rbx\w*|workspace\.|game:getservice|humanoid)/i, 3],
  ['roblox_ui', /\b(screengui|surfacegui|billboardgui|frame|textlabel|textbutton|imagelabel|uilistlayout|uigridlayout|uicorner|uistroke|tweenservice|hud|menü|menu|inventory ui|shop ui|gui)\b/i, 2.5],
  ['code', /\b(implement\w*|feature|function|class|endpoint|api|module|script|baue|erstelle|programmier\w*|schreib\w* (ein|eine|code)|add .{0,30}(support|option|command))\b/i, 1.5],
  ['debug', /\b(bug|error|fehler|crash\w*|exception|stack ?trace|traceback|doesn'?t work|not working|geht nicht|funktioniert nicht|broken|kaputt|why does|warum (geht|funktioniert|ist)|fails?|failing|undefined|nil value|attempt to index)\b/i, 3],
  ['arch', /\b(architect\w*|architektur|system design|design (the|a|an) |trade-?offs?|structure the|struktur\w*|how should (we|i) (build|structure|organi[sz]e)|data flow|schema design)\b/i, 3],
  ['research', /\b(research|recherch\w*|compare|vergleich\w*|which (library|tool|framework)|welche[sr]? (tool|bibliothek)|find out|investigate|untersuch\w*|look up|best practice)\b/i, 2.5],
  ['game_design', /\b(game design|spieldesign|economy|ökonomie|balanc\w*|progression|odds|rarity|drop rate|mechanic\w*|gameplay loop|monetiz\w*|gamepass)\b/i, 2.5],
  ['blender', /\b(blender|bpy|fbx|mesh|uv ?map|rig\w*|3d model|3d-modell|render\w*|sculpt)\b/i, 3],
  ['refactor', /\b(refactor\w*|clean ?up|aufräum\w*|extract|restructure|umstrukturier\w*|deduplicate|simplify|vereinfach\w*)\b/i, 2.5],
  ['test', /\b(tests?|testing|unit test|pytest|jest|vitest|testez|coverage|spec|teste\w*)\b/i, 2],
]

const FOLLOW_UP = /^(ok(ay)?|yes|yep|ja|jo|go( ahead)?|mach( weiter| das)?|weiter|continue|do it|passt|gut|super|perfect|perfekt|thanks?|danke|next|nächste[rs]?|and (now|also)|und jetzt|jetzt noch|now also|also\b|auch noch)\b/i
const CORRECTION = /\b(still (not|broken|fails|failing|errors?|crash\w*|doesn'?t)|doesn'?t work|not working|wrong|falsch|nope|that broke|immer noch|geht (immer noch )?nicht|funktioniert (immer noch )?nicht|same error|gleicher fehler|selber fehler|didn'?t fix|nicht behoben|hat nicht geklappt|revert)\b/i
const POSITIVE = /\b(works( now)?|it worked|fixed|funktioniert( jetzt)?|klappt|passt|perfect|perfekt|great|super|thanks|danke|looks good|sieht gut aus)\b/i
const HARD = /\b(architecture|architektur|from scratch|von grund auf|entire|whole (system|project|codebase)|gesamte[nr]?|komplett\w*|redesign|rewrite|migrat\w*|multi-?file|across (all|the) files|complex|komplex\w*|difficult|schwierig|tricky|concurren\w*|race condition|performance|optimi[sz]\w*|security|sicherheit)\b/i
const EASY = /\b(simple|einfach\w*|quick(ly)?|schnell|just|nur|small|klein\w*|minor|trivial|kurz)\b/i
const RISK_HIGH = /\b(security|sicherheit|auth\w*|password|passwort|token|secret|credential|payment|zahlung|robux|production|prod\b|live server|migration|drop table|delete all|rm -rf|datastore (wipe|reset)|purchase|monetiz\w*)\b/i
const RISK_MED = /\b(refactor\w*|database|datenbank|datastore|schema|deploy\w*|publish\w*|veröffentlich\w*)\b/i

// "[sonnet]" or "[opus:high]" at the start of a prompt. A leading "!" is Claude Code's shell-command
// prefix and never reaches the mod, so it cannot be used for overrides.
export function parseOverride(text: string): { model?: string; effort?: string; text: string } {
  const m = /^\s*\[(haiku|sonnet|opus|fable)(?::(low|medium|high|xhigh|max))?\]\s*/i.exec(text)
  if (!m) return { text }
  return { model: m[1].toLowerCase(), effort: m[2]?.toLowerCase(), text: text.slice(m[0].length) }
}

export function classify(text: string, cwd: string, hasTask: boolean): Classification {
  const t = text.trim()
  const lower = t.toLowerCase()
  const short = t.length < 80

  const correction = hasTask && CORRECTION.test(t)
  const positive = hasTask && POSITIVE.test(t) && !correction

  const score: Partial<Record<Category, number>> = {}
  for (const [cat, re, w] of RULES) {
    const hits = lower.match(new RegExp(re.source, 'gi'))
    if (hits) score[cat] = (score[cat] ?? 0) + w + 0.5 * (hits.length - 1)
  }
  // project context: Roblox projects in Leo's vault, Blender scripts
  if (/roblox|luau|rbx|ONE MORE THING|ROLL A ROLLER|MELT THE ICE|ROLL TO SURVIVE/i.test(cwd)) {
    if (score.luau !== undefined || score.code !== undefined || score.debug !== undefined) score.luau = (score.luau ?? 0) + 1
  }
  if ((score.roblox_ui ?? 0) > 0 && (score.luau ?? 0) > 0) score.roblox_ui = (score.roblox_ui ?? 0) + 1
  if ((score.debug ?? 0) > 0 && (score.luau ?? 0) > 0) score.debug = (score.debug ?? 0) + 0.5

  let category: Category = 'chat'
  let best = 0
  let second = 0
  for (const [cat, s] of Object.entries(score) as [Category, number][]) {
    if (s > best) { second = best; best = s; category = cat } else if (s > second) second = s
  }
  if (best === 0) category = /```|\.(ts|js|py|lua|luau|rs|go|json)\b|function |def /.test(t) ? 'code' : 'chat'
  const followUp = hasTask && ((FOLLOW_UP.test(t) && t.length < 160) || (best === 0 && t.split(/\s+/).length <= 3))
  const confidence = best === 0 ? 0.3 : Math.min(1, 0.4 + (best - second) / Math.max(best, 1) * 0.6)

  // complexity 1..5
  let c = 2
  if (t.length > 400) c++
  if (t.length > 1500) c++
  if (HARD.test(t)) c++
  const asks = (t.match(/^\s*(\d+[.)]|[-*•])\s+/gm) ?? []).length
  if (asks >= 3) c++
  if (EASY.test(t) && !HARD.test(t)) c--
  if (category === 'arch') c = Math.max(c, 3)
  if (category === 'edit' || category === 'chat') c = Math.min(c, 3)
  if (short && category !== 'arch' && category !== 'debug') c = Math.min(c, 2)
  const complexity = Math.max(1, Math.min(5, c))

  const risk = RISK_HIGH.test(t) ? 'high' : RISK_MED.test(t) ? 'medium' : 'low'

  const flags: string[] = []
  if (score.test) flags.push('needs_tests')
  if (score.research) flags.push('research')
  if (asks >= 3 || /multi-?file|several files|mehrere dateien|across/i.test(t)) flags.push('multi_file')
  if (score.roblox_ui) flags.push('ui')

  return { category, complexity, risk, flags, confidence: Math.round(confidence * 100) / 100, followUp, correction, positive }
}
