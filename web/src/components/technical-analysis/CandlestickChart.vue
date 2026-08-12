<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  markers: { type: Array, default: () => [] },
  levels: { type: Object, default: () => ({}) },
})

const width = 900
const height = 286
const plot = { left: 58, right: 14, top: 16, priceBottom: 218, volumeTop: 230, bottom: 268 }
const MIN_VISIBLE_ROWS = 24
const DEFAULT_VISIBLE_ROWS = 80
const MAX_VISIBLE_ROWS = 160
const visibleCount = ref(DEFAULT_VISIBLE_ROWS)
const hoverIndex = ref(-1)
const visibleRows = computed(() => props.rows.slice(-Math.min(MAX_VISIBLE_ROWS, Math.max(MIN_VISIBLE_ROWS, visibleCount.value))))
const canZoomIn = computed(() => visibleRows.value.length > MIN_VISIBLE_ROWS)
const canZoomOut = computed(() => visibleRows.value.length < Math.min(MAX_VISIBLE_ROWS, props.rows.length))

function zoomIn() {
  visibleCount.value = Math.max(MIN_VISIBLE_ROWS, visibleCount.value - 8)
  hoverIndex.value = -1
}

function zoomOut() {
  visibleCount.value = Math.min(MAX_VISIBLE_ROWS, visibleCount.value + 8)
  hoverIndex.value = -1
}

function resetZoom() {
  visibleCount.value = Math.min(DEFAULT_VISIBLE_ROWS, Math.max(MIN_VISIBLE_ROWS, props.rows.length))
  hoverIndex.value = -1
}

function onWheel(event) {
  if (event.deltaY < 0) zoomIn()
  else if (event.deltaY > 0) zoomOut()
}

function finite(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

const geometry = computed(() => {
  const rows = visibleRows.value
  if (!rows.length) return { candles: [], grid: [], labels: [], markers: [], levels: [], maxVolume: 0 }
  const low = Math.min(...rows.map(row => finite(row.low)))
  const high = Math.max(...rows.map(row => finite(row.high)))
  const span = Math.max(0.01, high - low)
  const innerWidth = width - plot.left - plot.right
  const step = innerWidth / rows.length
  const bodyWidth = Math.max(2, Math.min(8, step * 0.64))
  const priceY = value => plot.top + ((high - finite(value)) / span) * (plot.priceBottom - plot.top)
  const maxVolume = Math.max(1, ...rows.map(row => finite(row.volume)))
  const candles = rows.map((row, index) => {
    const x = plot.left + step * (index + 0.5)
    const openY = priceY(row.open)
    const closeY = priceY(row.close)
    const bodyTop = Math.min(openY, closeY)
    return {
      ...row,
      index,
      x,
      openY,
      closeY,
      highY: priceY(row.high),
      lowY: priceY(row.low),
      bodyTop,
      bodyHeight: Math.max(1, Math.abs(closeY - openY)),
      bodyWidth,
      volumeHeight: (finite(row.volume) / maxVolume) * (plot.bottom - plot.volumeTop),
      up: finite(row.close) >= finite(row.open),
    }
  })
  const grid = Array.from({ length: 5 }, (_, index) => {
    const ratio = index / 4
    return {
      y: plot.top + ratio * (plot.priceBottom - plot.top),
      value: high - ratio * span,
    }
  })
  const labelIndexes = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])]
  const labels = labelIndexes.map(index => ({
    x: plot.left + step * (index + 0.5),
    date: String(rows[index]?.date || '').slice(5),
  }))
  const visibleDates = new Map(rows.map((row, index) => [String(row.date || '').slice(0, 10), index]))
  const levels = Object.entries(props.levels || {})
    .map(([label, value]) => ({ label: String(label), value: finite(value) }))
    .filter(item => item.value > 0 && item.value >= low && item.value <= high)
    .slice(0, 6)
    .map(item => ({ ...item, y: priceY(item.value) }))
  const markers = props.markers
    .map((marker, markerIndex) => {
      const index = visibleDates.get(String(marker?.date || '').slice(0, 10))
      if (index === undefined) return null
      const candle = candles[index]
      const markerPrice = finite(marker.price)
      const markerY = Math.max(plot.top + 10, Math.min(plot.priceBottom - 10, priceY(markerPrice)))
      const buy = marker.type === 'buy'
      return {
        ...marker,
        key: `${marker.date}-${markerIndex}`,
        x: candle.x,
        y: markerY,
        labelY: buy ? Math.min(plot.priceBottom + 13, markerY + 16) : Math.max(plot.top + 8, markerY - 14),
        buy,
      }
    })
    .filter(Boolean)
  return { candles, grid, labels, markers, levels, maxVolume, low, high }
})

const hovered = computed(() => geometry.value.candles[hoverIndex.value] || null)
const chartSummary = computed(() => {
  const candle = hovered.value || geometry.value.candles.at(-1)
  if (!candle) return null
  const change = candle.open ? ((candle.close / candle.open) - 1) * 100 : 0
  return { ...candle, change }
})

function onPointerMove(event) {
  const target = event.currentTarget
  const rect = target.getBoundingClientRect()
  const localX = ((event.clientX - rect.left) / rect.width) * width
  const candles = geometry.value.candles
  if (!candles.length || localX < plot.left || localX > width - plot.right) {
    hoverIndex.value = -1
    return
  }
  const index = Math.floor(((localX - plot.left) / (width - plot.left - plot.right)) * candles.length)
  hoverIndex.value = Math.max(0, Math.min(candles.length - 1, index))
}

