<script setup>
import { defineAsyncComponent } from 'vue'
import ComplianceDialog from './ComplianceDialog.vue'
import { useDashboardTabs } from '../composables/useDashboardTabs.js'

const DragonTigerPanel = defineAsyncComponent(() => import('./DragonTigerPanel.vue'))
const IndustryFlowPanel = defineAsyncComponent(() => import('./IndustryFlowPanel.vue'))
const IndicesPanel = defineAsyncComponent(() => import('./IndicesPanel.vue'))
const MarketMonitorPanel = defineAsyncComponent(() => import('./MarketMonitorPanel.vue'))
const NiuOneMainlinePanel = defineAsyncComponent(() => import('./NiuOneMainlinePanel.vue'))
const OverviewPanel = defineAsyncComponent(() => import('./OverviewPanel.vue'))
const PracticePanel = defineAsyncComponent(() => import('./PracticePanel.vue'))
const RealtimeNewsPanel = defineAsyncComponent(() => import('./RealtimeNewsPanel.vue'))
const TodayCandidatesPanel = defineAsyncComponent(() => import('./TodayCandidatesPanel.vue'))
const TechnicalAnalysisPanel = defineAsyncComponent(() => import('./TechnicalAnalysisPanel.vue'))
const WatchlistPanel = defineAsyncComponent(() => import('./WatchlistPanel.vue'))

document.title = '牛牛1号'
const { activeCategory } = useDashboardTabs()
</script>

<template>
  <ComplianceDialog />
  <main>
    <section v-if="activeCategory === 'overview'" class="feed">
      <OverviewPanel />
    </section>
    <section v-else-if="activeCategory === 'dragon_tiger'" class="feed">
      <DragonTigerPanel />
    </section>
    <section v-else-if="activeCategory === 'indices'" class="feed">
      <IndicesPanel />
    </section>
    <section v-else-if="activeCategory === 'industry_flow'" class="feed">
      <IndustryFlowPanel />
    </section>
    <section v-else-if="activeCategory === 'market_monitor'" class="feed">
      <MarketMonitorPanel />
    </section>
    <section v-else-if="activeCategory === 'realtime_news'" class="feed">
      <RealtimeNewsPanel />
    </section>
    <section v-else-if="activeCategory === 'practice'" class="feed">
      <PracticePanel />
    </section>
    <section v-else-if="activeCategory === 'watchlist'" class="feed">
      <WatchlistPanel />
    </section>
    <section v-else-if="activeCategory === 'technical_analysis'" class="feed">
      <TechnicalAnalysisPanel />
    </section>
    <section v-else-if="activeCategory === 'candidates'" class="feed">
      <TodayCandidatesPanel />
    </section>
    <section v-else-if="activeCategory === 'niuone_mainline'" class="feed">
      <NiuOneMainlinePanel />
    </section>
    <section v-show="!['overview', 'dragon_tiger', 'indices', 'industry_flow', 'market_monitor', 'realtime_news', 'practice', 'candidates', 'technical_analysis', 'watchlist', 'niuone_mainline'].includes(activeCategory)" class="feed">
      <div class="loading">加载中…</div>
    </section>
  </main>
</template>

<style src="../../../frontend/dashboard.css"></style>
<style src="../../../frontend/tongdaxin-theme.css"></style>
<style src="../../../frontend/dashboard-compact.css"></style>
