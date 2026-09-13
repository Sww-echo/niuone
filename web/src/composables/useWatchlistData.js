import { reactive } from 'vue'
import { isJobActive } from '../utils/watchlistDisplay.js'

const REQUEST_TIMEOUT_MS = 60 * 1000
const ALLOWED_DAYS = [5, 7, 15, 60]

const state = reactive({
  loading: true,
  loaded: false,
  busy: false,
  error: '',
  loadError: '',
  status: '',
  actionFailures: [],
  job: null,
  jobError: '',
  boardFilterKey: '',
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
    total_all: 0,
    ungrouped_count: 0,
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
let observers = 0
let pollTimer = 0
let pollSequence = 0
let pollFailures = 0
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
    if (timedOut) throw new Error(action
      ? '请求超时，服务端可能仍在处理，请查看任务状态后再操作'
      : '读取自选股超时，请重试')
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
    total_all: Number(board.total_all) || 0,
    ungrouped_count: Number(board.ungrouped_count) || 0,
    default_buy_date: board.default_buy_date || '',
    summary: board.summary || state.board.summary,
    generated_at: board.generated_at || '',
  }
  if (board.days) state.days = Number(board.days)
}

function currentFilterKey() {
  return JSON.stringify([state.days, state.groupId, state.q])
}

async function loadBoard({ background = false } = {}) {
  const sequence = ++loadSequence
  const filterKey = currentFilterKey()
  state.loading = !background || state.boardFilterKey !== filterKey
  state.loadError = ''
  try {
    const params = new URLSearchParams({ days: String(state.days), q: state.q })
    if (state.groupId) params.set('group_id', state.groupId)
    const board = await api(`/api/watchlist/board?${params}`, { key: 'watchlist-board' })
    if (sequence !== loadSequence) return false
    applyBoard(board)
    state.boardFilterKey = filterKey
    state.loaded = true
    window.dispatchEvent(new CustomEvent('niuone:last-updated', {
      detail: { value: String(board.generated_at || '').slice(11, 19) || '--' },
    }))
    return true
  } catch (error) {
    if (sequence !== loadSequence || error?.name === 'AbortError') return false
    state.loadError = error instanceof Error ? error.message : String(error)
    return false
  } finally {
    if (sequence === loadSequence) state.loading = false
  }
}

function acceptJob(job) {
  const previous = state.job
  state.job = job || null
  state.jobError = ''
  return job && (previous?.id !== job.id || previous?.processed !== job.processed
    || previous?.status !== job.status)
}

function scheduleJobPoll(delay) {
  window.clearTimeout(pollTimer)
  if (observers > 0) pollTimer = window.setTimeout(pollJob, delay)
}

async function pollJob() {
  const sequence = ++pollSequence
  try {
    const result = await api('/api/watchlist/jobs/latest', {
      key: 'watchlist-job', timeoutMs: 15000,
    })
    if (sequence !== pollSequence || observers === 0) return
    const changed = acceptJob(result.job)
    pollFailures = 0
    if (changed && (result.job.processed > 0 || !isJobActive(result.job))) {
      await loadBoard({ background: true })
    }
  } catch (error) {
    if (sequence !== pollSequence || observers === 0) return
    state.jobError = '暂时无法读取任务进度，将自动重试；后台任务可能仍在进行。'
    pollFailures += 1
  } finally {
    if (sequence === pollSequence && observers > 0) {
      scheduleJobPoll(pollFailures ? Math.min(15000, 2000 * 2 ** pollFailures)
        : isJobActive(state.job) ? 2000 : 15000)
    }
  }
}

function startJobPolling() {
  observers += 1
  if (observers === 1) return pollJob()
}

function stopJobPolling() {
  observers = Math.max(0, observers - 1)
  if (observers) return
  window.clearTimeout(pollTimer)
  pollSequence += 1
  loadSequence += 1
  controllers.get('watchlist-job')?.abort()
  controllers.get('watchlist-board')?.abort()
  state.loading = false
}

async function runAction(label, fn, { reload = true } = {}) {
  if (state.busy) return { ok: false, error: 'busy' }
  state.busy = true
  state.status = label
  state.error = ''
  state.actionFailures = []
  try {
    const result = await fn()
    state.actionFailures = Array.isArray(result?.failed) ? result.failed : []
    if (result?.job) {
      pollSequence += 1
      controllers.get('watchlist-job')?.abort()
      acceptJob(result.job)
    }
    // Mutation responses may contain a board with default filters. Always read
    // the user's current selection, including changes made during the action.
    if (reload) await loadBoard({ background: true })
    state.status = result?.message || '完成'
    if (state.loadError) state.status += '，列表刷新失败，请重试刷新'
    return { ok: true, result }
  } catch (error) {
    if (isAdminRequired(error)) {
      state.status = '需要管理员验证'
      return { ok: false, needAdmin: true, error }
    }
    state.error = error instanceof Error ? error.message : String(error)
    state.status = '操作未完成'
    return { ok: false, error }
  } finally {
    state.busy = false
    scheduleJobPoll(0)
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
    message: `已新增 ${(result.added || []).length} 只 · 已有 ${(result.skipped_existing || []).length} 只已跳过`
      + ((result.failed || []).length ? ` · ${(result.failed || []).length} 只待补齐，详见失败项` : ''),
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

function submitJob(kind, fields = {}) {
  if (isJobActive(state.job)) return Promise.resolve({ ok: false, error: '任务进行中' })
  return runAction('提交行情任务…', () => api('/api/watchlist/jobs', {
    method: 'POST', action: true, body: { kind, ...fields }, key: 'watchlist-start-job',
  }).then(result => ({ ...result, message: '已提交后台处理，可继续筛选和浏览' })), { reload: false })
}

function updateToday() {
  return submitJob('update-today')
}

function backfill({ days = 60, missingOnly = true, codes } = {}) {
  return submitJob('backfill', { days, missing_only: missingOnly, ...(codes ? { codes } : {}) })
}

function retryJob() {
  if (!state.job?.id || isJobActive(state.job)) return Promise.resolve({ ok: false })
  return runAction('重试未完成股票…', () => api(`/api/watchlist/jobs/${state.job.id}/retry`, {
    method: 'POST', action: true, body: {}, key: 'watchlist-retry-job',
  }).then(result => ({ ...result, message: '已提交重试，成功项会保留' })), { reload: false })
}

export function useWatchlistData() {
  return {
    state,
    loadBoard,
    currentFilterKey,
    startJobPolling,
    stopJobPolling,
    retryJob,
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
