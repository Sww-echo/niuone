function normalizedText(value) {
  return String(value || '').trim().toLocaleLowerCase('zh-CN')
}

function candidateScore(item) {
  const score = Number(item?.best_score ?? item?.score)
  return Number.isFinite(score) ? score : Number.NEGATIVE_INFINITY
}

function qualifiedCount(item) {
  const count = Number(item?.qualified_count)
  return Number.isFinite(count) ? count : 0
}

function timestamp(value) {
  const parsed = Date.parse(String(value || ''))
  return Number.isFinite(parsed) ? parsed : 0
}

function strategyLabel(strategy, strategyMeta) {
  return String(strategyMeta?.[strategy]?.label || strategy || '综合')
}

function finiteNumber(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function positiveNumber(value) {
  const number = finiteNumber(value)
  return number != null && number > 0 ? number : null
}

function firstProfitTargetR(item) {
  const strategy = String(item?.best_strategy || '')
  if (!strategy.startsWith('niu_')) return 2
  const regime = String(item?.market_regime || '').trim().toLowerCase()
  return strategy === 'niu_reversal_probe'
    && ['offensive', 'recovery', 'defensive'].includes(regime)
    ? 0.75
    : 1
}

export function todayCandidateExpectedOutcome(item = {}) {
  const entryPrice = positiveNumber(item.price)
  const stopPrice = positiveNumber(item.stop_price)
  const suppliedStopDistance = positiveNumber(item.stop_distance_pct)
  const stopDistancePct = suppliedStopDistance ?? (
    entryPrice != null && stopPrice != null && stopPrice < entryPrice
      ? (entryPrice - stopPrice) / entryPrice * 100
      : null
  )
  const targetR = firstProfitTargetR(item)
  const expectedReturnPct = stopDistancePct == null ? null : stopDistancePct * targetR
  const expectedLossPct = positiveNumber(item.effective_loss_distance_pct) ?? stopDistancePct
  const targetPrice = entryPrice == null || stopDistancePct == null
    ? null
    : entryPrice * (1 + expectedReturnPct / 100)
  return {
    expectedReturnPct,
    expectedLossPct,
    targetPrice,
    targetR,
  }
}

export function todayCandidateStrategyOptions(items, strategyMeta = {}) {
  const counts = new Map()
  for (const item of Array.isArray(items) ? items : []) {
    const strategy = String(item?.best_strategy || '')
    counts.set(strategy, (counts.get(strategy) || 0) + 1)
  }
  const strategies = [...counts.entries()]
    .map(([key, count]) => ({ key, count, label: strategyLabel(key, strategyMeta) }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label, 'zh-CN'))
  return [
    { key: 'all', count: Array.isArray(items) ? items.length : 0, label: '全部' },
    ...strategies,
  ]
}

export function filterAndSortTodayCandidates(
  items,
  { query = '', strategy = 'all', sortBy = 'score' } = {},
  strategyMeta = {},
) {
  const normalizedQuery = normalizedText(query)
  const selectedStrategy = String(strategy || 'all')
  const filtered = (Array.isArray(items) ? items : []).filter((item) => {
    const itemStrategy = String(item?.best_strategy || '')
    if (selectedStrategy !== 'all' && itemStrategy !== selectedStrategy) return false
    if (!normalizedQuery) return true
    const searchable = [
      item?.code,
      item?.name,
      item?.signal_theme,
      item?.industry,
      item?.sector,
      strategyLabel(itemStrategy, strategyMeta),
    ].map(normalizedText).join(' ')
    return searchable.includes(normalizedQuery)
  })

  return filtered.sort((left, right) => {
    let difference = 0
    if (sortBy === 'recent') {
      difference = timestamp(right?.last_qualified_at) - timestamp(left?.last_qualified_at)
    } else if (sortBy === 'frequency') {
      difference = qualifiedCount(right) - qualifiedCount(left)
    } else {
      difference = candidateScore(right) - candidateScore(left)
    }
    if (difference) return difference
    const scoreDifference = candidateScore(right) - candidateScore(left)
    if (scoreDifference) return scoreDifference
    return String(left?.code || '').localeCompare(String(right?.code || ''), 'zh-CN')
  })
}
