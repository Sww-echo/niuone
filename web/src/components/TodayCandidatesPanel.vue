<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useTodayCandidatesData } from '../composables/useTodayCandidatesData.js'
import { practiceCandidateStrategyMeta } from '../utils/practiceCandidateDisplay.js'
import {
  filterAndSortTodayCandidates,
  todayCandidateStrategyOptions,
} from '../utils/todayCandidatesDisplay.js'
import CandidateIntradayChart from './candidates/CandidateIntradayChart.vue'
import PracticeCandidateCard from './practice/PracticeCandidateCard.vue'

const {
  state,
  activateTodayCandidates,
  deactivateTodayCandidates,
  refreshTodayCandidates,
} = useTodayCandidatesData()
const refreshing = ref(false)
const activeStrategy = ref('all')
const sortBy = ref('score')
const strategyMeta = computed(() => practiceCandidateStrategyMeta(state.strategyMeta))
const candidateCount = computed(() => Number(state.count) || state.items.length)
const currentCandidateCount = computed(() => Math.max(0, Number(state.currentCount) || 0))
const strategyOptions = computed(() => todayCandidateStrategyOptions(state.items, strategyMeta.value))
const strategyCount = computed(() => Math.max(0, strategyOptions.value.length - 1))
const filteredItems = computed(() => filterAndSortTodayCandidates(state.items, {
  strategy: activeStrategy.value,
  sortBy: sortBy.value,
}, strategyMeta.value))
const dateLabel = computed(() => {
  const parts = String(state.currentDate || '').split('-')
  return parts.length === 3 ? `${parts[0]}年${Number(parts[1])}月${Number(parts[2])}日` : '今日'
})
const generatedTime = computed(() => String(state.generatedAt || '').slice(11, 19) || '--')
const compactStrategyOptionLabel = (label) => {
  const parts = String(label || '').split(' · ')
  return parts.at(-1) || label
}

async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    await refreshTodayCandidates({ background: state.items.length > 0 })
  } finally {
    refreshing.value = false
  }
}

watch(strategyOptions, (options) => {
  if (!options.some((option) => option.key === activeStrategy.value)) activeStrategy.value = 'all'
})

onMounted(activateTodayCandidates)
onBeforeUnmount(deactivateTodayCandidates)
</script>

