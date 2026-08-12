<script setup>
import { computed, ref } from 'vue'
import {
  displayScore,
  scoreFor,
  TECHNICAL_SCORE_CARDS,
  useTechnicalAnalysis,
} from '../composables/useTechnicalAnalysis.js'
import CandlestickChart from './technical-analysis/CandlestickChart.vue'
import TechnicalDetail from './technical-analysis/TechnicalDetail.vue'

const symbolInput = ref('600519')
const period = ref('day')
const activeMode = ref('analysis')
const expandedScanRows = ref(new Set())
const inputError = ref('')
const { analysis, minuteAnalysis, analysisState, analyze, analyzeMinute, scan, scanActive, startScan } = useTechnicalAnalysis()
const minuteLoading = ref(false)

const moduleCards = computed(() => TECHNICAL_SCORE_CARDS.map(card => ({
  ...card,
  ...scoreFor(analysis.value?.signal?.moduleScores, card.key),
})))
const chartMarkers = computed(() => {
  const rows = analysis.value?.klines || []
  const signals = analysis.value?.signal?.chanlun?.signals
  if (!rows.length || !Array.isArray(signals)) return []
  const rowDates = new Set(rows.map(row => String(row.date || '').slice(0, 10)))
  return signals
    .filter(item => item && rowDates.has(String(item.date || '').slice(0, 10)) && Number.isFinite(Number(item.price)))
    .slice(-8)
    .map(item => ({
      date: String(item.date).slice(0, 10),
      price: Number(item.price),
      type: String(item.type || '').toLowerCase().startsWith('buy') ? 'buy' : 'sell',
      label: String(item.type_name || item.type || '信号'),
    }))
})
const headlineTone = computed(() => actionTone(analysis.value?.signal?.action))
const quoteChange = computed(() => {
  const quote = analysis.value?.quote
  if (!quote) return null
  if (quote.changePct !== null) return quote.changePct
  if (quote.price !== null && quote.previousClose) return (quote.price / quote.previousClose - 1) * 100
  return null
})
const scanStatusLabel = computed(() => ({
  idle: '尚未开始', queued: '排队中', running: '扫描中', done: '扫描完成',
  error: '扫描失败', cancelled: '已取消',
}[scan.status] || scan.status))
const detailSections = computed(() => {
  const signal = analysis.value?.signal
  if (!signal) return []
  return [
    { key: 'trend', title: '趋势系统', subtitle: '均线结构、阶段与趋势线', value: signal.trend },
    { key: 'volume', title: '量价分析', subtitle: '成交量、OBV 与资金动能', value: signal.volumePrice },
    { key: 'pattern', title: '形态识别', subtitle: '典型形态及成立条件', value: signal.patterns },
    { key: 'breakout', title: '突破系统', subtitle: '通道突破、波动与止损', value: signal.breakouts },
    { key: 'canslim', title: 'CAN SLIM', subtitle: '成长性与市场环境综合打分', value: signal.canslim },
    { key: 'chanlun', title: '缠论结构', subtitle: '分型、笔、中枢与当前状态', value: signal.chanlun },
  ]
})

function normalizeSymbol(value) {
  const compact = String(value || '').replace(/[^A-Za-z0-9]/g, '').toLowerCase()
  const match = compact.match(/^(?:sh|sz|bj)?(\d{6})$/)
  return match ? match[1] : ''
}

async function submitAnalysis() {
  const symbol = normalizeSymbol(symbolInput.value)
  if (!symbol) {
    inputError.value = '请输入 6 位 A 股代码'
    return
  }
  inputError.value = ''
  symbolInput.value = symbol
  activeMode.value = 'analysis'
  await analyze(symbol, period.value)
}

async function submitScan() {
  activeMode.value = 'scan'
  expandedScanRows.value = new Set()
  await startScan(period.value)
}

async function submitMinute() {
  const symbol = normalizeSymbol(symbolInput.value)
  if (!symbol) {
    inputError.value = '请输入 6 位 A 股代码'
    return
  }
  inputError.value = ''
  symbolInput.value = symbol
  minuteLoading.value = true
  try {
    await analyzeMinute(symbol)
  } finally {
    minuteLoading.value = false
  }
}

