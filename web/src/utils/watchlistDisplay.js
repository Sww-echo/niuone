export function numberOrNull(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null
  if (typeof value !== 'number' && typeof value !== 'string') return null
  if (typeof value === 'string' && !value.trim()) return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function formatPrice(value) {
  const number = numberOrNull(value)
  return number === null ? '—' : number.toFixed(2)
}

export function formatMoney(value) {
  const number = numberOrNull(value)
  return number === null ? '—' : number.toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })
}

export function formatSignedMoney(value) {
  const number = numberOrNull(value)
  return number === null ? '—' : `${number > 0 ? '+' : ''}${formatMoney(number)}`
}

export function formatPct(value) {
  const number = numberOrNull(value)
  return number === null ? '—' : `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

export function toneClass(value) {
  const number = numberOrNull(value)
  return number === null || number === 0 ? '' : number > 0 ? 'up' : 'down'
}

export function dateLabel(value) {
  if (!value) return '—'
  const date = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(date.getTime())) return value
  const week = ['日', '一', '二', '三', '四', '五', '六'][date.getUTCDay()]
  return `${date.getUTCMonth() + 1}.${date.getUTCDate()} 周${week}`
}

export function isJobActive(job) {
  return ['queued', 'running'].includes(job?.status)
}

export function jobStatusLabel(job) {
  return {
    queued: '已排队', running: '进行中', completed: '已完成',
    partial: '部分完成', failed: '未完成', interrupted: '已中断',
  }[job?.status] || ''
}

export function formatDuration(value) {
  const seconds = Math.max(0, Math.floor(numberOrNull(value) || 0))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