<template>
  <section class="today-candidates-page mainline-page" aria-labelledby="todayCandidatesTitle">
    <h2 id="todayCandidatesTitle" class="visually-hidden">今日候选股</h2>
    <section class="today-candidates-toolbar theme-ranking-panel" aria-label="候选股概况与筛选">
      <div class="today-candidates-overview">
        <div class="today-candidates-context" aria-label="今日候选股概况">
          <div class="today-candidates-context-primary">
            <time :datetime="state.currentDate">{{ dateLabel }}</time>
            <strong class="today-candidates-count">当前 {{ currentCandidateCount }}只达标</strong>
          </div>
          <div class="today-candidates-context-secondary">
            <span class="today-candidates-cumulative-count">今日累计 {{ candidateCount }}只曾达标</span>
            <span class="today-candidates-scan-count">{{ state.scanCount }}轮扫描</span>
            <span class="today-candidates-strategy-count">{{ strategyCount }}类策略</span>
            <span class="today-candidates-updated-at">更新 {{ generatedTime }}</span>
          </div>
        </div>
        <button type="button" class="today-candidates-refresh" :disabled="refreshing" @click="refresh">
          {{ refreshing ? '刷新中…' : '刷新' }}
        </button>
      </div>
      <div v-if="state.items.length" class="today-candidates-controls">
        <div class="today-candidates-strategies" role="group" aria-label="按策略筛选">
          <button
            v-for="option in strategyOptions"
            :key="option.key"
            type="button"
            :class="{ active: activeStrategy === option.key }"
            :aria-pressed="activeStrategy === option.key"
            @click="activeStrategy = option.key"
          >
            <span class="today-candidates-strategy-option-label-full">{{ option.label }}</span>
            <span class="today-candidates-strategy-option-label-compact">{{ compactStrategyOptionLabel(option.label) }}</span>
            <span class="today-candidates-strategy-option-count">{{ option.count }}</span>
          </button>
        </div>
        <label class="today-candidates-sort">
          <span class="today-candidates-sort-label">排序</span>
          <select v-model="sortBy" aria-label="候选股排序方式">
            <option value="score">最佳评分</option>
            <option value="recent">最近达标</option>
            <option value="frequency">达标轮次</option>
          </select>
        </label>
      </div>
    </section>

    <div v-if="state.loading && !state.loaded" class="loading">今日候选股加载中...</div>
    <div v-else-if="state.error && !state.items.length" class="empty today-candidates-error">
      ⚠️ 今日候选股暂时无法加载：{{ state.error }}
    </div>
    <template v-else-if="state.items.length">
      <div v-if="state.error" class="industry-flow-notice warning">
        自动更新暂时失败，继续展示缓存结果：{{ state.error }}
      </div>

      <div v-if="filteredItems.length" class="today-candidates-list">
        <div
          v-for="item in filteredItems"
          :key="item.code"
          class="today-candidate-entry"
        >
          <PracticeCandidateCard
            :item="item"
            :strategy-meta="strategyMeta"
          />
          <CandidateIntradayChart
            :item="item"
            :series="state.intradayByCode[item.code]"
            :loading="state.intradayLoading"
          />
        </div>
      </div>
      <div v-else class="empty today-candidates-filter-empty">
        <strong>没有匹配的候选股</strong>
        <span>可以尝试切换策略筛选。</span>
        <button type="button" @click="activeStrategy = 'all'">重置筛选</button>
      </div>
    </template>
    <div v-else class="empty today-candidates-empty">
      <strong>{{ state.scanCount ? '今日扫描尚无达标股票' : '今日尚无候选扫描结果' }}</strong>
      <span>{{ state.scanCount ? '后续扫描出现达标股票后会自动更新。' : '完成第一轮选股扫描后，这里会自动汇总达标记录。' }}</span>
    </div>
  </section>
</template>

<style scoped>
.today-candidates-page {
  --candidate-card-surface: var(--panel);
  --candidate-card-border: var(--line);
  --candidate-card-subtle: var(--panel2);
  --candidate-card-divider: var(--line);
  color: var(--text);
  display: grid;
  gap: 8px;
  min-width: 0;
  padding: 0;
  width: 100%;
}

.today-candidates-toolbar {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
  overflow: hidden;
}

.today-candidates-overview {
  align-items: center;
  display: flex;
  gap: 10px;
  justify-content: space-between;
  min-height: 38px;
  padding: 6px 8px 6px 11px;
}

.today-candidates-context {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  font-size: 11px;
  min-width: 0;
}

.today-candidates-context-primary,
.today-candidates-context-secondary {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
}

.today-candidates-context time,
.today-candidates-context strong,
.today-candidates-context span {
  color: var(--muted);
  font-style: normal;
  font-variant-numeric: tabular-nums;
  line-height: 1.45;
  white-space: nowrap;
}

.today-candidates-context-primary > * + *,
.today-candidates-context-secondary > * + *,
.today-candidates-context-secondary {
  border-left: 1px solid var(--line);
  margin-left: 9px;
  padding-left: 9px;
}

.today-candidates-context time {
  color: var(--text);
  font-weight: 700;
}

.today-candidates-context strong {
  color: var(--accent-text);
  font-weight: 750;
}

.today-candidates-refresh {
  background: var(--panel2);
  border: 1px solid var(--line);
  border-radius: 7px;
  color: var(--text);
  cursor: pointer;
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 700;
  min-height: 27px;
  padding: 0 10px;
}

.today-candidates-refresh:hover:not(:disabled) {
  background: var(--accent-soft);
  border-color: var(--accent-border);
}

.today-candidates-refresh:disabled {
  cursor: wait;
  opacity: .65;
}

.today-candidates-sort select:focus {
  border-color: var(--accent-border);
  outline: 2px solid var(--accent-soft);
}

.today-candidates-controls {
  align-items: center;
  background: var(--panel2);
  border-top: 1px solid var(--line);
  display: flex;
  gap: 8px;
  min-width: 0;
  padding: 5px 8px;
}

