<script setup>
import { computed } from 'vue'

const props = defineProps({
  value: { default: null },
  emptyText: { type: String, default: '暂无有效信号' },
})

const LABELS = {
  direction: '方向', strength: '强度', stage: '阶段', ma_arrangement: '均线排列',
  pattern: '量价形态', confidence: '可信度', volume_ratio: '量比', turnover: '换手率',
  obv_trend: 'OBV 趋势', status: '状态', target_price: '目标价', signal: '信号',
  system: '系统', breakout_price: '突破价', current_n: 'N 值', stop_loss: '止损价',
  entry_price: '入场价', position_units: '仓位单元', exit_price: '离场价',
  channel_high: '通道上轨', channel_low: '通道下轨', next_add_price: '下次加仓价',
  grade: '等级', total: '总分', current_state: '当前结构', summary: '摘要',
  description: '解读', market_source: '市场判定来源', risk_reward_ratio: '盈亏比',
  position_size: '建议仓位', holding_period: '持有周期', max_loss_pct: '最大损失',
  action: '操作', notes: '备注', count: '数量', row_count: 'K 线条数',
  rejected_row_count: '剔除异常行', missing_volume_count: '缺失成交量',
  quote_available: '实时行情', fund_flow_available: '资金流数据', index_available: '大盘数据',
  degraded: '降级状态', kline_source: 'K 线来源', kline_count: 'K 线条数',
  last_kline_date: '最新 K 线', latest_bar_status: '最新行状态', generated_at: '生成时间',
}

const HIDDEN = new Set([
  'signals', 'warnings', 'scores', 'ma_scores', 'moving_averages', 'trendline', 'fractals', 'strokes',
  'zhongshus', 'cup_handle', 'key_levels', 'counts',
])

function humanize(key) {
  return LABELS[key] || String(key).replaceAll('_', ' ')
}

function displayValue(value, key = '') {
  if (typeof value === 'boolean') return value ? '可用' : '不可用'
  if (value === null || value === undefined || value === '') return '--'
  if (typeof value === 'number') {
    if (key.includes('pct') || key === 'turnover') return `${Number(value.toFixed(2))}%`
    return Number.isInteger(value) ? value.toLocaleString('zh-CN') : Number(value.toFixed(3)).toLocaleString('zh-CN')
  }
  if (typeof value === 'object') return ''
  return String(value)
}

const source = computed(() => props.value)
const items = computed(() => {
  if (!source.value || typeof source.value !== 'object' || Array.isArray(source.value)) return []
  return Object.entries(source.value)
    .filter(([key, value]) => !HIDDEN.has(key) && value !== null && value !== undefined && value !== '' && typeof value !== 'object')
    .map(([key, value]) => ({ key, label: humanize(key), value: displayValue(value, key) }))
})
const textLines = computed(() => {
  if (Array.isArray(source.value)) {
    return source.value.map(item => typeof item === 'object'
      ? String(item.description || item.summary || item.name || item.signal || '')
      : String(item || '')).filter(Boolean)
  }
  if (typeof source.value === 'string' || typeof source.value === 'number') return [String(source.value)]
  const record = source.value && typeof source.value === 'object' ? source.value : {}
  const signals = Array.isArray(record.signals) ? record.signals : []
  const warnings = Array.isArray(record.warnings) ? record.warnings : []
  return [...signals, ...warnings].map(item => typeof item === 'object'
    ? String(item.description || item.summary || item.signal || item.name || '')
    : String(item || '')).filter(Boolean)
})
const nestedItems = computed(() => {
  if (!source.value || typeof source.value !== 'object' || Array.isArray(source.value)) return []
  const result = []
  for (const key of ['scores', 'ma_scores', 'moving_averages', 'counts', 'key_levels']) {
    const record = source.value[key]
    if (!record || typeof record !== 'object' || Array.isArray(record)) continue
    for (const [nestedKey, value] of Object.entries(record)) {
      const formatted = displayValue(value, nestedKey)
      if (!formatted) continue
      result.push({ key: `${key}-${nestedKey}`, label: humanize(nestedKey), value: formatted })
    }
  }
  return result
})
const recordCards = computed(() => {
  if (!Array.isArray(source.value)) return []
  return source.value.filter(item => item && typeof item === 'object').slice(0, 8).map((item, index) => ({
    key: `${item.name || item.system || item.date || 'record'}-${index}`,
    title: String(item.name || item.system || item.signal || item.date || `记录 ${index + 1}`),
    summary: String(item.description || item.summary || item.status || ''),
  }))
})
const hasContent = computed(() => items.value.length || nestedItems.value.length || textLines.value.length || recordCards.value.length)
</script>

<template>
  <div v-if="hasContent" class="technical-detail-content">
    <dl v-if="items.length" class="technical-detail-grid">
      <div v-for="item in items" :key="item.key">
        <dt>{{ item.label }}</dt>
        <dd>{{ item.value }}</dd>
      </div>
    </dl>
    <dl v-if="nestedItems.length" class="technical-detail-grid technical-detail-nested">
      <div v-for="item in nestedItems" :key="item.key">
        <dt>{{ item.label }}</dt>
        <dd>{{ item.value }}</dd>
      </div>
    </dl>
    <div v-if="recordCards.length" class="technical-record-list">
      <article v-for="record in recordCards" :key="record.key">
        <b>{{ record.title }}</b><span v-if="record.summary">{{ record.summary }}</span>
      </article>
    </div>
    <ul v-if="textLines.length" class="technical-signal-list">
      <li v-for="(line, index) in textLines" :key="`${line}-${index}`">{{ line }}</li>
    </ul>
  </div>
  <p v-else class="technical-empty-copy">{{ emptyText }}</p>
</template>
