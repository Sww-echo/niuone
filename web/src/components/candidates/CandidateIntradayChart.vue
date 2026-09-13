<script setup>
import { computed } from 'vue'
import { formatPracticeNumber } from '../../utils/practiceCandidateDisplay.js'
import { todayCandidateExpectedOutcome } from '../../utils/todayCandidatesDisplay.js'
import IndexSparkline from '../indices/IndexSparkline.vue'

const props = defineProps({
  item: { type: Object, required: true },
  series: { type: Object, default: null },
  loading: { type: Boolean, default: false },
})

const points = computed(() => Array.isArray(props.series?.points) ? props.series.points : [])
const hasChart = computed(() => points.value.length >= 2)
const lastPrice = computed(() => Number(props.series?.last_price ?? props.item.price))
const lastPct = computed(() => Number(props.series?.last_pct ?? props.item.change_pct))
const changeClass = computed(() => lastPct.value > 0 ? 'up' : lastPct.value < 0 ? 'down' : 'flat')
const changeText = computed(() => Number.isFinite(lastPct.value)
  ? `${lastPct.value > 0 ? '+' : ''}${lastPct.value.toFixed(2)}%`
  : '--')
const updatedTime = computed(() => String(props.series?.updated_at || '').slice(11, 16) || '--')
const expectedOutcome = computed(() => todayCandidateExpectedOutcome(props.item))
const expectedReturnText = computed(() => Number.isFinite(expectedOutcome.value.expectedReturnPct)
  ? `+${formatPracticeNumber(expectedOutcome.value.expectedReturnPct)}%`
  : '--')
const expectedLossText = computed(() => Number.isFinite(expectedOutcome.value.expectedLossPct)
  ? `-${formatPracticeNumber(expectedOutcome.value.expectedLossPct)}%`
  : '--')
const scoreText = computed(() => {
  const score = Number(props.item.best_score ?? props.item.score)
  return Number.isFinite(score)
    ? `${formatPracticeNumber(score)}/${props.item.score_total || 10}`
    : '--'
})
const expectedReturnBasis = computed(() => `${formatPracticeNumber(expectedOutcome.value.targetR)}R 首次止盈`)
const expectedLossBasis = computed(() => {
  const effectiveLoss = Number(props.item.effective_loss_distance_pct)
  return props.item.effective_loss_distance_pct != null && Number.isFinite(effectiveLoss) && effectiveLoss > 0
    ? '含跳空与费用缓冲'
    : '按结构止损计算'
})
function qualificationTime(value) {
  return String(value || '').slice(11, 16)
}

const qualificationMarkers = computed(() => {
  const source = Array.isArray(props.item.qualification_transitions) && props.item.qualification_transitions.length
    ? props.item.qualification_transitions
    : [
        { at: props.item.first_qualified_at, qualified: true, score: props.item.best_score },
      ]
  let qualifiedSeen = 0
  return source.map((point) => {
    const time = qualificationTime(point?.at)
    if (!time) return null
    const qualified = point?.qualified !== false
    const status = qualified
      ? (qualifiedSeen++ ? '重新达标' : '首次达标')
      : '转为未达标'
    return {
      time,
      kind: qualified ? 'qualified' : 'missed',
      shortLabel: qualified ? '达标' : '未达标',
      label: `${time} ${status}`,
    }
  }).filter(Boolean)
})

function qualificationProgress(time) {
  const match = String(time || '').match(/(\d{1,2}):(\d{2})/)
  if (!match) return null
  const minutes = Number(match[1]) * 60 + Number(match[2])
  const morningStart = 9 * 60 + 30
  const morningEnd = 11 * 60 + 30
  const afternoonStart = 13 * 60
  const afternoonEnd = 15 * 60
  let elapsed = 0
  if (minutes <= morningStart) elapsed = 0
  else if (minutes <= morningEnd) elapsed = minutes - morningStart
  else if (minutes < afternoonStart) elapsed = 120
  else if (minutes <= afternoonEnd) elapsed = 120 + minutes - afternoonStart
  else elapsed = 240
  return Math.max(0, Math.min(100, elapsed / 240 * 100))
}