function price(value) {
  if (value === null || value === undefined || value === '') return '--'
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 3 })
}

function pct(value, signed = true) {
  if (value === null || value === undefined || value === '') return '--'
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${signed && number > 0 ? '+' : ''}${Number(number.toFixed(1))}%`
}

function confidence(value) {
  if (value === null || value === undefined || value === '') return '--'
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return pct(number <= 1 ? number * 100 : number, false)
}

function valueTone(value) {
  const number = Number(value)
  if (!Number.isFinite(number) || number === 0) return 'neutral'
  return number > 0 ? 'positive' : 'negative'
}

function actionTone(value) {
  const text = String(value || '').toLowerCase()
  if (/buy|买|增持|强势|看多/.test(text)) return 'positive'
  if (/sell|卖|减持|弱势|看空|回避/.test(text)) return 'negative'
  return 'neutral'
}

function riskTone(value) {
  const text = String(value || '').toLowerCase()
  if (/high|critical|高|极高/.test(text)) return 'negative'
  if (/low|低/.test(text)) return 'positive'
  return 'neutral'
}

function qualityRows(value) {
  if (!value || typeof value !== 'object') return []
  const labels = {
    status: '数据状态', row_count: 'K 线条数', kline_count: 'K 线条数',
    rejected_row_count: '剔除异常行', missing_volume_count: '缺失成交量',
    quote_available: '实时行情', fund_flow_available: '资金流', index_available: '大盘数据',
    kline_source: 'K 线来源', last_kline_date: '最新 K 线', generated_at: '生成时间',
  }
  return Object.entries(value).filter(([key, item]) => key !== 'warnings' && key !== 'degraded' && item !== null && item !== undefined && item !== '').map(([key, item]) => ({
    key,
    label: labels[key] || key.replaceAll('_', ' '),
    value: typeof item === 'boolean' ? (item ? '可用' : '不可用') : String(item),
    ok: typeof item === 'boolean' ? item : null,
  }))
}

function levelRows(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  const labels = {
    support: '支撑位', resistance: '压力位', entry: '参考入场', stop_loss: '止损位',
    target: '目标位', entry_price: '入场价', target_price: '目标价',
  }
  return Object.entries(value).map(([key, item]) => ({ key, label: labels[key] || key.replaceAll('_', ' '), value: price(item) }))
}

function scanScoreSummary(row) {
  return TECHNICAL_SCORE_CARDS.map(card => ({
    ...card,
    ...scoreFor(row.moduleScores, card.key),
  }))
}

function toggleScanRow(index) {
  const next = new Set(expandedScanRows.value)
  if (next.has(index)) next.delete(index)
  else next.add(index)
  expandedScanRows.value = next
}
</script>

<template>
  <div class="technical-page">
    <section class="technical-hero card">
      <div class="technical-heading">
        <div>
          <span class="technical-eyebrow">MULTI-SYSTEM RESEARCH</span>
          <h2>个股技术分析</h2>
          <p>结合趋势、量价、形态、突破、CAN SLIM 与缠论结构，仅供研究参考。</p>
        </div>
        <div class="technical-mode-tabs" role="tablist" aria-label="技术分析模式">
          <button :class="{ active: activeMode === 'analysis' }" type="button" role="tab" :aria-selected="activeMode === 'analysis'" @click="activeMode = 'analysis'">个股研究</button>
          <button :class="{ active: activeMode === 'scan' }" type="button" role="tab" :aria-selected="activeMode === 'scan'" @click="activeMode = 'scan'">全市场扫描</button>
        </div>
      </div>

      <form class="technical-toolbar" :class="{ 'is-scan-mode': activeMode === 'scan' }" @submit.prevent="submitAnalysis">
        <label v-if="activeMode === 'analysis'" class="technical-symbol-field">
          <span>股票代码</span>
          <input v-model.trim="symbolInput" type="text" inputmode="numeric" maxlength="8" autocomplete="off" placeholder="例如 600519" aria-describedby="technical-input-help" @input="inputError = ''" />
        </label>
        <fieldset class="technical-period-control">
          <legend>分析周期</legend>
          <button type="button" :class="{ active: period === 'day' }" :aria-pressed="period === 'day'" @click="period = 'day'">日线</button>
          <button type="button" :class="{ active: period === 'week' }" :aria-pressed="period === 'week'" @click="period = 'week'">周线</button>
        </fieldset>
        <div class="technical-toolbar-actions">
          <button v-if="activeMode === 'analysis'" class="technical-primary-btn" type="submit" :disabled="analysisState.loading">
            <span v-if="analysisState.loading" class="technical-spinner" aria-hidden="true"></span>
            {{ analysisState.loading ? '分析中…' : '开始分析' }}
          </button>
          <button v-if="activeMode === 'analysis'" class="technical-scan-btn" type="button" :disabled="minuteLoading" @click="submitMinute">
            {{ minuteLoading ? '分时分析中…' : '分时分析' }}
          </button>
          <button v-if="activeMode === 'scan'" class="technical-primary-btn" type="button" :disabled="scanActive" @click="submitScan">
            {{ scanActive ? '扫描中…' : '开始扫描' }}
          </button>
        </div>
      </form>
      <p id="technical-input-help" class="technical-input-help" :class="{ error: inputError }">
        {{ inputError || '支持沪深京 A 股 6 位代码；全市场扫描仅使用 NiuOne 本地 K 线缓存。' }}
      </p>
    </section>

    <section v-if="analysisState.error && activeMode === 'analysis'" class="technical-alert error" role="alert">
      <b>分析未完成</b><span>{{ analysisState.error }}</span>
    </section>

      <template v-if="activeMode === 'analysis'">
      <section v-if="!analysis && !analysisState.loading" class="technical-welcome card">
        <div class="technical-welcome-mark" aria-hidden="true">个</div>
        <div><h3>输入股票代码开始分析</h3><p>生成 K 线、五维评分、关键价位、风险提示与交易计划。</p></div>
      </section>

      <template v-if="analysis">
        <section class="technical-overview card">
          <div class="technical-quote">
            <div class="technical-security-name">
              <h3>{{ analysis.name || analysis.symbol || '--' }}</h3>
              <span v-if="analysis.name && analysis.symbol">{{ analysis.symbol }}</span>
              <span>{{ analysis.period === 'week' ? '周线' : '日线' }}</span>
            </div>
            <div class="technical-price-row">
              <strong>{{ price(analysis.quote.price ?? analysis.klines.at(-1)?.close) }}</strong>
              <span :class="valueTone(quoteChange)">{{ pct(quoteChange) }}</span>
            </div>
          </div>
          <div class="technical-verdict">
            <span>综合结论</span>
            <strong :class="headlineTone">{{ analysis.signal.action || '观望' }}</strong>
          </div>
          <dl class="technical-overview-metrics">
            <div><dt>综合得分</dt><dd>{{ displayScore(analysis.signal.score) }}</dd></div>
            <div><dt>信心度</dt><dd>{{ confidence(analysis.signal.confidence) }}</dd></div>
            <div><dt>风险级别</dt><dd :class="riskTone(analysis.signal.riskLevel)">{{ analysis.signal.riskLevel || '--' }}</dd></div>
          </dl>
        </section>

        <section class="technical-score-grid" aria-label="五维技术评分：趋势、量能、形态、突破、CAN SLIM">
          <article v-for="card in moduleCards" :key="card.key" class="technical-score-card card" :class="card.tone">
            <div class="technical-score-head"><span>{{ card.label }}</span><strong>{{ displayScore(card.score) }}<small v-if="card.score !== null">/{{ card.maximum }}</small></strong></div>
            <div class="technical-score-track" aria-hidden="true"><span :style="{ width: `${card.progress}%` }"></span></div>
            <p>{{ card.summary || (card.score === null ? '暂无评分' : card.tone === 'positive' ? '信号偏强' : card.tone === 'negative' ? '信号偏弱' : '信号中性') }}</p>
          </article>
        </section>

        <section class="technical-chart-card card">
          <div class="technical-card-heading"><div><h3>K 线走势</h3><p>支持按钮或滚轮缩放 {{ analysis.period === 'week' ? '周' : '日' }} K 线</p></div><span class="technical-legend"><i class="up"></i>上涨<i class="down"></i>下跌<i class="buy-marker"></i>买点<i class="sell-marker"></i>卖点</span></div>
          <CandlestickChart :rows="analysis.klines" :markers="chartMarkers" :levels="analysis.signal.keyLevels" />
        </section>

        <section class="technical-evidence-section">
          <div class="technical-section-heading">
            <div><span>ANALYSIS EVIDENCE</span><h3>系统分析依据</h3><p>从六个技术模块拆解当前结论，先看评分，再查看对应证据。</p></div>
            <em>{{ detailSections.length }} 个模块</em>
          </div>
          <div class="technical-detail-grid-layout">
            <article v-for="section in detailSections" :key="section.key" class="technical-detail-card card">
              <div class="technical-card-heading"><div><h3>{{ section.title }}</h3><p>{{ section.subtitle }}</p></div></div>
              <TechnicalDetail :value="section.value" />
            </article>
          </div>
        </section>

        <section class="technical-decisions-section">
          <div class="technical-section-heading">
            <div><span>DECISION SUPPORT</span><h3>执行参考</h3><p>把信号、风险、关键价位和交易计划集中在一个决策区。</p></div>
          </div>
          <div class="technical-decision-grid">
          <article class="technical-decision-card card signals">
            <div class="technical-card-heading"><div><h3>信号汇总</h3><p>多系统共振与反向信号</p></div></div>
            <div class="technical-signal-columns">
              <div><h4>买入信号</h4><ul v-if="analysis.signal.buySignals.length"><li v-for="item in analysis.signal.buySignals" :key="item">{{ item }}</li></ul><p v-else>暂无明确买入信号</p></div>
              <div><h4>卖出信号</h4><ul v-if="analysis.signal.sellSignals.length"><li v-for="item in analysis.signal.sellSignals" :key="item">{{ item }}</li></ul><p v-else>暂无明确卖出信号</p></div>
            </div>
          </article>
          <article class="technical-decision-card card risk">
            <div class="technical-card-heading"><div><h3>风险提示</h3><p>执行前需要确认的失效条件</p></div></div>
            <ul v-if="analysis.signal.riskWarnings.length" class="technical-warning-list"><li v-for="item in analysis.signal.riskWarnings" :key="item">{{ item }}</li></ul>
            <p v-else class="technical-empty-copy">未生成额外风险警示，仍需设定止损。</p>
          </article>
          <article class="technical-decision-card card levels">
            <div class="technical-card-heading"><div><h3>关键价位</h3><p>支撑、压力与交易失效位</p></div></div>
            <dl v-if="levelRows(analysis.signal.keyLevels).length" class="technical-levels"><div v-for="item in levelRows(analysis.signal.keyLevels)" :key="item.key"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div></dl>
            <p v-else class="technical-empty-copy">暂无可用价位</p>
          </article>
          <article class="technical-decision-card card plan">
            <div class="technical-card-heading"><div><h3>交易计划</h3><p>资金管理与执行参考</p></div></div>
            <TechnicalDetail :value="analysis.signal.tradePlan" empty-text="当前信号未生成交易计划" />
          </article>
          </div>
        </section>

        <section class="technical-utility-section">
          <div class="technical-section-heading compact">
            <div><span>DATA HEALTH</span><h3>数据与辅助分析</h3></div>
          </div>
          <section class="technical-quality card" :class="{ degraded: analysis.dataQuality.degraded }">
          <div class="technical-card-heading"><div><h3>数据质量</h3><p>行情来源、样本覆盖与降级状态</p></div><span class="technical-quality-badge">{{ analysis.dataQuality.degraded ? '降级数据' : '数据正常' }}</span></div>
          <dl class="technical-quality-grid"><div v-for="item in qualityRows(analysis.dataQuality)" :key="item.key"><dt>{{ item.label }}</dt><dd :class="item.ok === false ? 'negative' : item.ok === true ? 'positive' : ''">{{ item.value }}</dd></div></dl>
          <ul v-if="Array.isArray(analysis.dataQuality.warnings) && analysis.dataQuality.warnings.length" class="technical-warning-list"><li v-for="item in analysis.dataQuality.warnings" :key="String(item)">{{ item }}</li></ul>
        </section>
          <section v-if="minuteAnalysis" class="technical-quality technical-minute-card card">
            <div class="technical-card-heading"><div><h3>分时缠论</h3><p>今日五分钟数据结构分析</p></div><span>{{ minuteAnalysis.available === false ? '数据不足' : '已完成' }}</span></div>
            <TechnicalDetail :value="minuteAnalysis" empty-text="分时数据暂不可用" />
          </section>
        </section>
      </template>
    </template>

    <template v-else>
      <section class="technical-scan-status card">
        <div class="technical-card-heading"><div><h3>全市场扫描</h3><p>从本地 K 线缓存中筛选技术信号较强的候选股</p></div><span class="technical-scan-state" :class="scan.status">{{ scanStatusLabel }}</span></div>
        <div v-if="scan.status !== 'idle'" class="technical-progress-block">
          <div class="technical-progress-copy"><span>{{ scan.stage || '等待扫描进度' }}</span><b>{{ Math.round(scan.progress) }}%</b></div>
          <div class="technical-progress-track"><span :style="{ width: `${scan.progress}%` }"></span></div>
          <p>{{ scan.scanned.toLocaleString('zh-CN') }} / {{ scan.total ? scan.total.toLocaleString('zh-CN') : '--' }} 只已扫描<span v-if="scan.results.length"> · {{ scan.results.length }} 只入选</span></p>
        </div>
        <div v-else class="technical-scan-empty"><div aria-hidden="true">扫</div><p><b>尚未开始扫描</b><span>选择日线或周线，然后点击“开始扫描”。</span></p></div>
        <div v-if="scan.error" class="technical-alert error" role="alert"><b>{{ scan.status === 'cancelled' ? '扫描已取消' : '扫描未完成' }}</b><span>{{ scan.error }}</span></div>
      </section>

      <section v-if="scan.status === 'done'" class="technical-scan-results card">
        <div class="technical-card-heading"><div><h3>扫描结果</h3><p>按综合得分与信心度排序，最多展示 50 只</p></div><span>{{ scan.results.length }} 只候选</span></div>
        <div v-if="scan.results.length" class="technical-scan-table-wrap">
          <table class="technical-scan-table">
            <thead><tr><th>代码 / 名称</th><th>价格</th><th>操作</th><th>得分</th><th>信心度</th><th>风险</th><th><span class="sr-only">详情</span></th></tr></thead>
            <tbody v-for="(row, index) in scan.results" :key="`${row.symbol}-${index}`">
              <tr class="technical-scan-row" :class="{ expanded: expandedScanRows.has(index) }" @click="toggleScanRow(index)">
                <td><b>{{ row.name || row.symbol || '--' }}</b><small v-if="row.name">{{ row.symbol }}</small></td>
                <td>{{ price(row.price) }}</td><td><span class="technical-action-badge" :class="actionTone(row.action)">{{ row.action }}</span></td>
                <td><strong>{{ displayScore(row.score) }}</strong></td><td>{{ confidence(row.confidence) }}</td><td><span :class="riskTone(row.riskLevel)">{{ row.riskLevel || '--' }}</span></td>
                <td><button type="button" :aria-expanded="expandedScanRows.has(index)" :aria-label="`查看 ${row.name || row.symbol} 详情`" @click.stop="toggleScanRow(index)">{{ expandedScanRows.has(index) ? '−' : '+' }}</button></td>
              </tr>
              <tr v-if="expandedScanRows.has(index)" class="technical-scan-detail-row"><td colspan="7"><div class="technical-scan-detail"><div class="technical-mini-scores"><span v-for="card in scanScoreSummary(row)" :key="card.key"><i>{{ card.label }}</i><b>{{ displayScore(card.score) }}</b></span></div><div><h4>交易计划</h4><TechnicalDetail :value="row.tradePlan" empty-text="暂无交易计划" /></div></div></td></tr>
            </tbody>
          </table>
        </div>
        <p v-else class="technical-no-results">本次扫描未找到达到筛选阈值的候选股。</p>
      </section>
    </template>
  </div>
</template>

<style src="../../../frontend/technical-analysis.css"></style>
