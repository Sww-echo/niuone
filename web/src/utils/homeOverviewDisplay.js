import { marketItems } from './marketDisplay.js'
import {
  practiceCandidateIndustryLabel,
  practiceCandidateStrategyMeta,
  practiceCandidateTier,
} from './practiceCandidateDisplay.js'

const MARKET_STATE_LABELS = {
  offensive: '进攻',
  rotation: '轮动',
  recovery: '修复',
  balanced: '均衡',
  cautious: '谨慎',
  defensive: '防守',
}

const MARKET_STATE_TONES = {
  offensive: 'positive',
  rotation: 'positive',
  recovery: 'warning',
  balanced: 'neutral',
  cautious: 'warning',
  defensive: 'negative',
}

export function finiteNumber(value) {
  if (value == null || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function valueTone(value) {
  const number = finiteNumber(value)
  if (number == null || number === 0) return 'flat'
  return number > 0 ? 'up' : 'down'
}

export function overviewViewportMode(width, height) {
  const viewportWidth = finiteNumber(width)
  const viewportHeight = finiteNumber(height)
  const hasWidth = viewportWidth != null && viewportWidth > 0
  const hasHeight = viewportHeight != null && viewportHeight > 0

  const layout = hasWidth && viewportWidth <= 720
    ? 'mobile'
    : hasWidth && viewportWidth < 1320
      ? 'compact'
      : 'wide'
  const density = hasHeight && viewportHeight < 560
    ? 'ultra-compact'
    : hasHeight && viewportHeight < 720
      ? 'compact'
      : 'comfortable'

  return { layout, density }
}

export function overviewFlowRowLimit(height) {
  const viewportHeight = finiteNumber(height)
  if (viewportHeight != null && viewportHeight > 0 && viewportHeight < 560) return 3
  if (viewportHeight != null && viewportHeight < 720) return 4
  if (viewportHeight != null && viewportHeight >= 900) return 6
  return 5
}

export function overviewMainlinePanelMode(height) {
  const panelHeight = finiteNumber(height)
  if (panelHeight != null && panelHeight > 0 && panelHeight < 118) return 'summary'
  if (panelHeight != null && panelHeight > 0 && panelHeight < 218) return 'compact'
  return 'full'
}

export function formatOverviewNumber(value, digits = 1) {
  const number = finiteNumber(value)
  if (number == null) return '--'
  return number.toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  })
}

export function formatOverviewPercent(value, digits = 1, signed = false) {
  const number = finiteNumber(value)
  if (number == null) return '--'
  const sign = signed && number > 0 ? '+' : ''
  return `${sign}${formatOverviewNumber(number, digits)}%`
}

export function formatOverviewAmount(value, signed = false) {
  const number = finiteNumber(value)
  if (number == null) return '--'
  const sign = signed && number > 0 ? '+' : ''
  const absolute = Math.abs(number)
  if (absolute >= 100_000_000) return `${sign}${formatOverviewNumber(number / 100_000_000, 2)}亿`
  if (absolute >= 10_000) return `${sign}${formatOverviewNumber(number / 10_000, 2)}万`
  return `${sign}${formatOverviewNumber(number, 2)}`
}

export function formatOverviewYi(value, signed = false) {
  const number = finiteNumber(value)
  if (number == null) return '--'
  const sign = signed && number > 0 ? '+' : ''
  return `${sign}${formatOverviewNumber(number, 1)}亿`
}

export function freshnessText(value) {
  const text = String(value || '').trim()
  if (!text) return '时间待补充'
  const time = text.match(/(?:T|\s)(\d{2}:\d{2})(?::\d{2})?/)?.[1]
  return time ? `更新 ${time}` : `更新 ${text}`
}

export function overviewMarketState(payload = {}) {
  const source = payload && typeof payload === 'object' ? payload : {}
  const code = String(source.state || '').trim()
  const score = finiteNumber(source.score)
  return {
    available: Boolean(code) || score != null,
    code,
    label: MARKET_STATE_LABELS[code] || code || '待评估',
    score,
    tone: MARKET_STATE_TONES[code] || 'neutral',
    allowNewBuys: source.allow_new_buys !== false,
  }
}

export function overviewPracticeMarketSummary(summary = {}, generating = false) {
  const source = summary && typeof summary === 'object' ? summary : {}
  const text = String(source.summary || '').trim()
  if (text) return text
  if (generating || source.running) {
    return String(source.stage_label || '').trim() || '正在生成此刻盘面总结与评价…'
  }
  if (source.loading !== false) return '正在读取模拟交易盘面资料…'
  if (source.error) return '模拟交易盘面资料暂不可用'
  return '模拟交易盘面资料待更新'
}

