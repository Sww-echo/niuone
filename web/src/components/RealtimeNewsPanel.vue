<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRealtimeNewsData } from '../composables/useRealtimeNewsData.js'
import {
  filterRealtimeNews,
  realtimeNewsClock,
  realtimeNewsDate,
  realtimeNewsErrorText,
  realtimeNewsSourceOptions,
  realtimeNewsStatusText,
} from '../utils/realtimeNewsDisplay.js'

const {
  state,
  activateRealtimeNews,
  deactivateRealtimeNews,
  refreshRealtimeNews,
} = useRealtimeNewsData()
const activeSource = ref('all')
const importantOnly = ref(false)
const sourceOptions = computed(() => realtimeNewsSourceOptions(state.sources, state.items))
const visibleItems = computed(() => filterRealtimeNews(
  state.items,
  activeSource.value,
  importantOnly.value,
))
const sourceErrors = computed(() => state.sources
  .filter(source => source?.error)
  .map(source => `${source.label || source.id}：${realtimeNewsErrorText(source.error)}`))

watch(sourceOptions, options => {
  if (!options.some(option => option.id === activeSource.value)) activeSource.value = 'all'
})

onMounted(() => activateRealtimeNews())
onBeforeUnmount(() => deactivateRealtimeNews())
</script>

<template>
  <section class="realtime-news-page" aria-label="财经快讯">
    <div class="realtime-news-header sector-cloud">
      <div class="realtime-news-heading">
        <div class="realtime-news-title">
          <div class="realtime-news-title-line">
            <h2>财经快讯</h2>
            <span class="realtime-news-eyebrow">MARKET FLASH</span>
          </div>
          <p>聚合设置中选定的 NewsNow 来源，页面可见时每 30 秒检查一次。</p>
        </div>
        <div class="realtime-news-health" :class="state.status">
          <span class="realtime-news-health-dot" aria-hidden="true"></span>
          <span>{{ realtimeNewsStatusText(state.status) }}</span>
        </div>
      </div>

      <div class="realtime-news-controls">
        <div class="realtime-news-sources" role="tablist" aria-label="新闻来源">
          <button
            v-for="option in sourceOptions"
            :key="option.id"
            type="button"
            role="tab"
            :aria-selected="activeSource === option.id"
            :class="{ active: activeSource === option.id, stale: option.stale }"
            @click="activeSource = option.id"
          >{{ option.label }} <span>{{ option.count }}</span></button>
        </div>
        <label class="realtime-news-important-toggle">
          <input v-model="importantOnly" type="checkbox">
          <span>仅重要</span>
        </label>
        <button
          type="button"
          class="realtime-news-refresh"
          :disabled="state.loading"
          @click="refreshRealtimeNews()"
        >{{ state.loading ? '更新中…' : '立即检查' }}</button>
      </div>
    </div>

    <div
      v-if="state.stale || (state.error && state.items.length)"
      class="industry-flow-notice warning"
    >
      {{ sourceErrors.join('；') || realtimeNewsErrorText(state.error) }}。继续展示最近一次有效新闻。
    </div>

    <div v-if="state.loading && !state.loaded" class="loading">正在连接 NewsNow…</div>
    <div v-else-if="!state.enabled" class="empty">
      财经快讯尚未启用，请在管理设置的“财经快讯”中开启。
    </div>
    <div v-else-if="!state.items.length" class="empty">
      {{ realtimeNewsErrorText(state.error) }}
    </div>
    <div v-else-if="!visibleItems.length" class="empty">当前筛选条件下暂无新闻</div>
    <div v-else class="realtime-news-stream">
      <div class="realtime-news-table-head" aria-hidden="true">
        <span>来源 / 时间</span>
        <span>快讯内容</span>
      </div>
      <ol class="realtime-news-list">
        <li
          v-for="item in visibleItems"
          :key="item.id"
          class="realtime-news-item"
          :class="{ important: item.important }"
        >
          <div class="realtime-news-meta-cell">
            <div class="realtime-news-source-cell">
              <span>{{ item.source_name || item.source_id }}</span>
            </div>
            <time :datetime="item.published_at || undefined">
              <strong>{{ realtimeNewsClock(item) }}</strong>
              <span>{{ realtimeNewsDate(item) }}</span>
            </time>
          </div>
          <article>
            <div class="realtime-news-title-row">
              <span v-if="item.important" class="realtime-news-important">重要</span>
              <a
                v-if="item.url"
                :href="item.url"
                target="_blank"
                rel="noopener noreferrer"
              >{{ item.title }}</a>
              <h3 v-else>{{ item.title }}</h3>
            </div>
            <p v-if="item.summary">{{ item.summary }}</p>
          </article>
        </li>
      </ol>
    </div>
  </section>
</template>
