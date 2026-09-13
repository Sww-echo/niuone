import { reactive } from 'vue'
import { subscribePublicProjection } from './usePublicProjection.js'

const CACHE_TTL_MS = 30 * 1000
const REQUEST_TIMEOUT_MS = 15 * 1000
const CACHE_KEY = 'niuniu-dashboard-today-candidates-v2'
const SECTION_NAME = 'today_candidates'

const state = reactive({
  loading: true,
  loaded: false,
  items: [],
  count: 0,
  currentCount: 0,
  currentDate: '',
  generatedAt: '',
  scanCount: 0,
  strategyMeta: {},
  intradayByCode: {},
  intradayLoading: false,
  intradayLoaded: false,
  intradayGeneratedAt: '',
  intradayError: '',
  error: '',
})

let users = 0
let controller = null
let intradayController = null
let loadSequence = 0
let intradayLoadSequence = 0
let unsubscribeProjection = null
let sectionDigest = ''
let pendingDigest = ''

function publishLastUpdated() {
  window.dispatchEvent(new CustomEvent('niuone:last-updated', {
    detail: { value: String(state.generatedAt || '').slice(11, 19) || '--' },
  }))
}

function saveCache() {
  try {
    sessionStorage.setItem(CACHE_KEY, JSON.stringify({
      items: state.items,
      count: state.count,
      currentCount: state.currentCount,
      currentDate: state.currentDate,
      generatedAt: state.generatedAt,
      scanCount: state.scanCount,
      strategyMeta: state.strategyMeta,
      intradayByCode: state.intradayByCode,
      intradayLoaded: state.intradayLoaded,
      intradayGeneratedAt: state.intradayGeneratedAt,
      sectionDigest,
      savedAt: Date.now(),
    }))
  } catch {}
}

function restoreCache() {
  try {
    const cached = JSON.parse(sessionStorage.getItem(CACHE_KEY) || '{}')
    if (!cached.savedAt || Date.now() - Number(cached.savedAt) > CACHE_TTL_MS) return
    state.items = Array.isArray(cached.items) ? cached.items : []
    state.count = Number(cached.count || state.items.length)
    state.currentCount = Number(cached.currentCount || 0)
    state.currentDate = String(cached.currentDate || '')
    state.generatedAt = String(cached.generatedAt || '')
    state.scanCount = Number(cached.scanCount || 0)
    state.strategyMeta = cached.strategyMeta || {}
    state.intradayByCode = cached.intradayByCode || {}
    state.intradayLoaded = Boolean(cached.intradayLoaded)
    state.intradayGeneratedAt = String(cached.intradayGeneratedAt || '')
    state.loading = false
    state.loaded = true
    sectionDigest = String(cached.sectionDigest || '')
  } catch {}
}

