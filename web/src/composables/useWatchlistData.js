import { reactive } from 'vue'

const REQUEST_TIMEOUT_MS = 60 * 1000
const ALLOWED_DAYS = [5, 7, 15, 60]

const state = reactive({
  loading: true,
  loaded: false,
  busy: false,
  error: '',
  status: '',
  days: 5,
  groupId: '',
  q: '',
  board: {
    days: 5,
    allowed_days: ALLOWED_DAYS,
    trade_dates: [],
    groups: [],
    sections: [],
    stocks: [],
    total: 0,
    summary: {
      filled_count: 0,
      pending_count: 0,
      total_cost: 0,
      total_market_value: 0,
      total_pnl: 0,
      total_pnl_percent: null,
      target_amount_default: 10000,
    },
  },
})

let loadSequence = 0
const controllers = new Map()

function controllerFor(key) {
  const existing = controllers.get(key)
  if (existing) existing.abort()
  const controller = new AbortController()
  controllers.set(key, controller)
  return controller
}

function isAdminRequired(error) {
  const code = String(error?.code || error?.message || '')
  const status = Number(error?.status || 0)
  return status === 401
    || status === 403
    || code.includes('admin')
    || code.includes('password')
    || code.includes('unauthorized')
}

async function api(url, {
  method = 'GET',
  body,
  action = false,
  key,
  timeoutMs = REQUEST_TIMEOUT_MS,
} = {}) {
  const controller = controllerFor(key || `${method}:${url}`)
  let timedOut = false
  const timeout = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  try {
    const headers = { Accept: 'application/json' }
    if (action) headers['X-NiuOne-Action'] = '1'
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const response = await fetch(url, {
      method,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller.signal,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      const error = new Error(payload?.error || `HTTP ${response.status}`)
      error.code = String(payload?.error || '')
      error.status = response.status
      throw error
    }
    return payload
  } catch (error) {
    if (timedOut) throw new Error('自选股请求超时')
    throw error
  } finally {
    window.clearTimeout(timeout)
    if (controllers.get(key || `${method}:${url}`) === controller) {
      controllers.delete(key || `${method}:${url}`)
    }
  }
}

function applyBoard(board) {
  if (!board || typeof board !== 'object') return
  state.board = {
    days: Number(board.days) || state.days,
    allowed_days: Array.isArray(board.allowed_days) ? board.allowed_days : ALLOWED_DAYS,
    trade_dates: board.trade_dates || [],
    latest_trade_date: board.latest_trade_date || '',
    groups: board.groups || [],
    sections: board.sections || [],
    stocks: board.stocks || [],
    total: Number(board.total) || 0,
    summary: board.summary || state.board.summary,
    generated_at: board.generated_at || '',
  }
  if (board.days) state.days = Number(board.days)
}

async function loadBoard({ background = false } = {}) {
  const sequence = ++loadSequence
  if (!background && !state.loaded) state.loading = true
  state.error = ''
  try {
    const params = new URLSearchParams({
      days: String(state.days || 5),
      q: state.q || '',
    })
    if (state.groupId) params.set('group_id', String(state.groupId))
    const board = await api(`/api/watchlist/board?${params}`, { key: 'watchlist-board' })
    if (sequence !== loadSequence) return false
    applyBoard(board)
    state.loading = false
    state.loaded = true
    state.status = board.generated_at
      ? `已更新 ${String(board.generated_at).replace('T', ' ').slice(0, 19)}`
      : '就绪'
    window.dispatchEvent(new CustomEvent('niuone:last-updated', {
      detail: { value: String(board.generated_at || '').slice(11, 19) || '--' },
    }))
    return true
  } catch (error) {
    if (sequence !== loadSequence) return false
    state.error = error instanceof Error ? error.message : String(error)
    state.loading = false
    if (!state.loaded) state.status = '加载失败'
    return false
  }
}

async function runAction(label, fn) {
  if (state.busy) return { ok: false, error: 'busy' }
  state.busy = true
  state.status = label
  state.error = ''
  try {
    const result = await fn()
    if (result?.board) applyBoard(result.board)
    else if (result?.groups) state.board.groups = result.groups
    else await loadBoard({ background: true })
    state.status = result?.message || '完成'
    return { ok: true, result }
  } catch (error) {
    if (isAdminRequired(error)) {
      state.status = '需要管理员验证'
      return { ok: false, needAdmin: true, error }
    }
    state.error = error instanceof Error ? error.message : String(error)
    state.status = '失败'
    return { ok: false, error }
  } finally {
    state.busy = false
  }
}

function setDays(days) {
  const value = Number(days)
  state.days = ALLOWED_DAYS.includes(value) ? value : 5
  return loadBoard()
}

function setGroupId(groupId) {
  state.groupId = groupId === null || groupId === undefined ? '' : String(groupId)
  return loadBoard()
}

function setQuery(q) {
  state.q = String(q || '')
  return loadBoard()
}

function createGroup({ name, note = '' }) {
  return runAction('创建分组…', () => api('/api/watchlist/groups', {
    method: 'POST',
    action: true,
    body: { name, note },
    key: 'watchlist-create-group',
  }).then(result => ({ ...result, message: `已创建分组 ${name}` })))
}

function updateGroup(groupId, fields) {
  return runAction('更新分组…', () => api(`/api/watchlist/groups/${groupId}`, {
    method: 'PATCH',
    action: true,
    body: fields,
    key: `watchlist-patch-group-${groupId}`,
  }).then(result => ({ ...result, message: '分组已更新' })))
}

function deleteGroup(groupId) {
  return runAction('删除分组…', () => api(`/api/watchlist/groups/${groupId}`, {
    method: 'DELETE',
    action: true,
    key: `watchlist-del-group-${groupId}`,
  }).then(result => ({ ...result, message: '分组已删除' })))
}

function addStocks(payload) {
  return runAction('添加股票…', () => api('/api/watchlist/stocks', {
    method: 'POST',
    action: true,
    body: payload,
    key: 'watchlist-add-stocks',
    timeoutMs: 120 * 1000,
  }).then(result => ({
    ...result,
    message: `已处理 ${(result.added || []).length} 只股票`,
  })))
}

function updateStock(code, fields) {
  return runAction('更新股票…', () => api(`/api/watchlist/stocks/${code}`, {
    method: 'PATCH',
    action: true,
    body: fields,
    key: `watchlist-patch-stock-${code}`,
  }).then(result => ({ ...result, message: `已更新 ${code}` })))
}

function deleteStock(code) {
  return runAction('删除股票…', () => api(`/api/watchlist/stocks/${code}`, {
    method: 'DELETE',
    action: true,
    key: `watchlist-del-stock-${code}`,
  }).then(result => ({ ...result, message: `已删除 ${code}` })))
}

function updateToday() {
  return runAction('更新今日行情…', () => api('/api/watchlist/quotes/update-today', {
    method: 'POST',
    action: true,
    body: {},
    key: 'watchlist-update-today',
    timeoutMs: 120 * 1000,
  }).then(result => ({
    ...result,
    message: result.skipped
      ? `非交易日，已跳过写入`
      : `已更新 ${result.updated || 0} 只`,
  })))
}

function backfill({ days = 60, missingOnly = true } = {}) {
  return runAction('回补历史行情…', () => api('/api/watchlist/quotes/backfill', {
    method: 'POST',
    action: true,
    body: { days, missing_only: missingOnly },
    key: 'watchlist-backfill',
    timeoutMs: 180 * 1000,
  }).then(result => ({
    ...result,
    message: `已回补 ${result.stocks || 0} 只 / 写入 ${result.quotes || 0} 条`,
  })))
}

export function useWatchlistData() {
  return {
    state,
    loadBoard,
    setDays,
    setGroupId,
    setQuery,
    createGroup,
    updateGroup,
    deleteGroup,
    addStocks,
    updateStock,
    deleteStock,
    updateToday,
    backfill,
  }
}