export function overviewBreadth(payload = {}, fallback = {}) {
  const latest = payload?.latest && typeof payload.latest === 'object' ? payload.latest : {}
  const advancing = finiteNumber(latest.red)
  const declining = finiteNumber(latest.green)
  const limitUp = finiteNumber(latest.limit_up) ?? finiteNumber(fallback.limit_up)
  const limitDown = finiteNumber(latest.limit_down) ?? finiteNumber(fallback.limit_down)
  const total = (advancing ?? 0) + (declining ?? 0)
  const advancingPct = total > 0 && advancing != null
    ? advancing / total * 100
    : finiteNumber(fallback.breadth_score)
  return {
    available: [advancingPct, limitUp, limitDown].some(value => value != null),
    countsAvailable: advancing != null && declining != null,
    advancing,
    declining,
    limitUp,
    limitDown,
    brokenLimit: finiteNumber(latest.broken_limit),
    advancingPct,
    medianChangePct: finiteNumber(latest.median_change_pct) ?? finiteNumber(fallback.median_change_pct),
    generatedAt: latest.generated_at || payload.generated_at || fallback.generated_at || '',
    displayDate: String(payload.display_date || latest.generated_at || '').slice(0, 10),
    previousTradingDay: payload.displaying_previous_trading_day === true,
  }
}

function overviewDailyReturn(practice, equity) {
  const explicitPnl = finiteNumber(practice.daily_pnl ?? practice.today_pnl)
  const explicitPnlPct = finiteNumber(
    practice.daily_pnl_pct
    ?? practice.today_pnl_pct
    ?? practice.daily_loss_budget_pnl_pct,
  )
  if (explicitPnl != null || explicitPnlPct != null) {
    const openingEquity = explicitPnl != null && equity != null ? equity - explicitPnl : null
    const pnl = explicitPnl ?? (
      equity != null && explicitPnlPct != null && explicitPnlPct > -100
        ? equity - equity / (1 + explicitPnlPct / 100)
        : null
    )
    const pnlPct = explicitPnlPct ?? (
      pnl != null && openingEquity != null && openingEquity > 0
        ? pnl / openingEquity * 100
        : null
    )
    return { pnl, pnlPct }
  }

  const points = [
    ...(Array.isArray(practice.daily_equity_history) ? practice.daily_equity_history : []),
    ...(Array.isArray(practice.equity_history) ? practice.equity_history : []),
  ].map(point => ({
    time: String(point?.time || point?.date || ''),
    equity: finiteNumber(point?.equity),
  })).filter(point => /^\d{4}-\d{2}-\d{2}/.test(point.time) && point.equity != null)
    .sort((left, right) => left.time.localeCompare(right.time))
  const timestamp = String(
    practice.current_date
    || practice.trading_calendar?.date
    || practice.current_time
    || practice.generated_at
    || practice.source_updated_at
    || '',
  )
  const latestHistoryDate = points.at(-1)?.time.slice(0, 10) || ''
  const timestampDate = /^\d{4}-\d{2}-\d{2}/.test(timestamp) ? timestamp.slice(0, 10) : ''
  const targetDate = practice.trading_calendar?.is_trading_day === false && latestHistoryDate
    ? latestHistoryDate
    : timestampDate || latestHistoryDate
  if (equity == null || !targetDate) return { pnl: null, pnlPct: null }

  const previousEquity = points.filter(point => point.time.slice(0, 10) < targetDate).at(-1)?.equity
  const firstTodayEquity = points.find(point => point.time.slice(0, 10) === targetDate)?.equity
  const baseEquity = previousEquity != null && previousEquity > 0
    ? previousEquity
    : firstTodayEquity != null && firstTodayEquity > 0
      ? firstTodayEquity
      : null
  if (baseEquity == null) return { pnl: null, pnlPct: null }

  const pnl = equity - baseEquity
  return { pnl, pnlPct: pnl / baseEquity * 100 }
}

export function overviewAccount(practice = {}) {
  const equity = finiteNumber(practice.total_equity)
  const cash = finiteNumber(practice.cash)
  const initialCash = finiteNumber(practice.initial_cash)
  const explicitPnl = finiteNumber(practice.total_pnl)
  const pnl = explicitPnl ?? (
    equity != null && initialCash != null ? equity - initialCash : null
  )
  const explicitPnlPct = finiteNumber(practice.total_pnl_pct)
  const pnlPct = explicitPnlPct ?? (
    pnl != null && initialCash != null && initialCash !== 0 ? pnl / initialCash * 100 : null
  )
  const exposurePct = equity != null && equity > 0 && cash != null
    ? (equity - cash) / equity * 100
    : null
  const positions = Array.isArray(practice.positions) ? practice.positions : []
  const dailyReturn = overviewDailyReturn(practice, equity)
  return {
    available: [equity, cash, initialCash].some(value => value != null) || positions.length > 0,
    equity,
    cash,
    pnl,
    pnlPct,
    dailyPnl: dailyReturn.pnl,
    dailyPnlPct: dailyReturn.pnlPct,
    exposurePct,
    positionCount: positions.length,
    paused: practice.trading_paused === true,
    pauseReason: String(practice.pause_reason || ''),
    generatedAt: practice.current_time || practice.source_updated_at || practice.generated_at || '',
  }
}

