<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  rows: { type: Array, default: () => [] },
})

const width = 900
const height = 286
const plot = { left: 58, right: 14, top: 16, priceBottom: 218, volumeTop: 230, bottom: 268 }
const visibleRows = computed(() => props.rows.slice(-80))
const hoverIndex = ref(-1)

function finite(value) {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

const geometry = computed(() => {
  const rows = visibleRows.value
  if (!rows.length) return { candles: [], grid: [], labels: [], maxVolume: 0 }
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
  return { candles, grid, labels, maxVolume }
})

const hovered = computed(() => geometry.value.candles[hoverIndex.value] || null)

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
  <div class="technical-chart">
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
      <g class="technical-chart-dates">
        <text v-for="label in geometry.labels" :key="label.x" :x="label.x" y="283" text-anchor="middle">{{ label.date }}</text>
      </g>
      <g v-if="hovered" class="technical-chart-crosshair">
        <line :x1="hovered.x" :x2="hovered.x" :y1="plot.top" :y2="plot.bottom" />
      </g>
    </svg>
    <div v-if="hovered" class="technical-chart-tooltip" aria-live="polite">
      <b>{{ hovered.date || '--' }}</b>
      <span>开 {{ price(hovered.open) }}</span>
      <span>高 {{ price(hovered.high) }}</span>
      <span>低 {{ price(hovered.low) }}</span>
      <span>收 {{ price(hovered.close) }}</span>
      <span>量 {{ compactVolume(hovered.volume) }}</span>
    </div>
  </div>
</template>