const qualificationGuides = computed(() => qualificationMarkers.value.map((marker) => {
  const progress = qualificationProgress(marker.time)
  if (progress == null) return null
  return {
    ...marker,
    left: `${progress}%`,
    edge: progress <= 5 ? 'edge-start' : progress >= 95 ? 'edge-end' : '',
  }
}).filter(Boolean))
const chartItem = computed(() => ({
  ...props.item,
  market_type: 'a_stock',
  minute_line: points.value,
  prev_close: props.series?.prev_close,
  price: Number.isFinite(lastPrice.value) ? lastPrice.value : props.item.price,
  change_pct: Number.isFinite(lastPct.value) ? lastPct.value : props.item.change_pct,
  markers: [],
}))
const ariaLabel = computed(() => {
  const stock = `${props.item.code || ''} ${props.item.name || ''}`.trim()
  return hasChart.value
    ? `${stock}今日分时，最新价${formatPracticeNumber(lastPrice.value)}，涨跌幅${changeText.value}，金黄色竖线标注达标、蓝紫色竖线标注未达标，共${qualificationMarkers.value.length}个状态切换点`
    : `${stock}今日分时暂不可用`
})
</script>

<template>
  <div class="candidate-market-strip">
    <div class="candidate-key-facts" aria-label="候选股最佳评分、预期收益与损失">
      <div class="candidate-key-fact candidate-score">
        <span>最佳评分</span>
        <strong>{{ scoreText }}</strong>
        <small>策略综合评价</small>
      </div>
      <div class="candidate-key-fact expected-return">
        <span>预期收益</span>
        <strong>{{ expectedReturnText }}</strong>
        <small>{{ expectedReturnBasis }}</small>
      </div>
      <div class="candidate-key-fact expected-loss">
        <span>预期损失</span>
        <strong>{{ expectedLossText }}</strong>
        <small>{{ expectedLossBasis }}</small>
      </div>
    </div>
    <div class="candidate-intraday" :class="changeClass" role="img" :aria-label="ariaLabel">
      <div class="candidate-intraday-plot">
        <template v-if="hasChart">
          <IndexSparkline :item="chartItem" aria-hidden="true" />
          <div v-if="qualificationGuides.length" class="candidate-intraday-guides" aria-hidden="true">
            <div
              v-for="guide in qualificationGuides"
              :key="`${guide.time}-${guide.kind}`"
              class="candidate-intraday-guide"
              :class="[guide.kind, guide.edge]"
              :style="{ left: guide.left }"
            >
              <span>{{ guide.shortLabel }}</span>
            </div>
          </div>
        </template>
        <div v-else-if="loading" class="candidate-intraday-skeleton" aria-hidden="true"></div>
        <span v-else class="candidate-intraday-empty">暂无有效分时数据</span>
      </div>
      <div class="candidate-intraday-quote">
        <strong>{{ formatPracticeNumber(lastPrice) }}</strong>
        <span :class="changeClass">{{ changeText }}</span>
        <small v-if="hasChart">{{ loading ? '更新中' : updatedTime }}</small>
        <small v-else>{{ loading ? '加载中' : '暂无行情' }}</small>
      </div>
    </div>
  </div>
</template>

<style scoped>
.candidate-market-strip {
  background: color-mix(in srgb, var(--panel2) 58%, var(--panel));
  border-top: 1px solid var(--line);
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  min-width: 0;
}

.candidate-key-facts {
  align-items: stretch;
  border-right: 1px solid var(--line);
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  min-width: 0;
  padding: 6px 4px;
}

.candidate-key-fact {
  align-content: center;
  border-left: 1px solid var(--line);
  display: grid;
  gap: 1px;
  min-width: 0;
  padding: 1px 9px;
}

.candidate-key-fact:first-child {
  border-left: 0;
}

