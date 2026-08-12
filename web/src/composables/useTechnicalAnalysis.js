import { computed, onBeforeUnmount, reactive, ref } from 'vue'

const ANALYZE_TIMEOUT_MS = 30 * 1000
const SCAN_REQUEST_TIMEOUT_MS = 20 * 1000
const SCAN_POLL_INTERVAL_MS = 1500
const MAX_SCAN_POLL_FAILURES = 3
const TERMINAL_SCAN_STATUSES = new Set(['done', 'error', 'cancelled'])

const SCORE_ALIASES = {
  trend: ['trend', 'trend_score', '趋势'],
  volume: ['volume', 'volume_price', 'volume_score', 'volume_price_score', '量价', '量能'],
  pattern: ['pattern', 'patterns', 'pattern_score', '形态'],
  breakout: ['breakout', 'breakouts', 'breakout_score', '突破'],
  canslim: ['canslim', 'CAN_SLIM', 'can_slim', 'canslim_score', 'can_slim_score'],
}

export const TECHNICAL_SCORE_CARDS = [
  { key: 'trend', label: '趋势' },
  { key: 'volume', label: '量能' },
  { key: 'pattern', label: '形态' },
  { key: 'breakout', label: '突破' },
  { key: 'canslim', label: 'CAN SLIM' },
]

