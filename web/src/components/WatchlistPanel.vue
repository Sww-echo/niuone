<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useWatchlistData } from '../composables/useWatchlistData.js'
import { authenticateAdmin } from '../utils/adminSession.js'
import { formatPrice, formatSignedMoney, formatPct, formatMoney, toneClass, dateLabel,
  isJobActive, jobStatusLabel, formatDuration } from '../utils/watchlistDisplay.js'

const {
  state,
  loadBoard,
  currentFilterKey,
  startJobPolling,
  stopJobPolling,
  retryJob,
  setDays,
  setGroupId,
  setQuery,
  createGroup,
  addStocks,
  updateStock,
  deleteStock,
  updateToday,
  backfill,
} = useWatchlistData()

const form = reactive({
  codes: '',
  groupId: '',
  note: '',
  buyDate: '',
  groupName: '',
  groupNote: '',
  search: state.q,
  backfillDays: 60,
  missingOnly: true,
})

const edit = reactive({
  open: false,
  code: '',
  name: '',
  note: '',
  groupId: '',
  buyDate: '',
  active: true,
})

const actionDialog = reactive({ open: false, mode: 'add' })
const adminAuth = reactive({ open: false, credential: '', error: '', submitting: false })
const adminCredentialInput = ref(null)
const pendingAdminAction = ref(null)
let searchTimer = 0
const buyDateEdited = ref(false)

const vModal = {
  mounted(dialog) { dialog.showModal() },
  beforeUnmount(dialog) { dialog.close() },
}

const dayOptions = computed(() => state.board.allowed_days?.length ? state.board.allowed_days : [5,7,15,60])

const summary = computed(() => state.board.summary || {})
const tradeDates = computed(() => state.board.trade_dates || [])
const groups = computed(() => state.board.groups || [])
const sections = computed(() => {
  const list = state.board.sections || []
  if (list.length) return list
  const flat = state.board.stocks || []
  if (!flat.length) return []
  return [{
    group_id: null,
    group_name: '全部',
    stock_count: flat.length,
    total_pnl: summary.value.total_pnl,
    total_pnl_percent: summary.value.total_pnl_percent,
    stocks: flat,
  }]
})
const stocks = computed(() => state.board.stocks || [])
const pendingCount = computed(() => state.board.summary?.pending_count || 0)
const ungroupedCount = computed(() => state.board.ungrouped_count || 0)
const hasCurrentBoard = computed(() => state.loaded && state.boardFilterKey === currentFilterKey())
const jobActive = computed(() => isJobActive(state.job))
const filtered = computed(() => !!(state.groupId || state.q))
const refreshedAt = computed(() => String(state.board.generated_at || '').replace('T', ' ').slice(0, 19))
const jobTitle = computed(() => state.job?.kind === 'backfill' ? '历史行情回补' : '今日行情更新')
const retryLabel = computed(() => state.job?.processed < state.job?.total ? '重试失败及未完成项' : '仅重试失败项')

watch(() => state.board.default_buy_date, (value) => {
  if (value && !buyDateEdited.value && !form.buyDate) form.buyDate = value
}, { immediate: true })

const buyDatePresets = computed(() => {
  const dates = (state.board.trade_dates || []).filter(date => date <= state.board.default_buy_date)
  return dates.slice().reverse().slice(0, 8)
})

function pickBuyDate(date, target = 'form') {
  if (target === 'form') { form.buyDate = date; buyDateEdited.value = true }
  else edit.buyDate = date
}

function fillStatusLabel(status) {
  const map = {
    filled: '已建仓',
    pending_fill: '待回补',
    no_buy_date: '未设置买入日',
    pending_quote: '暂无最新行情',
    awaiting_close: '待收盘',
    invalid_buy_date: '请调整买入日期',
  }
  return map[status] || status
}

function openActionDialog(mode) {
  actionDialog.mode = mode
  actionDialog.open = true
}

function closeActionDialog() {
  actionDialog.open = false
}

async function withAdminRetry(actionFn) {
  const result = await actionFn()
  if (result?.needAdmin) {
    pendingAdminAction.value = actionFn
    adminAuth.open = true
    adminAuth.error = ''
    adminAuth.credential = ''
    await nextTick()
    adminCredentialInput.value?.focus()
  }
  return result
}