.today-candidates-strategies {
  display: flex;
  flex: 1 1 auto;
  gap: 5px;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}

.today-candidates-strategies::-webkit-scrollbar {
  display: none;
}

.today-candidates-strategies button {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--muted);
  flex: 0 0 auto;
  font-size: 10px;
  min-height: 26px;
  padding: 4px 8px;
}

.today-candidates-strategy-option-count {
  color: inherit;
  font-variant-numeric: tabular-nums;
  margin-left: 2px;
  opacity: .78;
}

.today-candidates-strategy-option-label-compact {
  display: none;
}

.today-candidates-strategies button.active {
  background: var(--accent-soft);
  border-color: var(--accent-border);
  color: var(--accent-text);
  font-weight: 700;
}

.today-candidates-sort {
  align-items: center;
  color: var(--muted);
  display: flex;
  flex: 0 0 auto;
  font-size: 10px;
  gap: 5px;
}

.today-candidates-sort select {
  background: var(--panel);
  border-color: var(--line);
  color: var(--text);
  font-size: 10px;
  min-height: 26px;
  padding: 3px 24px 3px 7px;
}

.today-candidates-list {
  display: grid;
  gap: 5px;
}

.today-candidate-entry {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: 0 1px 2px rgba(16, 24, 40, .04);
  min-width: 0;
  overflow: hidden;
  transition: border-color .16s ease, box-shadow .16s ease;
}

.today-candidate-entry:hover {
  border-color: var(--accent-border);
}

.today-candidate-entry :deep(.practice-candidate-card) {
  border: 0;
  border-radius: 0;
  box-shadow: none;
}

.today-candidate-entry :deep(.candidate-tier) {
  display: none;
}

.today-candidate-entry :deep(.candidate-summary),
.today-candidate-entry :deep(.candidate-summary.has-industry) {
  align-items: center;
  grid-template-areas: 'primary industry';
  grid-template-columns: minmax(220px, 1fr) auto;
}

:global(html[data-theme="dark"] .today-candidates-toolbar),
:global(html[data-theme="dark"] .today-candidate-entry) {
  box-shadow: none;
}

:global(html[data-theme="tongdaxin"] .today-candidates-page) {
  gap: 4px;
}

:global(html[data-theme="tongdaxin"] .today-candidates-toolbar),
:global(html[data-theme="tongdaxin"] .today-candidate-entry) {
  border-color: var(--line);
  background: var(--panel);
  box-shadow: none;
}

:global(html[data-theme="tongdaxin"] .today-candidates-controls) {
  border-color: var(--line);
  background: var(--panel2);
}

.today-candidates-empty,
.today-candidates-filter-empty {
  align-items: center;
  display: flex;
  flex-direction: column;
  gap: 7px;
  justify-content: center;
  min-height: 180px;
}

.today-candidates-empty strong,
.today-candidates-filter-empty strong {
  color: var(--text);
  font-size: 15px;
}

.today-candidates-filter-empty button {
  background: var(--accent-soft);
  border-color: var(--accent-border);
  color: var(--accent-text);
  font-size: 11px;
  margin-top: 4px;
  padding: 6px 10px;
}

.today-candidates-error {
  color: var(--red-text);
}

.visually-hidden {
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  height: 1px;
  overflow: hidden;
  position: absolute;
  white-space: nowrap;
  width: 1px;
}