function price(value) {
  const number = finite(value)
  return number ? number.toFixed(number >= 100 ? 2 : 3) : '--'
}

function compactVolume(value) {
  const number = finite(value)
  if (number >= 100000000) return `${(number / 100000000).toFixed(1)}亿`
  if (number >= 10000) return `${(number / 10000).toFixed(1)}万`
  return Math.round(number).toLocaleString('zh-CN')
}
</script>

<template>
  <div class="technical-chart-shell">
    <div class="technical-chart-toolbar" aria-label="K线图缩放控制">
      <span>显示 {{ visibleRows.length }} 根</span>
      <div>
        <button type="button" :disabled="!canZoomIn" aria-label="放大K线图" @click="zoomIn">＋</button>
        <button type="button" :disabled="!canZoomOut" aria-label="缩小K线图" @click="zoomOut">−</button>
        <button type="button" aria-label="重置K线图缩放" @click="resetZoom">重置</button>
      </div>
    </div>
    <div class="technical-chart" @wheel.prevent="onWheel">
    <div v-if="!geometry.candles.length" class="technical-chart-empty">K 线数据暂不可用</div>
    <svg
      v-else
      class="technical-chart-svg"
      :viewBox="`0 0 ${width} ${height}`"
      role="img"
      aria-label="近期 K 线和成交量图"
      @pointermove="onPointerMove"
      @pointerleave="hoverIndex = -1"
    >
      <g class="technical-chart-grid">
        <g v-for="line in geometry.grid" :key="line.y">
          <line :x1="plot.left" :x2="width - plot.right" :y1="line.y" :y2="line.y" />
          <text :x="plot.left - 7" :y="line.y + 4" text-anchor="end">{{ price(line.value) }}</text>
        </g>
        <line :x1="plot.left" :x2="width - plot.right" :y1="plot.volumeTop" :y2="plot.volumeTop" />
      </g>
      <g v-for="level in geometry.levels" :key="level.label" class="technical-chart-level">
        <line :x1="plot.left" :x2="width - plot.right" :y1="level.y" :y2="level.y" />
        <text :x="width - plot.right - 3" :y="level.y - 3" text-anchor="end">{{ level.label }} {{ price(level.value) }}</text>
      </g>
      <g v-for="candle in geometry.candles" :key="`${candle.date}-${candle.index}`" :class="candle.up ? 'chart-up' : 'chart-down'">
        <line class="technical-candle-wick" :x1="candle.x" :x2="candle.x" :y1="candle.highY" :y2="candle.lowY" />
        <rect
          class="technical-candle-body"
          :x="candle.x - candle.bodyWidth / 2"
          :y="candle.bodyTop"
          :width="candle.bodyWidth"
          :height="candle.bodyHeight"
        />
        <rect
          class="technical-volume-bar"
          :x="candle.x - candle.bodyWidth / 2"
          :y="plot.bottom - candle.volumeHeight"
          :width="candle.bodyWidth"
          :height="candle.volumeHeight"
        />
      </g>
      <g v-for="marker in geometry.markers" :key="marker.key" class="technical-chart-marker" :class="marker.buy ? 'marker-buy' : 'marker-sell'">
        <line :x1="marker.x" :x2="marker.x" :y1="marker.y" :y2="marker.labelY" />
        <path :d="marker.buy ? `M ${marker.x - 4} ${marker.labelY - 3} L ${marker.x} ${marker.labelY + 3} L ${marker.x + 4} ${marker.labelY - 3}` : `M ${marker.x - 4} ${marker.labelY + 3} L ${marker.x} ${marker.labelY - 3} L ${marker.x + 4} ${marker.labelY + 3}`" />
        <text :x="marker.x" :y="marker.buy ? marker.labelY + 12 : marker.labelY - 5" text-anchor="middle">{{ marker.label }}</text>
      </g>
      <g class="technical-chart-dates">
        <text v-for="label in geometry.labels" :key="label.x" :x="label.x" y="283" text-anchor="middle">{{ label.date }}</text>
      </g>
      <g v-if="hovered" class="technical-chart-crosshair">
        <line :x1="hovered.x" :x2="hovered.x" :y1="plot.top" :y2="plot.bottom" />
      </g>
    </svg>
    <div v-if="chartSummary" class="technical-chart-summary" aria-live="polite">
      <b>{{ chartSummary.date || '--' }}</b>
      <span>开 {{ price(chartSummary.open) }}</span>
      <span>高 {{ price(chartSummary.high) }}</span>
      <span>低 {{ price(chartSummary.low) }}</span>
      <span>收 {{ price(chartSummary.close) }}</span>
      <span :class="chartSummary.change >= 0 ? 'up-text' : 'down-text'">{{ chartSummary.change >= 0 ? '+' : '' }}{{ chartSummary.change.toFixed(2) }}%</span>
      <span>量 {{ compactVolume(chartSummary.volume) }}</span>
      <span v-if="chartSummary.amount">额 {{ compactVolume(chartSummary.amount) }}</span>
    </div>
    </div>
  </div>
</template>