async function submitAdminAuthentication() {
  if (adminAuth.submitting) return
  adminAuth.submitting = true
  adminAuth.error = ''
  try {
    await authenticateAdmin(adminAuth.credential)
    const action = pendingAdminAction.value
    pendingAdminAction.value = null
    adminAuth.open = false
    adminAuth.credential = ''
    if (action) await action()
  } catch (error) {
    adminAuth.error = error instanceof Error ? error.message : '管理员凭据错误'
    adminAuth.credential = ''
    await nextTick()
    adminCredentialInput.value?.focus()
  } finally {
    adminAuth.submitting = false
  }
}

function cancelAdminAuthentication() {
  pendingAdminAction.value = null
  adminAuth.open = false
  adminAuth.credential = ''
  adminAuth.error = ''
}

async function onCreateGroup() {
  if (!form.groupName.trim()) { state.error = '请输入分组名称'; return }
  const result = await withAdminRetry(() => createGroup({ name: form.groupName.trim(), note: form.groupNote.trim() }))
  if (result?.ok) { form.groupName = ''; form.groupNote = '' }
}

async function onAddStocks() {
  if (!form.codes.trim()) { state.error = '请输入股票代码'; return }
  const payload = {
    codes: form.codes,
    note: form.note,
    buy_date: form.buyDate || undefined,
    group_id: form.groupId || undefined,
  }
  const result = await withAdminRetry(() => addStocks(payload))
  if (result?.ok) {
    form.codes = ''
    form.note = ''
    closeActionDialog()
  }
}

async function onUpdateToday() {
  await withAdminRetry(() => updateToday())
}

async function onBackfill() {
  const result = await withAdminRetry(() => backfill({ days: Number(form.backfillDays) || 60, missingOnly: form.missingOnly }))
  if (result?.ok) closeActionDialog()
}

function onDaysChange(e) { setDays(e.target.value) }
function onGroupFilterChange(e) { setGroupId(e.target.value) }
function onSearchInput(e) {
  form.search = e.target.value
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => setQuery(form.search), 250)
}

function openEdit(stock) {
  edit.open = true
  edit.code = stock.code
  edit.name = stock.name || ''
  edit.note = stock.note || ''
  edit.groupId = stock.group_id == null ? '' : String(stock.group_id)
  edit.buyDate = stock.buy_date || ''
  edit.active = stock.active !== false
}

function closeEdit() { edit.open = false }

async function saveEdit() {
  const result = await withAdminRetry(() => updateStock(edit.code, {
    name: edit.name,
    note: edit.note,
    group_id: edit.groupId === '' ? null : Number(edit.groupId),
    buy_date: edit.buyDate,
    active: edit.active,
  }))
  if (result?.ok) closeEdit()
}

async function onDelete(stock) {
  if (!window.confirm(`删除 ${stock.code}${stock.name ? ' ' + stock.name : ''}？`)) return
  await withAdminRetry(() => deleteStock(stock.code))
}

onMounted(() => { loadBoard(); startJobPolling() })
onUnmounted(() => { window.clearTimeout(searchTimer); stopJobPolling() })
</script>