function indexPriority(item) {
  const key = String(item?.key || '').toLowerCase()
  const name = String(item?.name || '')
  const code = String(item?.code || '').toLowerCase()
  const values = [
    [/^(sh|sse|sh_comp)$/.test(key) || code === 'sh000001' || /上证指数/.test(name), 0],
    [/^(sz|szse|sz_comp)$/.test(key) || code === 'sz399001' || /深证成指/.test(name), 1],
    [/^(cyb|chinext)$/.test(key) || code === 'sz399006' || /创业板/.test(name), 2],
    [/^(kc50|star50)$/.test(key) || code === 'sh000688' || /科创(?:板|50)/.test(name), 3],
  ]
  return values.find(([match]) => match)?.[1] ?? 20
}

export function overviewIndices(payload = {}, limit = 4) {
  return marketItems(payload, 'a_index', 'domestic')
    .map((item, order) => ({ ...item, _order: order }))
    .sort((left, right) => indexPriority(left) - indexPriority(right) || left._order - right._order)
    .slice(0, limit)
    .map(({ _order, ...item }) => item)
}

function candidateScore(item) {
  return finiteNumber(item?.best_score ?? item?.score) ?? Number.NEGATIVE_INFINITY
}

export function overviewCandidatePeriod(generatedAt, tradingCalendar = {}) {
  const generatedDate = String(generatedAt || '').slice(0, 10)
  const currentDate = String(tradingCalendar?.date || '').slice(0, 10)
  const previousDate = String(
    tradingCalendar?.previous_trading_day || '',
  ).slice(0, 10)
  const historical = Boolean(
    /^\d{4}-\d{2}-\d{2}$/.test(generatedDate)
    && /^\d{4}-\d{2}-\d{2}$/.test(currentDate)
    && generatedDate < currentDate,
  )
  const previousTradingDay = historical && generatedDate === previousDate
  return {
    historical,
    previousTradingDay,
    generatedDate,
    label: previousTradingDay
      ? `上一交易日候选 ${generatedDate.slice(5)}`
      : historical
        ? `历史候选 ${generatedDate.slice(5)}`
        : '',
  }
}

export function overviewCandidateStrategyDisplayLabel(strategyId, strategyLabel) {
  const id = String(strategyId || '').trim()
  const label = String(strategyLabel || '').trim() || id || '综合策略'
  if (!id.startsWith('niu_') && !label.startsWith('牛牛战法')) return label

  const subStrategy = label
    .replace(/^牛牛战法\s*(?:[·•｜|/—–-]\s*)?/, '')
    .trim()
  return subStrategy || label
}

export function overviewCandidates(
  items = [],
  payloadMeta = {},
  limit = Infinity,
) {
  const strategyMeta = practiceCandidateStrategyMeta(payloadMeta)
  const tierOrder = { high: 0, mid: 1, low: 2 }
  return (Array.isArray(items) ? items : [])
    .map((item, order) => {
      const strategyId = String(item?.best_strategy || '')
      const strategyLabel = strategyMeta[strategyId]?.label || strategyId || '综合策略'
      const tier = practiceCandidateTier(item)
      const blockers = Array.isArray(item?.hard_blockers) ? item.hard_blockers : []
      return {
        ...item,
        _order: order,
        score: candidateScore(item),
        strategyLabel,
        strategyDisplayLabel: overviewCandidateStrategyDisplayLabel(strategyId, strategyLabel),
        industryLabel: practiceCandidateIndustryLabel(item),
        themeLabel: String(item?.signal_theme || ''),
        tier,
        tierLabel: tier === 'high' ? '交易达标' : tier === 'mid' && blockers.length ? '未达标' : tier === 'mid' ? '待确认' : '仅观察',
      }
    })
    .sort((left, right) => tierOrder[left.tier] - tierOrder[right.tier]
      || right.score - left.score
      || left._order - right._order)
    .slice(0, limit)
    .map(({ _order, ...item }) => item)
}