.candidate-key-fact span,
.candidate-key-fact small {
  color: var(--muted);
  font-size: 9px;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.candidate-key-fact strong {
  color: var(--text);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.candidate-key-fact.expected-return strong { color: var(--red); }
.candidate-key-fact.expected-loss strong { color: var(--green); }
.candidate-key-fact.candidate-score strong { color: var(--accent-text); }

.candidate-intraday {
  align-items: center;
  display: grid;
  gap: 8px;
  grid-template-columns: minmax(110px, 1fr) 70px;
  min-height: 54px;
  min-width: 0;
  padding: 6px 10px 7px;
}

.candidate-intraday-quote {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.candidate-intraday-plot {
  align-items: center;
  display: flex;
  min-height: 42px;
  min-width: 0;
  position: relative;
}

.candidate-intraday-plot :deep(.sparkline) {
  color: var(--muted);
  display: block;
  height: 42px;
  margin: 0;
  width: 100%;
}

.candidate-intraday.up .candidate-intraday-plot :deep(.sparkline) {
  color: var(--red);
}

.candidate-intraday.down .candidate-intraday-plot :deep(.sparkline) {
  color: var(--green);
}

.candidate-intraday-plot :deep(.sparkline-zero) {
  stroke: var(--chart-zero);
}

.candidate-intraday-guides {
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  position: absolute;
}

.candidate-intraday-guide {
  bottom: 1px;
  color: var(--yellow);
  position: absolute;
  top: 1px;
  width: 0;
}

.candidate-intraday-guide::before {
  background: currentColor;
  bottom: 0;
  content: '';
  left: -.5px;
  opacity: .72;
  position: absolute;
  top: 0;
  width: 1px;
}

.candidate-intraday-guide span {
  background: color-mix(in srgb, var(--yellow) 22%, var(--panel));
  border: 0;
  border-radius: 4px;
  box-shadow: 0 1px 2px color-mix(in srgb, var(--yellow) 20%, transparent);
  color: var(--yellow-text);
  font-size: 8px;
  font-weight: 900;
  left: 0;
  line-height: 1;
  padding: 2px 4px;
  position: absolute;
  top: 0;
  transform: translateX(-50%);
  white-space: nowrap;
  z-index: 1;
}

.candidate-intraday-guide.missed { color: #7357c9; }
.candidate-intraday-guide.missed span {
  background: color-mix(in srgb, #7357c9 18%, var(--panel));
  bottom: 0;
  box-shadow: 0 1px 2px color-mix(in srgb, #7357c9 18%, transparent);
  color: #6548bd;
  top: auto;
}

.candidate-intraday-guide.edge-start span { transform: none; }
.candidate-intraday-guide.edge-end span { transform: translateX(-100%); }

.candidate-intraday-skeleton {
  background: linear-gradient(
    100deg,
    color-mix(in srgb, var(--line) 48%, transparent) 20%,
    color-mix(in srgb, var(--accent) 24%, transparent) 48%,
    color-mix(in srgb, var(--line) 48%, transparent) 76%
  );
  background-size: 220% 100%;
  clip-path: polygon(0 72%, 12% 60%, 23% 67%, 35% 36%, 49% 52%, 63% 28%, 77% 44%, 89% 22%, 100% 35%, 100% 48%, 89% 35%, 77% 57%, 63% 41%, 49% 65%, 35% 49%, 23% 80%, 12% 73%, 0 85%);
  height: 38px;
  width: 100%;
  animation: candidate-intraday-loading 1.4s ease-in-out infinite;
}

.candidate-intraday-empty {
  color: var(--muted);
  font-size: 10px;
  text-align: center;
  width: 100%;
}

.candidate-intraday-quote {
  justify-items: end;
  text-align: right;
}

.candidate-intraday-quote strong {
  color: var(--text);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.candidate-intraday-quote span {
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  font-weight: 750;
}

.candidate-intraday-quote small {
  color: var(--muted);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.candidate-intraday-quote span.up { color: var(--red); }
.candidate-intraday-quote span.down { color: var(--green); }

:global(html[data-theme="tongdaxin"] .candidate-market-strip) {
  background: var(--panel2);
}

:global(html[data-theme="tongdaxin"] .candidate-intraday) {
  gap: 8px;
  min-height: 48px;
  padding-bottom: 3px;
  padding-top: 3px;
}

:global(html[data-theme="dark"] .candidate-intraday-guide.missed) {
  color: #c5b5ff;
}

:global(html[data-theme="dark"] .candidate-intraday-guide.missed span) {
  background: color-mix(in srgb, #b39aff 22%, var(--panel));
  color: #d3c7ff;
}

:global(html[data-tongdaxin-palette="light"] .candidate-market-strip) {
  background: var(--panel2);
}

@keyframes candidate-intraday-loading {
  from { background-position: 100% 0; }
  to { background-position: -100% 0; }
}

@media (prefers-reduced-motion: reduce) {
  .candidate-intraday-skeleton { animation: none; }
}

@media (max-width: 760px) {
  .candidate-market-strip {
    grid-template-columns: minmax(0, 1fr);
  }

  .candidate-key-facts {
    border-bottom: 1px solid var(--line);
    border-right: 0;
  }

  .candidate-intraday {
    min-height: 58px;
    padding: 6px 8px;
  }
}

@media (max-width: 560px) {
  .candidate-key-facts {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    padding: 5px 2px;
  }

  .candidate-key-fact {
    min-height: 42px;
    padding-inline: 5px;
  }

  .candidate-key-fact span,
  .candidate-key-fact small {
    font-size: 8px;
  }

  .candidate-key-fact strong {
    font-size: 11px;
  }

  .candidate-intraday {
    gap: 7px;
    grid-template-columns: minmax(90px, 1fr) 58px;
    padding-left: 7px;
    padding-right: 7px;
  }

  .candidate-intraday-quote strong { font-size: 12px; }
}
</style>