<template>
  <section class="card watchlist-panel">
    <div class="watchlist-hero">
      <div class="watchlist-title-block">
        <h2>自选股走势</h2>
        <p class="watchlist-subtitle">
          按分组跟踪多日走势；可选买入日默认按 1 万元尽量买满，仅展示浮动盈亏。
        </p>
      </div>
      <div class="watchlist-hero-side">
        <div class="watchlist-status" :class="{ error: !!state.error }">
          {{ state.error || state.status || '就绪' }}
        </div>
        <div class="watchlist-actions">
          <button type="button" class="primary" :disabled="state.busy" @click="openActionDialog('add')">添加自选</button>
          <button type="button" :disabled="state.busy" @click="openActionDialog('manage')">分组 / 回补</button>
          <button type="button" class="accent" :disabled="state.busy || jobActive" @click="onUpdateToday">更新今日行情</button>
        </div>
      </div>
    </div>

    <div class="watchlist-summary" v-if="hasCurrentBoard">
      <div class="watchlist-metric">
        <span class="label">{{ filtered ? '筛选内自选数' : '自选数' }}</span>
        <strong>{{ state.board.total }}</strong>
      </div>
      <div class="watchlist-metric">
        <span class="label">已建仓</span>
        <strong>{{ summary.filled_count || 0 }}</strong>
      </div>
      <div class="watchlist-metric">
        <span class="label">待补齐</span>
        <strong>{{ pendingCount }}</strong>
      </div>
      <div class="watchlist-metric">
        <span class="label">成本合计</span>
        <strong>{{ formatMoney(summary.total_cost) }}</strong>
      </div>
      <div class="watchlist-metric">
        <span class="label">市值合计</span>
        <strong>{{ formatMoney(summary.total_market_value) }}</strong>
      </div>
      <div class="watchlist-metric highlight">
        <span class="label">浮动盈亏</span>
        <strong :class="toneClass(summary.total_pnl)">
          {{ formatSignedMoney(summary.total_pnl) }}
          <small v-if="summary.total_pnl_percent != null">
            ({{ formatPct(summary.total_pnl_percent) }})
          </small>
        </strong>
      </div>
    </div>

    <p v-if="hasCurrentBoard && summary.valuation_complete === false" class="watchlist-data-note">
      {{ summary.missing_quote_count }} 只持仓缺少有效报价，市值与盈亏待补齐，成本已保留。
    </p>
    <p v-else-if="hasCurrentBoard && summary.stale_count" class="watchlist-data-note">
      {{ summary.stale_count }} 只持仓按最近有效价估值，报价日期见下表。
    </p>

    <div v-if="state.loadError" class="watchlist-notice" role="alert">
      <span>{{ state.loadError }}{{ hasCurrentBoard ? '，当前保留上次结果。' : '' }}</span>
      <button type="button" :disabled="state.loading" @click="loadBoard()">重试刷新</button>
    </div>
    <details v-if="state.actionFailures.length" class="watchlist-notice" open>
      <summary>{{ state.actionFailures.length }} 只股票处理未完成</summary>
      <ul class="watchlist-failures">
        <li v-for="item in state.actionFailures" :key="item.code">{{ item.code }} · {{ item.error }}</li>
      </ul>
    </details>

    <section v-if="state.job" class="watchlist-job" aria-label="行情处理进度">
      <div class="watchlist-job-header">
        <strong>{{ jobTitle }} · {{ jobStatusLabel(state.job) }}</strong>
        <span role="status">{{ state.job.processed }} / {{ state.job.total }} 只 · {{ formatDuration(state.job.elapsed_seconds) }}</span>
      </div>
      <progress v-if="jobActive" :value="state.job.processed" :max="Math.max(1, state.job.total)" aria-label="已处理股票数量" />
      <div class="watchlist-job-header">
        <span>成功 {{ state.job.succeeded }} · 跳过 {{ state.job.skipped }} · 失败 {{ state.job.failed.length }}<template v-if="state.job.current_code"> · 正在处理 {{ state.job.current_code }}</template></span>
        <button v-if="state.job.retry_count > 0 && !jobActive" type="button" :disabled="state.busy" @click="withAdminRetry(() => retryJob())">
          {{ retryLabel }} ({{ state.job.retry_count }})
        </button>
      </div>
      <p v-if="state.job.skipped && state.job.kind === 'update-today'" class="watchlist-data-note">跳过项已保留原行情，可在需要时再次更新。</p>
      <p v-if="state.job.error" class="watchlist-data-note">{{ state.job.error }}</p>
      <details v-if="state.job.failed.length">
        <summary>查看失败股票与原因</summary>
        <ul class="watchlist-failures">
          <li v-for="item in state.job.failed" :key="item.code">{{ item.code }} · {{ item.error }}</li>
        </ul>
      </details>
    </section>
    <p v-if="state.jobError" class="watchlist-data-note" role="status">{{ state.jobError }}</p>

    <div class="watchlist-control-panel" v-if="state.loaded">
      <div class="watchlist-filter-grid">
        <label class="watchlist-field grow">
          <span class="field-label">搜索</span>
          <input :value="form.search" type="text" placeholder="代码 / 名称 / 分组 / 备注" @input="onSearchInput">
        </label>
        <label class="watchlist-field">
          <span class="field-label">分组筛选</span>
          <select :value="state.groupId" @change="onGroupFilterChange">
            <option value="">全部组别</option>
            <option value="ungrouped">未分组 ({{ ungroupedCount }})</option>
            <option v-for="group in groups" :key="group.id" :value="String(group.id)">
              {{ group.name }} ({{ group.stock_count || 0 }})
            </option>
          </select>
        </label>
        <label class="watchlist-field compact">
          <span class="field-label">展示天数</span>
          <select :value="String(state.days)" @change="onDaysChange">
            <option v-for="day in dayOptions" :key="day" :value="String(day)">最近 {{ day }} 日</option>
          </select>
        </label>
      </div>
      <span class="watchlist-meta">
        当前 {{ hasCurrentBoard ? state.board.total : '—' }} / 全部 {{ state.board.total_all }} 只 · 展示 {{ state.days }} 日
        <br>行情截至 {{ hasCurrentBoard ? state.board.latest_trade_date || '暂无' : '—' }}
        <br>页面刷新 {{ refreshedAt || '—' }}
      </span>
    </div>

    <div class="watchlist-chip-row" v-if="state.loaded">
      <button
        type="button"
        class="watchlist-chip"
        :class="{ active: !state.groupId }"
        @click="setGroupId('')"
      >
        全部 <span class="chip-pnl">{{ state.board.total_all }}</span>
      </button>
      <button
        v-for="group in groups"
        :key="`chip-${group.id}`"
        type="button"
        class="watchlist-chip"
        :class="{ active: String(state.groupId) === String(group.id) }"
        @click="setGroupId(String(group.id))"
      >
        {{ group.name }}
        <span class="chip-pnl">{{ group.stock_count || 0 }}</span>
        <span class="chip-pnl" :class="toneClass(group.total_pnl)">
          {{ formatSignedMoney(group.total_pnl) }}
        </span>
      </button>
      <button
        v-if="ungroupedCount > 0 || state.groupId === 'ungrouped'"
        type="button"
        class="watchlist-chip"
        :class="{ active: state.groupId === 'ungrouped' }"
        @click="setGroupId('ungrouped')"
      >
        未分组 <span class="chip-pnl">{{ ungroupedCount }}</span>
      </button>
    </div>

    <div v-if="state.loading && !hasCurrentBoard" class="loading">自选股加载中…</div>
    <div v-else-if="!hasCurrentBoard" class="empty">当前筛选结果加载失败，请重试刷新。</div>
    <div v-else-if="!stocks.length && state.board.total_all > 0" class="empty">没有符合当前分组或搜索条件的股票。</div>
    <div v-else-if="!stocks.length" class="empty">还没有自选股，请先点击“添加自选”录入股票代码。</div>

    <div v-else class="watchlist-grouped">
      <section v-for="section in sections" :key="section.group_id || 'ungrouped'" class="watchlist-group-card">
        <div class="group-header">
          <span class="group-name">{{ section.group_name || '未分组' }}</span>
          <span class="group-count">{{ section.stock_count }} 只</span>
          <span class="group-pnl" :class="toneClass(section.total_pnl)">
            {{ formatSignedMoney(section.total_pnl) }} <small>({{ formatPct(section.total_pnl_percent) }})</small>
          </span>
        </div>

        <div class="watchlist-table-wrap">
          <table class="watchlist-table">
            <thead>
              <tr>
                <th class="sticky-col">代码</th>
                <th class="sticky-col sticky-2">名称</th>
                <th>买入日期</th>
                <th>模拟买入</th>
                <th>最近报价</th>
                <th class="pnl-col">自买入盈亏</th>
                <th
                  v-for="tradeDate in tradeDates"
                  :key="tradeDate"
                  colspan="2"
                  class="day-head"
                >
                  {{ dateLabel(tradeDate) }}
                </th>
                <th>操作</th>
              </tr>
              <tr>
                <th class="sticky-col sub"></th>
                <th class="sticky-col sticky-2 sub"></th>
                <th class="sub">日期</th>
                <th class="sub">价 / 股</th>
                <th class="sub">最新</th>
                <th class="sub">额 / 率</th>
                <template v-for="tradeDate in tradeDates" :key="`sub-${tradeDate}`">
                  <th class="sub">收</th>
                  <th class="sub">涨跌</th>
                </template>
                <th class="sub"></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="stock in section.stocks" :key="stock.code">
                <td class="code sticky-col">{{ stock.code }}</td>
                <td class="sticky-col sticky-2">{{ stock.name || '—' }}</td>
                <td class="buy-date-col">{{ stock.buy_date || '—' }}</td>
                <td class="buy-cell">
                  <div class="price">{{ formatPrice(stock.buy_price) }}</div>
                  <div class="muted">{{ stock.buy_shares || 0 }} 股 · {{ formatMoney(stock.buy_amount) }}</div>
                </td>
                <td class="price">
                  {{ formatPrice(stock.last_price) }}
                  <span class="watchlist-quote-date">{{ stock.last_quote_date || '暂无报价' }}{{ stock.quote_final === false ? ' · 盘中' : stock.quote_stale ? ' · 较早' : '' }}</span>
                </td>
                <td :class="['pct', 'pnl-col', stock.fill_status === 'filled' ? toneClass(stock.pnl) : 'muted']">
                  <template v-if="stock.fill_status === 'filled'">
                    <div class="pnl-amount">{{ formatSignedMoney(stock.pnl) }}</div>
                    <div class="pnl-rate">{{ formatPct(stock.pnl_percent) }}</div>
                    <div v-if="stock.fill_hint" class="watchlist-fill-hint">{{ stock.fill_hint }}</div>
                  </template>
                  <template v-else>
                    <div>—</div>
                    <div class="watchlist-fill-hint">{{ stock.fill_hint || fillStatusLabel(stock.fill_status) }}</div>
                  </template>
                </td>
                <template v-for="(quote, index) in stock.quotes" :key="index">
                  <td class="price">{{ quote ? formatPrice(quote.close_price) : '—' }}</td>
                  <td :class="['pct', quote ? toneClass(quote.change_percent) : 'muted']">
                    {{ quote ? formatPct(quote.change_percent) : '—' }}
                  </td>
                </template>
                <td class="actions">
                  <button type="button" class="link" @click="openEdit(stock)">调整</button>
                  <button type="button" class="link danger" @click="onDelete(stock)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
  </section>

  <dialog v-if="actionDialog.open" v-modal class="watchlist-dialog watchlist-action-dialog" aria-labelledby="watchlist-action-title" @close="closeActionDialog">
    <form v-if="actionDialog.mode === 'add'" class="watchlist-dialog-form" @submit.prevent="onAddStocks">
      <div class="watchlist-dialog-headline">
        <h3 id="watchlist-action-title">添加自选并模拟买入</h3>
        <p class="watchlist-subtitle">输入一个或多个股票代码，系统会按买入日收盘价模拟买入。</p>
      </div>
      <label class="watchlist-field grow">
        <span class="field-label">股票代码</span>
        <input v-model="form.codes" type="text" inputmode="numeric" placeholder="001896, 600519, 300750" autofocus>
      </label>
      <label class="watchlist-field">
        <span class="field-label">分组</span>
        <select v-model="form.groupId">
          <option value="">未分组</option>
          <option v-for="group in groups" :key="group.id" :value="String(group.id)">
            {{ group.name }}
          </option>
        </select>
      </label>
      <div class="watchlist-field buy-date-field">
        <label class="field-label" for="watchlist-buy-date">模拟买入日</label>
        <input id="watchlist-buy-date" v-model="form.buyDate" type="date" :max="state.board.generated_at?.slice(0, 10)" @input="buyDateEdited = true">
        <div class="buy-date-hint">默认最近已收盘交易日；按收盘价买入约 1 万。已有股票会跳过，保留原买入信息。</div>
        <div class="buy-date-presets" v-if="buyDatePresets.length">
          <button
            v-for="d in buyDatePresets"
            :key="`buy-${d}`"
            type="button"
            class="buy-date-chip"
            :class="{ active: form.buyDate === d }"
            @click="pickBuyDate(d, 'form')"
          >{{ dateLabel(d) }}</button>
        </div>
      </div>
      <label class="watchlist-field grow">
        <span class="field-label">备注</span>
        <input v-model="form.note" type="text" placeholder="可选">
      </label>
      <div class="watchlist-dialog-actions">
        <button type="button" @click="closeActionDialog">取消</button>
        <button type="submit" class="primary" :disabled="state.busy">添加并模拟买入</button>
      </div>
    </form>

    <form v-else class="watchlist-dialog-form" @submit.prevent>
      <div class="watchlist-dialog-headline">
        <h3 id="watchlist-action-title">分组与数据维护</h3>
        <p class="watchlist-subtitle">创建新分组，或回补历史行情数据。</p>
      </div>
      <section class="watchlist-dialog-section">
        <h4>创建分组</h4>
        <label class="watchlist-field">
          <span class="field-label">新分组</span>
          <input v-model="form.groupName" type="text" placeholder="例如：半导体" autofocus>
        </label>
        <label class="watchlist-field grow">
          <span class="field-label">分组备注</span>
          <input v-model="form.groupNote" type="text" placeholder="可选">
        </label>
        <button type="button" class="primary" :disabled="state.busy" @click="onCreateGroup">创建分组</button>
      </section>
      <section class="watchlist-dialog-section">
        <h4>回补行情</h4>
        <label class="watchlist-field">
          <span class="field-label">回补天数</span>
          <select v-model.number="form.backfillDays">
            <option :value="30">30 日</option>
            <option :value="60">60 日</option>
            <option :value="120">120 日</option>
          </select>
        </label>
        <label class="watchlist-check">
          <input v-model="form.missingOnly" type="checkbox">只补缺失日
        </label>
        <button type="button" :disabled="state.busy || jobActive" @click="onBackfill">回补历史行情</button>
      </section>
      <div class="watchlist-dialog-actions">
        <button type="button" @click="closeActionDialog">关闭</button>
      </div>
    </form>
  </dialog>

  <dialog v-if="edit.open" v-modal class="watchlist-dialog" aria-labelledby="watchlist-edit-title" @close="closeEdit">
    <form class="watchlist-dialog-form" @submit.prevent="saveEdit">
      <h3 id="watchlist-edit-title">调整自选 · {{ edit.code }}</h3>
      <p class="watchlist-subtitle">
        选择买入日（例如周一），系统按该日收盘价买入约 1 万元，相对最新价计算浮动盈亏。
      </p>
      <label class="watchlist-field">
        <span class="field-label">名称</span>
        <input v-model="edit.name" type="text">
      </label>
      <label class="watchlist-field">
        <span class="field-label">分组</span>
        <select v-model="edit.groupId">
          <option value="">未分组</option>
          <option v-for="group in groups" :key="group.id" :value="String(group.id)">
            {{ group.name }}
          </option>
        </select>
      </label>
      <div class="watchlist-field buy-date-field">
        <label class="field-label" for="watchlist-edit-buy-date">模拟买入日</label>
        <input id="watchlist-edit-buy-date" v-model="edit.buyDate" type="date" :max="state.board.generated_at?.slice(0, 10)">
        <div class="buy-date-presets" v-if="buyDatePresets.length">
          <button
            v-for="d in buyDatePresets"
            :key="`edit-buy-${d}`"
            type="button"
            class="buy-date-chip"
            :class="{ active: edit.buyDate === d }"
            @click="pickBuyDate(d, 'edit')"
          >{{ dateLabel(d) }}</button>
        </div>
      </div>
      <label class="watchlist-field">
        <span class="field-label">备注</span>
        <textarea v-model="edit.note" rows="3" />
      </label>
      <label class="watchlist-check">
        <input v-model="edit.active" type="checkbox">继续关注
      </label>
      <div class="watchlist-dialog-actions">
        <button type="button" @click="closeEdit">取消</button>
        <button type="submit" class="primary" :disabled="state.busy">保存并重算</button>
      </div>
    </form>
  </dialog>

  <dialog v-if="adminAuth.open" v-modal class="watchlist-dialog" aria-labelledby="watchlist-admin-title" @close="cancelAdminAuthentication">
    <form class="watchlist-dialog-form" @submit.prevent="submitAdminAuthentication">
      <h3 id="watchlist-admin-title">管理员验证</h3>
      <p class="watchlist-subtitle">修改自选股、更新行情需要管理员身份。</p>
      <label class="watchlist-field">
        <span class="field-label">管理员密码 / Token</span>
        <input ref="adminCredentialInput" v-model="adminAuth.credential" type="password" autocomplete="current-password">
      </label>
      <div v-if="adminAuth.error" class="watchlist-status error">{{ adminAuth.error }}</div>
      <div class="watchlist-dialog-actions">
        <button type="button" @click="cancelAdminAuthentication">取消</button>
        <button type="submit" class="primary" :disabled="adminAuth.submitting">
          {{ adminAuth.submitting ? '验证中…' : '验证并继续' }}
        </button>
      </div>
    </form>
  </dialog>
</template>

<style src="../../../frontend/watchlist.css"></style>