@media (max-width: 760px) {
  .today-candidates-page {
    gap: 6px;
  }

  .today-candidates-toolbar,
  .today-candidate-entry {
    border-radius: 10px;
  }

  .today-candidates-overview {
    align-items: stretch;
    gap: 7px;
    padding: 6px 7px 6px 9px;
  }

  .today-candidates-context {
    flex: 1 1 auto;
    flex-direction: column;
    justify-content: center;
    row-gap: 2px;
  }

  .today-candidates-context-primary,
  .today-candidates-context-secondary {
    flex-wrap: nowrap;
    width: 100%;
  }

  .today-candidates-context-secondary {
    border-left: 0;
    margin-left: 0;
    padding-left: 0;
  }

  .today-candidates-strategy-count {
    display: none;
  }

  .today-candidates-refresh {
    min-height: 32px;
    padding-inline: 11px;
  }

  .today-candidates-controls {
    align-items: center;
    display: grid;
    gap: 6px;
    grid-template-areas: 'strategies sort';
    grid-template-columns: minmax(0, 1fr) auto;
    padding: 6px 7px;
  }

  .today-candidates-strategies {
    grid-area: strategies;
    margin-inline: -1px;
    scroll-padding-inline: 1px;
  }

  .today-candidates-strategies button {
    font-size: 11px;
    min-height: 32px;
    padding-inline: 10px;
  }

  .today-candidates-sort {
    grid-area: sort;
  }

  .today-candidates-sort select {
    font-size: 11px;
    min-height: 34px;
    width: 96px;
  }

  .today-candidates-sort-label {
    display: none;
  }

  .today-candidates-list {
    gap: 6px;
  }

  .today-candidate-entry :deep(.practice-candidate-card) {
    padding: 8px;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-summary),
  .today-candidate-entry :deep(.niuone-candidate-card .candidate-summary.has-industry) {
    align-items: center;
    column-gap: 6px;
    grid-template-areas: 'primary industry';
    grid-template-columns: minmax(0, 1fr) auto;
    row-gap: 0;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-primary) {
    overflow: hidden;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-identity) {
    align-items: center;
    flex-direction: row;
    flex-wrap: nowrap;
    gap: 5px;
    overflow: hidden;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-stock-name) {
    flex: 1 1 auto;
    font-size: 15px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-strategy-badge),
  .today-candidate-entry :deep(.niuone-candidate-card .candidate-industry-badge) {
    flex: 0 0 auto;
    font-size: 10px;
    padding: 2px 6px;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-strategy-label-full),
  .today-candidate-entry :deep(.niuone-candidate-card .candidate-context-label-full) {
    display: none;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-strategy-label-compact),
  .today-candidate-entry :deep(.niuone-candidate-card .candidate-context-label-compact) {
    display: inline;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-industry) {
    flex-wrap: nowrap;
    justify-content: flex-end;
    justify-self: end;
    max-width: 52vw;
    overflow-x: auto;
    scrollbar-width: none;
    width: auto;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-industry-badge) {
    flex: 0 0 auto;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-industry)::-webkit-scrollbar {
    display: none;
  }
}

@media (min-width: 721px) {
  .today-candidates-list {
    gap: 5px;
  }

  .today-candidate-entry :deep(.practice-candidate-card) {
    padding: 9px;
  }

  .today-candidates-controls {
    padding: 5px 8px;
  }
}

@media (max-width: 440px) {
  .today-candidates-context {
    font-size: 10px;
  }

  .today-candidates-context-primary > * + *,
  .today-candidates-context-secondary > * + * {
    margin-left: 6px;
    padding-left: 6px;
  }

  .today-candidates-controls {
    grid-template-areas:
      'strategies'
      'sort';
    grid-template-columns: minmax(0, 1fr);
  }

  .today-candidates-strategies {
    padding-bottom: 1px;
  }

  .today-candidates-strategy-option-label-full {
    display: none;
  }

  .today-candidates-strategy-option-label-compact {
    display: inline;
  }

  .today-candidates-sort {
    justify-content: space-between;
    width: 100%;
  }

  .today-candidates-sort-label {
    display: inline;
    font-size: 10px;
  }

  .today-candidates-updated-at {
    font-size: 9px;
  }

  .today-candidates-sort select {
    width: 112px;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-summary),
  .today-candidate-entry :deep(.niuone-candidate-card .candidate-summary.has-industry) {
    grid-template-areas:
      'primary'
      'industry';
    grid-template-columns: minmax(0, 1fr);
    row-gap: 5px;
  }

  .today-candidate-entry :deep(.niuone-candidate-card .candidate-industry) {
    justify-content: flex-start;
    justify-self: stretch;
    max-width: 100%;
  }
}

@media (max-width: 360px) {
  .today-candidates-scan-count {
    display: none;
  }
}
</style>