async function fetchPayload(requestController) {
  let timedOut = false
  const timeout = window.setTimeout(() => {
    timedOut = true
    requestController.abort()
  }, REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch('/api/today_candidates', {
      signal: requestController.signal,
      credentials: 'same-origin',
      cache: 'no-store',
    })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return await response.json()
  } catch (error) {
    if (timedOut) throw new Error('今日候选股请求超时')
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

async function fetchIntradayPayload(requestController) {
  let timedOut = false
  const timeout = window.setTimeout(() => {
    timedOut = true
    requestController.abort()
  }, REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch('/api/today_candidates/intraday', {
      signal: requestController.signal,
      credentials: 'same-origin',
      cache: 'no-store',
    })
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return await response.json()
  } catch (error) {
    if (timedOut) throw new Error('候选股分时行情请求超时')
    throw error
  } finally {
    window.clearTimeout(timeout)
  }
}

function applyPayload(payload) {
  const items = Array.isArray(payload?.items) ? payload.items : []
  state.items = items
  state.count = Number(payload?.count || items.length)
  state.currentCount = Number(payload?.current_count || 0)
  state.currentDate = String(payload?.current_date || '')
  state.generatedAt = String(payload?.generated_at || '')
  state.scanCount = Number(payload?.scan_count || 0)
  state.strategyMeta = payload?.strategy_meta || {}
  const codes = new Set(items.map((item) => String(item?.code || '')))
  state.intradayByCode = Object.fromEntries(
    Object.entries(state.intradayByCode).filter(([code]) => codes.has(code)),
  )
  state.error = String(payload?.error || '')
  state.loading = false
  state.loaded = true
  publishLastUpdated()
}

function applyIntradayPayload(payload) {
  const rows = Array.isArray(payload?.items) ? payload.items : []
  state.intradayByCode = Object.fromEntries(
    rows
      .filter((item) => item && item.code && Array.isArray(item.points))
      .map((item) => [String(item.code), item]),
  )
  state.intradayGeneratedAt = String(payload?.generated_at || '')
  state.intradayError = ''
  state.intradayLoading = false
  state.intradayLoaded = true
}

async function loadTodayCandidateIntraday({ background = false } = {}) {
  const sequence = ++intradayLoadSequence
  intradayController?.abort()
  const requestController = new AbortController()
  intradayController = requestController
  state.intradayLoading = true
  if (!background) state.intradayError = ''
  try {
    const payload = await fetchIntradayPayload(requestController)
    if (sequence !== intradayLoadSequence) return false
    applyIntradayPayload(payload || {})
    saveCache()
    return true
  } catch (error) {
    if (error?.name === 'AbortError' || sequence !== intradayLoadSequence) return false
    state.intradayError = String(error?.message || error)
    state.intradayLoading = false
    state.intradayLoaded = true
    return false
  } finally {
    if (intradayController === requestController) intradayController = null
  }
}

async function loadTodayCandidates({ background = false } = {}) {
  const sequence = ++loadSequence
  controller?.abort()
  const requestController = new AbortController()
  controller = requestController
  if (!background || !state.items.length) state.loading = true
  try {
    const payload = await fetchPayload(requestController)
    if (sequence !== loadSequence) return false
    applyPayload(payload || {})
    if (pendingDigest) {
      sectionDigest = pendingDigest
      pendingDigest = ''
    }
    saveCache()
    if (state.items.length) {
      await loadTodayCandidateIntraday({ background: Object.keys(state.intradayByCode).length > 0 })
    }
    return true
  } catch (error) {
    if (error?.name === 'AbortError' || sequence !== loadSequence) return false
    state.error = String(error?.message || error)
    state.loading = false
    state.loaded = true
    return false
  } finally {
    if (controller === requestController) controller = null
  }
}

function handleProjection(snapshot) {
  const digest = String(snapshot?.sectionDigests?.[SECTION_NAME] || '')
  if (!/^[0-9a-f]{64}$/.test(digest)) return
  if (!sectionDigest && state.loaded && !pendingDigest) {
    sectionDigest = digest
    saveCache()
    return
  }
  if (digest === sectionDigest && !pendingDigest) return
  pendingDigest = digest
  if (!controller) loadTodayCandidates({ background: state.items.length > 0 })
}

function activateTodayCandidates() {
  users += 1
  if (users > 1) return
  unsubscribeProjection = subscribePublicProjection(handleProjection)
  if (state.loaded) {
    publishLastUpdated()
    if (state.items.length) {
      loadTodayCandidateIntraday({ background: Object.keys(state.intradayByCode).length > 0 })
    }
  } else loadTodayCandidates()
}

function deactivateTodayCandidates() {
  users = Math.max(0, users - 1)
  if (users) return
  loadSequence += 1
  intradayLoadSequence += 1
  controller?.abort()
  intradayController?.abort()
  controller = null
  intradayController = null
  unsubscribeProjection?.()
  unsubscribeProjection = null
}

restoreCache()

export function useTodayCandidatesData() {
  return {
    state,
    activateTodayCandidates,
    deactivateTodayCandidates,
    refreshTodayCandidates: loadTodayCandidates,
  }
}
