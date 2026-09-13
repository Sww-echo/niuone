import { reactive } from 'vue'
import { useDashboardTabs } from './useDashboardTabs.js'
import { startVisiblePolling } from '../utils/visiblePolling.js'

const CATEGORY = 'realtime_news'
const REFRESH_INTERVAL_MS = 30 * 1000
const CACHE_TTL_MS = 10 * 60 * 1000
const REQUEST_TIMEOUT_MS = 30 * 1000
const CACHE_KEY = 'niuniu-dashboard-realtime-news-v1'

const state = reactive({
  loading: true,
  loaded: false,
  enabled: true,
  available: false,
  status: '',
  stale: false,
  overviewImportantOnly: true,
  items: [],
  sources: [],
  generatedAt: '',
  error: '',
})

let users = 0
let stopPolling = null
let requestController = null
let requestSequence = 0
let activeRequest = null

function publishCount() {
  useDashboardTabs().setCategoryCount(CATEGORY, ` · ${state.items.length}`)
}

function publishLastUpdated() {
  window.dispatchEvent(new CustomEvent('niuone:last-updated', {
    detail: { value: String(state.generatedAt || '').slice(11, 19) || '--' },
  }))
}

function payloadSnapshot() {
  return {
    enabled: state.enabled,
    available: state.available,
    status: state.status,
    stale: state.stale,
    overviewImportantOnly: state.overviewImportantOnly,
    items: state.items,
    sources: state.sources,
    generatedAt: state.generatedAt,
    error: state.error,
  }
}

function saveCache() {
  try {
    sessionStorage.setItem(CACHE_KEY, JSON.stringify({ ...payloadSnapshot(), savedAt: Date.now() }))
  } catch {}
}

function applyPayload(payload) {
  state.enabled = payload?.enabled !== false
  state.available = payload?.available === true
  state.status = String(payload?.status || '')
  state.stale = payload?.stale === true
  state.overviewImportantOnly = payload?.overview_important_only !== false
    && payload?.overviewImportantOnly !== false
  state.items = Array.isArray(payload?.items) ? payload.items : []
  state.sources = Array.isArray(payload?.sources) ? payload.sources : []
  state.generatedAt = String(payload?.generated_at || payload?.generatedAt || '')
  state.error = String(payload?.error || '')
  state.loading = false
  state.loaded = true
  publishCount()
  publishLastUpdated()
}

function restoreCache() {
  try {
    const cached = JSON.parse(sessionStorage.getItem(CACHE_KEY) || '{}')
    if (!cached.savedAt || Date.now() - Number(cached.savedAt) > CACHE_TTL_MS) return
    applyPayload(cached)
  } catch {}
}

async function fetchPayload(controller) {
  let timedOut = false
  const timeout = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch('/api/realtime-news', {
      signal: controller.signal,
      credentials: 'same-origin',
      cache: 'no-store',
    })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return await response.json()
  } catch (error) {
    if (timedOut) throw new Error('请求超时')
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

async function refreshRealtimeNews({ background = false } = {}) {
  if (activeRequest) return activeRequest
  const sequence = ++requestSequence
  const controller = new AbortController()
  requestController = controller
  if (!background || !state.items.length) state.loading = true
  const request = fetchPayload(controller)
    .then(payload => {
      if (sequence !== requestSequence) return false
      applyPayload(payload)
      saveCache()
      return true
    })
    .catch(error => {
      if (error?.name === 'AbortError' || sequence !== requestSequence) return false
      state.error = String(error?.message || error)
      state.loading = false
      state.loaded = true
      return false
    })
    .finally(() => {
      if (requestController === controller) requestController = null
      if (activeRequest === request) activeRequest = null
    })
  activeRequest = request
  return request
}

function activateRealtimeNews() {
  users += 1
  if (users > 1) return
  if (state.loaded) refreshRealtimeNews({ background: state.items.length > 0 })
  else refreshRealtimeNews()
  stopPolling = startVisiblePolling(
    () => refreshRealtimeNews({ background: state.items.length > 0 }),
    REFRESH_INTERVAL_MS,
  )
}

function deactivateRealtimeNews() {
  users = Math.max(0, users - 1)
  if (users) return
  stopPolling?.()
  stopPolling = null
  requestSequence += 1
  requestController?.abort()
  requestController = null
  activeRequest = null
}

restoreCache()

export function useRealtimeNewsData() {
  return {
    state,
    activateRealtimeNews,
    deactivateRealtimeNews,
    refreshRealtimeNews,
  }
}