export function overviewThemes(payload = {}, limit = 5, ranking = 'structure') {
  const todayRanking = ranking === 'today'
  const source = todayRanking ? payload?.today_themes : payload?.themes
  const themes = Array.isArray(source) ? source : []
  return themes
    .map((theme, order) => {
      const strongStocks = Array.isArray(theme?.strong_stocks) ? theme.strong_stocks : []
      const todayLeaders = Array.isArray(theme?.today_leaders) ? theme.today_leaders : []
      const structuralLeader = theme?.leader_stock || strongStocks[0] || null
      const leader = todayRanking
        ? theme?.today_leader_stock || todayLeaders[0] || structuralLeader
        : structuralLeader
      const rankedStocks = todayRanking && todayLeaders.length ? todayLeaders : strongStocks
      const seenStocks = new Set()
      const coreStocks = [leader, ...rankedStocks]
        .filter(stock => stock && typeof stock === 'object')
        .map(stock => ({
          code: String(stock?.code || ''),
          name: String(stock?.name || ''),
          changePct: finiteNumber(stock?.change_pct),
          score: finiteNumber(stock?.strong_score),
        }))
        .filter(stock => {
          const key = stock.code || stock.name
          if (!key || seenStocks.has(key)) return false
          seenStocks.add(key)
          return true
        })
      return {
        ...theme,
        _order: order,
        rankingKey: todayRanking ? 'today' : 'structure',
        displayName: String(theme?.industry || theme?.name || ''),
        displayScore: finiteNumber(todayRanking ? theme?.today_strength_score : theme?.score),
        comparisonScore: finiteNumber(todayRanking ? theme?.score : theme?.today_strength_score),
        lifecycle: String(theme?.niuone_lifecycle_label || theme?.state || '待确认'),
        breadth: finiteNumber(todayRanking
          ? theme?.today_adjusted_breadth_pct ?? theme?.today_breadth_pct
          : theme?.effective_breadth_pct),
        todayScore: finiteNumber(theme?.today_strength_score),
        medianChangePct: finiteNumber(theme?.today_median_change_pct),
        strongStockCount: finiteNumber(todayRanking
          ? theme?.today_attributed_up_count ?? theme?.today_up_count
          : theme?.attributed_strong_stock_count ?? theme?.strong_stock_count),
        confirmationCount: finiteNumber(theme?.confirmation_count),
        countLabel: todayRanking ? '上涨' : '强股',
        comparisonLabel: todayRanking ? '结构' : '今日',
        leaderBadge: todayRanking ? '领涨' : '龙头',
        stockListLabel: todayRanking ? '今日领涨股' : '结构代表股',
        leader,
        coreStocks,
        followers: coreStocks.slice(1, 3)
          .map(stock => String(stock?.name || stock?.code || ''))
          .filter(Boolean),
      }
    })
    .filter(theme => theme.displayName)
    .sort((left, right) => (right.displayScore ?? -Infinity) - (left.displayScore ?? -Infinity)
      || (todayRanking
        ? (right.medianChangePct ?? -Infinity) - (left.medianChangePct ?? -Infinity)
        : 0)
      || left._order - right._order)
    .slice(0, limit)
    .map(({ _order, ...theme }) => theme)
}

function marketMoveValue(row) {
  return finiteNumber(row?.pct ?? row?.change_pct)
}

export function overviewSectorMoves(payload = {}, direction = 'gain', limit = 3) {
  const baseRows = Array.isArray(payload?.sectors)
    ? payload.sectors
    : (Array.isArray(payload?.items) ? payload.items : [])
  const explicit = direction === 'loss' ? payload?.loss_top : payload?.gain_top
  const rows = Array.isArray(explicit) && explicit.length ? explicit : baseRows
  return rows
    .map((row, order) => ({ ...row, _order: order, move: marketMoveValue(row) }))
    .filter(row => row.name && row.move != null)
    .sort((left, right) => direction === 'loss'
      ? left.move - right.move || left._order - right._order
      : right.move - left.move || left._order - right._order)
    .slice(0, limit)
    .map(({ _order, ...row }) => row)
}

export function overviewMoneyFlow(payload = {}, direction = 'inflow', limit = 3) {
  const rows = Array.isArray(payload?.[direction]) ? payload[direction] : []
  return rows.slice(0, limit).map(row => ({
    ...row,
    netFlowYi: finiteNumber(row?.net_flow_yi),
  }))
}

export function overviewMoneyFlowNet(payload = {}) {
  const rows = [
    ...(Array.isArray(payload?.inflow) ? payload.inflow : []),
    ...(Array.isArray(payload?.outflow) ? payload.outflow : []),
  ]
  const values = rows
    .map(row => finiteNumber(row?.net_flow_yi))
    .filter(value => value != null)
  return values.length ? values.reduce((total, value) => total + value, 0) : null
}