function objectValue(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

function arrayValue(value) {
  if (Array.isArray(value)) return value
  if (value === null || value === undefined || value === '') return []
  return [value]
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

function firstValue(source, keys, fallback = undefined) {
  const record = objectValue(source)
  for (const key of keys) {
    if (!Object.hasOwn(record, key)) continue
    const value = record[key]
    if (value !== null && value !== undefined && value !== '') return value
  }
  return fallback
}

function unwrapPayload(payload) {
  let current = objectValue(payload)
  for (let depth = 0; depth < 3; depth += 1) {
    const nested = firstValue(current, ['data', 'payload'])
    if (!nested || typeof nested !== 'object' || Array.isArray(nested)) break
    current = nested
  }
  return current
}

function normalizeStringList(value) {
  const values = []
  const seen = new Set()
  for (const item of arrayValue(value)) {
    let text = ''
    if (item && typeof item === 'object') {
      text = String(firstValue(item, [
        'summary', 'message', 'description', 'reason', 'signal', 'name', 'label', 'value',
      ], '')).trim()
    } else {
      text = String(item ?? '').trim()
    }
    if (!text || seen.has(text)) continue
    seen.add(text)
    values.push(text)
  }
  return values
}

function normalizeKline(row) {
  if (Array.isArray(row)) {
    const [date, open, close, high, low, volume] = row
    return normalizeKline({ date, open, close, high, low, volume })
  }
  const source = objectValue(row)
  const open = finiteNumber(firstValue(source, ['open', 'o']))
  const close = finiteNumber(firstValue(source, ['close', 'c', 'price']))
  const high = finiteNumber(firstValue(source, ['high', 'h']))
  const low = finiteNumber(firstValue(source, ['low', 'l']))
  if ([open, close, high, low].some(value => value === null)) return null
  return {
    date: String(firstValue(source, ['date', 'time', 'datetime', 'day'], '')).slice(0, 10),
    open,
    close,
    high: Math.max(high, open, close),
    low: Math.min(low, open, close),
    volume: Math.max(0, finiteNumber(firstValue(source, ['volume', 'vol', 'amount_volume'])) || 0),
    pct: finiteNumber(firstValue(source, ['pct', 'change_pct', 'pct_chg'])),
  }
}

function normalizedScoreValue(value) {
  if (value && typeof value === 'object') {
    return finiteNumber(firstValue(value, ['score', 'value', 'points', 'rating']))
  }
  return finiteNumber(value)
}

function scoreMaximum(value, score) {
  const explicit = value && typeof value === 'object'
    ? finiteNumber(firstValue(value, ['max_score', 'maximum', 'max', 'total']))
    : null
  if (explicit && explicit > 0) return explicit
  return 100
}

function scoreTone(score, maximum) {
  if (score === null) return 'neutral'
  const ratio = maximum > 0 ? score / maximum : 0
  if (ratio >= 0.7) return 'positive'
  if (ratio < 0.45) return 'negative'
  return 'neutral'
}

export function scoreFor(moduleScores, key) {
  const scores = objectValue(moduleScores)
  const alias = SCORE_ALIASES[key]?.find(name => Object.hasOwn(scores, name))
  const raw = alias ? scores[alias] : null
  const score = normalizedScoreValue(raw)
  const maximum = scoreMaximum(raw, score)
  const summary = raw && typeof raw === 'object'
    ? String(firstValue(raw, ['verdict', 'summary', 'label', 'status', 'signal'], '')).trim()
    : ''
  return {
    score,
    maximum,
    progress: score === null ? 0 : Math.max(0, Math.min(100, (score / maximum) * 100)),
    summary,
    tone: scoreTone(score, maximum),
  }
}

export function displayScore(score) {
  const number = finiteNumber(score)
  if (number === null) return '--'
  return Number.isInteger(number) ? String(number) : number.toFixed(1)
}

export function normalizeAnalysisPayload(payload) {
  const envelope = unwrapPayload(payload)
  const source = Object.keys(objectValue(envelope.analysis)).length
    ? { ...envelope, ...objectValue(envelope.analysis) }
    : envelope
  const signal = objectValue(firstValue(source, ['signal', 'technical_signal', 'analysis_result'], {}))
  const quote = objectValue(firstValue(source, ['quote', 'snapshot'], {}))
  const marketQuality = objectValue(firstValue(source, ['data_quality', 'quality'], {}))
  const klineSource = firstValue(source, ['klines', 'kline', 'candles', 'ohlcv'], [])
  const klines = arrayValue(klineSource).map(normalizeKline).filter(Boolean)
  const moduleScores = objectValue(firstValue(signal, ['module_scores', 'scores'], {}))
  const modules = objectValue(firstValue(signal, ['modules'], {}))
  const nestedSignals = objectValue(firstValue(signal, ['signals'], {}))
  const nestedRisk = objectValue(firstValue(signal, ['risk'], {}))
  const signalQuality = objectValue(firstValue(signal, ['data_quality', 'quality'], {}))
  const dataQuality = {
    ...signalQuality,
    ...marketQuality,
    warnings: [
      ...normalizeStringList(signalQuality.warnings),
      ...normalizeStringList(marketQuality.warnings),
    ].filter((item, index, values) => values.indexOf(item) === index),
  }
  return {
    raw: source,
    symbol: String(firstValue(source, ['symbol', 'code', 'stock_code'], '')).trim(),
    name: String(firstValue(source, ['name', 'stock_name'], firstValue(quote, ['name'], ''))).trim(),
    period: String(firstValue(source, ['period'], 'day')).toLowerCase(),
    quote: {
      ...quote,
      price: finiteNumber(firstValue(quote, ['price', 'current', 'last', 'close'])),
      previousClose: finiteNumber(firstValue(quote, ['previous_close', 'prev_close', 'pre_close'])),
      changePct: finiteNumber(firstValue(quote, ['change_pct', 'pct', 'pct_chg', 'percent'])),
    },
    klines,
    dataQuality,
    signal: {
      ...signal,
      action: String(firstValue(signal, ['action', 'verdict', 'signal'], '观望')).trim(),
      score: finiteNumber(firstValue(signal, ['score', 'total_score', 'overall_score'])),
      confidence: firstValue(signal, ['confidence', 'confidence_level'], null),
      riskLevel: String(firstValue(signal, ['risk_level', 'risk_grade'], firstValue(nestedRisk, ['level'], ''))).trim(),
      moduleScores,
      trend: firstValue(signal, ['trend', 'trend_analysis'], firstValue(modules, ['trend'], null)),
      volumePrice: firstValue(signal, ['volume_price', 'volume', 'volume_analysis'], firstValue(modules, ['volume_price', 'volume'], null)),
      patterns: firstValue(
        signal,
        ['patterns', 'pattern', 'pattern_analysis'],
        firstValue(objectValue(firstValue(modules, ['patterns', 'pattern'], {})), ['items'], firstValue(modules, ['patterns', 'pattern'], null)),
      ),
      breakouts: firstValue(
        signal,
        ['breakouts', 'breakout', 'breakout_analysis'],
        firstValue(objectValue(firstValue(modules, ['breakouts', 'breakout'], {})), ['systems', 'items'], firstValue(modules, ['breakouts', 'breakout'], null)),
      ),
      canslim: firstValue(signal, ['canslim', 'can_slim'], firstValue(modules, ['canslim', 'can_slim'], null)),
      chanlun: firstValue(signal, ['chanlun', 'chan_lun', 'chan'], firstValue(modules, ['chanlun', 'chan_lun'], null)),
      buySignals: normalizeStringList(firstValue(signal, ['buy_signals', 'bullish_signals'], firstValue(nestedSignals, ['buy'], []))),
      sellSignals: normalizeStringList(firstValue(signal, ['sell_signals', 'bearish_signals'], firstValue(nestedSignals, ['sell'], []))),
      riskWarnings: normalizeStringList(firstValue(signal, ['risk_warnings', 'warnings', 'risks'], firstValue(nestedRisk, ['warnings'], []))),
      keyLevels: firstValue(signal, ['key_levels', 'levels'], null),
      tradePlan: firstValue(signal, ['trade_plan', 'plan'], null),
    },
  }
}

function normalizeScanResult(row, defaultPeriod) {
  const source = objectValue(row)
  const moduleScores = objectValue(firstValue(source, ['module_scores', 'scores'], {}))
  return {
    raw: source,
    symbol: String(firstValue(source, ['symbol', 'code', 'stock_code'], '')).trim(),
    name: String(firstValue(source, ['name', 'stock_name'], '')).trim(),
    price: finiteNumber(firstValue(source, ['price', 'current', 'close'])),
    action: String(firstValue(source, ['action', 'verdict', 'signal'], '观望')).trim(),
    score: finiteNumber(firstValue(source, ['score', 'total_score', 'overall_score'])),
    confidence: firstValue(source, ['confidence', 'confidence_level'], null),
    riskLevel: String(firstValue(source, ['risk_level', 'risk'], '')).trim(),
    period: String(firstValue(source, ['period'], defaultPeriod || 'day')).toLowerCase(),
    moduleScores,
    tradePlan: firstValue(source, ['trade_plan', 'plan'], null),
  }
}

function normalizedProgress(value, scanned, total) {
  const number = finiteNumber(value)
  if (number !== null) return Math.max(0, Math.min(100, number <= 1 ? number * 100 : number))
  if (total > 0) return Math.max(0, Math.min(100, (scanned / total) * 100))
  return 0
}

export function normalizeScanPayload(payload, defaultPeriod = 'day') {
  const source = unwrapPayload(payload)
  const scanned = Math.max(0, Math.trunc(finiteNumber(firstValue(source, ['scanned', 'completed', 'processed'])) || 0))
  const total = Math.max(0, Math.trunc(finiteNumber(firstValue(source, ['total', 'count'])) || 0))
  const status = String(firstValue(source, ['status', 'state'], 'queued')).toLowerCase()
  const resultSource = firstValue(source, ['results', 'items', 'candidates'], [])
  return {
    jobId: String(firstValue(source, ['job_id', 'jobId', 'id'], '')).trim(),
    status,
    progress: normalizedProgress(firstValue(source, ['progress', 'percent', 'percentage']), scanned, total),
    stage: String(firstValue(source, ['stage', 'stage_label', 'message'], '')).trim(),
    scanned,
    total,
    results: arrayValue(resultSource).map(row => normalizeScanResult(row, defaultPeriod)),
    error: String(firstValue(source, ['error', 'error_message'], '')).trim(),
  }
}

async function responsePayload(response) {
  const text = await response.text()
  if (!text) return {}
  try {
    return JSON.parse(text)
  } catch {
    return { error: text.slice(0, 300) }
  }
}

function payloadError(payload, fallback) {
  const source = unwrapPayload(payload)
  const detail = source.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  if (detail && typeof detail === 'object') {
    const message = firstValue(detail, ['message', 'error', 'detail'])
    if (message) return String(message)
  }
  const message = firstValue(source, ['message', 'error', 'error_message'])
  return message ? String(message) : fallback
}

async function fetchJson(url, options, timeoutMs) {
  const controller = new AbortController()
  const upstreamSignal = options?.signal
  const abortFromUpstream = () => controller.abort()
  if (upstreamSignal?.aborted) controller.abort()
  else upstreamSignal?.addEventListener('abort', abortFromUpstream, { once: true })
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(url, {
      credentials: 'same-origin',
      cache: 'no-store',
      ...options,
      signal: controller.signal,
    })
    const payload = await responsePayload(response)
    if (!response.ok) throw new Error(payloadError(payload, `HTTP ${response.status}`))
    return payload
  } finally {
    window.clearTimeout(timeout)
    upstreamSignal?.removeEventListener('abort', abortFromUpstream)
  }
}

function requestErrorMessage(error, timeoutLabel) {
  if (error?.name === 'AbortError') return timeoutLabel
  const message = String(error?.message || error || '')
  if (message === 'invalid_symbol') return '请输入 6 位 A 股代码'
  if (message === 'kline_unavailable') return '暂时无法获取该股 K 线数据'
  if (message === 'kline_insufficient') return '该股历史 K 线数据不足'
  if (message === 'kline_cache_empty') return '本地 K 线缓存为空，暂时无法执行全市场扫描'
  if (message === 'scan_not_found') return '扫描任务已过期或不存在'
  return message || '请求失败，请稍后重试'
}

export function useTechnicalAnalysis() {
  const analysis = ref(null)
  const minuteAnalysis = ref(null)
  const analysisState = reactive({ loading: false, error: '' })
  const scan = reactive({
    jobId: '',
    status: 'idle',
    progress: 0,
    stage: '',
    scanned: 0,
    total: 0,
    results: [],
    error: '',
    starting: false,
  })
  const scanActive = computed(() => scan.starting || ['queued', 'running'].includes(scan.status))
  let analysisController = null
  let minuteController = null
  let scanController = null
  let scanTimer = null
  let pollFailures = 0
  let disposed = false

  function stopScanPolling() {
    if (scanTimer !== null) window.clearTimeout(scanTimer)
    scanTimer = null
    scanController?.abort()
    scanController = null
  }

  async function analyze(symbol, period = 'day') {
    analysisController?.abort()
    const controller = new AbortController()
    analysisController = controller
    analysisState.loading = true
    analysisState.error = ''
    try {
      const params = new URLSearchParams({ symbol: String(symbol), period: String(period) })
      const payload = await fetchJson(
        `/api/technical-analysis/analyze?${params.toString()}`,
        { signal: controller.signal },
        ANALYZE_TIMEOUT_MS,
      )
      if (!disposed && analysisController === controller) analysis.value = normalizeAnalysisPayload(payload)
      return analysis.value
    } catch (error) {
      if (!disposed && analysisController === controller && !controller.signal.aborted) {
        analysisState.error = requestErrorMessage(error, '分析请求超时，请稍后重试')
      }
      return null
    } finally {
      if (!disposed && analysisController === controller) {
        analysisState.loading = false
        analysisController = null
      }
    }
  }

  async function analyzeMinute(symbol) {
    minuteController?.abort()
    minuteController = new AbortController()
    try {
      const params = new URLSearchParams({ symbol: String(symbol) })
      const payload = await fetchJson(
        `/api/technical-analysis/minute?${params.toString()}`,
        { signal: minuteController.signal },
        ANALYZE_TIMEOUT_MS,
      )
      if (!disposed) minuteAnalysis.value = unwrapPayload(payload)
      return minuteAnalysis.value
    } catch (error) {
      if (!disposed && !minuteController?.signal.aborted) analysisState.error = requestErrorMessage(error, '分时请求超时，请稍后重试')
      return null
    }
  }

  function applyScanPayload(payload, period) {
    const normalized = normalizeScanPayload(payload, period)
    if (normalized.jobId) scan.jobId = normalized.jobId
    scan.status = normalized.status
    scan.progress = normalized.progress
    scan.stage = normalized.stage
    scan.scanned = normalized.scanned
    scan.total = normalized.total
    if (normalized.results.length || normalized.status === 'done') scan.results = normalized.results
    scan.error = normalized.error
    return normalized
  }

  async function pollScan(period) {
    if (disposed || !scan.jobId) return
    const controller = new AbortController()
    scanController = controller
    try {
      const payload = await fetchJson(
        `/api/technical-analysis/scans/${encodeURIComponent(scan.jobId)}`,
        { signal: controller.signal },
        SCAN_REQUEST_TIMEOUT_MS,
      )
      pollFailures = 0
      const normalized = applyScanPayload(payload, period)
      if (normalized.status === 'error' && !scan.error) scan.error = '全市场扫描失败'
      if (normalized.status === 'cancelled' && !scan.error) scan.error = '本次扫描已取消'
      if (TERMINAL_SCAN_STATUSES.has(normalized.status)) return
    } catch (error) {
      if (disposed || controller.signal.aborted) return
      pollFailures += 1
      if (pollFailures >= MAX_SCAN_POLL_FAILURES) {
        scan.status = 'error'
        scan.error = requestErrorMessage(error, '扫描进度请求超时')
        return
      }
    } finally {
      if (scanController === controller) scanController = null
    }
    if (!disposed && !TERMINAL_SCAN_STATUSES.has(scan.status)) {
      scanTimer = window.setTimeout(() => pollScan(period), SCAN_POLL_INTERVAL_MS)
    }
  }

  async function startScan(period = 'day') {
    stopScanPolling()
    Object.assign(scan, {
      jobId: '', status: 'queued', progress: 0, stage: '正在创建扫描任务',
      scanned: 0, total: 0, results: [], error: '', starting: true,
    })
    pollFailures = 0
    const controller = new AbortController()
    scanController = controller
    try {
      const payload = await fetchJson('/api/technical-analysis/scans', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ period: String(period), limit: 1000 }),
        signal: controller.signal,
      }, SCAN_REQUEST_TIMEOUT_MS)
      const normalized = applyScanPayload(payload, period)
      if (!normalized.jobId) throw new Error('扫描任务未返回 job_id')
      if (!TERMINAL_SCAN_STATUSES.has(normalized.status)) {
        scanTimer = window.setTimeout(() => pollScan(period), 300)
      }
      return normalized
    } catch (error) {
      if (!disposed && !controller.signal.aborted) {
        scan.status = 'error'
        scan.error = requestErrorMessage(error, '创建扫描任务超时')
      }
      return null
    } finally {
      scan.starting = false
      if (scanController === controller) scanController = null
    }
  }

  onBeforeUnmount(() => {
    disposed = true
    analysisController?.abort()
    minuteController?.abort()
    stopScanPolling()
  })

  return {
    analysis,
    minuteAnalysis,
    analysisState,
    analyze,
    analyzeMinute,
    scan,
    scanActive,
    startScan,
  }
}
