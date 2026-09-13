const ERROR_LABELS = Object.freeze({
  concurrency_timeout: '请求排队超时',
  http_403: 'NewsNow 拒绝了当前请求',
  http_429: 'NewsNow 请求过于频繁',
  invalid_configuration: '财经快讯配置无效',
  invalid_content_type: 'NewsNow 返回了非 JSON 内容',
  invalid_json: 'NewsNow 返回格式异常',
  invalid_response: 'NewsNow 返回字段不完整',
  network_error: '无法连接 NewsNow',
  realtime_news_disabled: '财经快讯尚未启用',
  request_failed: 'NewsNow 请求失败',
  response_too_large: 'NewsNow 返回内容过大',
  unexpected_error: '财经快讯服务异常',
})

export function realtimeNewsErrorText(value) {
  const code = String(value || '').trim()
  return ERROR_LABELS[code] || code || '暂时无法获取财经快讯'
}

export function realtimeNewsStatusText(status) {
  const value = String(status || '').trim()
  if (value === 'success') return '来源正常'
  if (value === 'partial') return '部分来源可用'
  if (value === 'cache') return '使用最近缓存'
  if (value === 'disabled') return '尚未启用'
  if (value === 'invalid_configuration') return '配置无效'
  return '暂不可用'
}

function parsedDate(value, milliseconds) {
  const numeric = Number(milliseconds || 0)
  if (Number.isFinite(numeric) && numeric > 0) return new Date(numeric)
  const parsed = new Date(String(value || ''))
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function realtimeNewsClock(item) {
  const date = parsedDate(item?.published_at, item?.published_at_ms)
  if (!date) return '榜单'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

export function realtimeNewsDate(item) {
  const date = parsedDate(item?.published_at, item?.published_at_ms)
  if (!date) {
    const rank = Number(item?.rank)
    return Number.isInteger(rank) && rank > 0 ? `#${String(rank).padStart(2, '0')}` : ''
  }
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
  }).format(date)
}

export function filterRealtimeNews(items, sourceId = 'all', importantOnly = false) {
  return (Array.isArray(items) ? items : []).filter(item => {
    if (!item || typeof item !== 'object') return false
    if (sourceId !== 'all' && String(item.source_id || '') !== sourceId) return false
    if (importantOnly && item.important !== true) return false
    return true
  })
}

export function realtimeNewsSourceOptions(sources, items) {
  const counts = new Map()
  for (const item of Array.isArray(items) ? items : []) {
    const sourceId = String(item?.source_id || '')
    if (sourceId) counts.set(sourceId, (counts.get(sourceId) || 0) + 1)
  }
  const options = [{ id: 'all', label: '全部', count: Array.isArray(items) ? items.length : 0 }]
  for (const source of Array.isArray(sources) ? sources : []) {
    const id = String(source?.id || '')
    if (!id) continue
    options.push({
      id,
      label: String(source?.label || id),
      count: counts.get(id) || 0,
      stale: source?.stale === true,
    })
  }
  return options
}
