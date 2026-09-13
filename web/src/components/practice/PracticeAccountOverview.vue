<script setup>
import { computed, ref } from 'vue'
import { formatPracticeAmount, formatPracticeNumber } from '../../utils/practiceDisplay.js'
import PracticeMarketSummary from './PracticeMarketSummary.vue'
import PracticePositions from './PracticePositions.vue'

const props = defineProps({
  practice: { type: Object, required: true },
  manualCycle: { type: Object, required: true },
  dataReadiness: { type: Object, required: true },
  marketSummary: { type: Object, required: true },
  marketSummaryGenerating: Boolean,
  strategyMeta: { type: Object, default: () => ({}) },
  error: { type: String, default: '' },
})
const emit = defineEmits(['manual-cycle', 'market-summary', 'resume'])
const dismissedManualFailure = ref('')

const pnl = computed(() => Number(props.practice.total_pnl || 0))
const exitReview = computed(() => props.practice.post_exit_observation_summary || {})
const feedbackPolicy = computed(() => (
  props.practice.exit_feedback_policy
  || exitReview.value.feedback_policy
  || {}
))
const feedbackStatusLabel = computed(() => ({
  active: '已生效',
  hold: '参数保持',
  learning: '样本积累中',
  cooldown: '冷却观察中',
  disabled: '未启用',
}[feedbackPolicy.value.status] || feedbackPolicy.value.status || '学习中'))
const feedbackActionLabel = computed(() => {
  const action = String(feedbackPolicy.value.action || '')
  if (action.startsWith('reduce_sell_fly:')) return '降低卖飞（单档）'
  if (action.startsWith('restore_defense:')) return '恢复防守（单档）'
  if (action.startsWith('loosen_reentry:')) return '降低再入门槛（单档）'
  if (action.startsWith('tighten_reentry:')) return '提高再入门槛（单档）'
  return {
    sample_gate: '等待样本门',
    awaiting_first_review: '等待首次盘后复盘',
    cooldown: '等待新增样本',
    automatic_rollback: '自动回退上一版',
    algorithm_upgrade: '升级反馈基线',
    raise_replacement_margin: '提高换仓门槛',
    lower_replacement_margin: '降低换仓门槛',
    hold: '保持参数',
  }[action] || action
})
const manualRunning = computed(() => props.manualCycle.running === true)
const manualFailureKey = computed(() => [
  props.manualCycle.job_id,
  props.manualCycle.finished_at,
  props.manualCycle.error_code,
].map(value => String(value || '')).join(':'))
const manualFailureVisible = computed(() => (
  !manualRunning.value
  && ['error', 'interrupted'].includes(String(props.manualCycle.stage || ''))
  && Boolean(props.manualCycle.error)
  && props.manualCycle.error_visible !== false
  && dismissedManualFailure.value !== manualFailureKey.value
))
const manualNoticeVisible = computed(() => (
  manualRunning.value && Boolean(props.manualCycle.notice)
))
const deploymentBlocked = computed(() => (
  props.dataReadiness?.blockers?.includes('runtime_storage_not_writable') === true
))
const dashboardRestartRequired = computed(() => (
  props.dataReadiness?.blockers?.includes('dashboard_restart_required') === true
))
const initializationDisabled = computed(() => (
  props.dataReadiness?.blockers?.includes('kline_cache_disabled') === true
  || props.dataReadiness?.blockers?.includes('kline_prewarm_disabled') === true
))
const initializationProgress = computed(() => Number(
  props.manualCycle.stage === 'data_initializing'
    ? props.manualCycle.progress_pct
    : props.dataReadiness?.kline?.progress_pct,
) || 0)
const readinessCoverage = computed(() => (
  Number(props.dataReadiness?.kline?.coverage || 0) * 100
))
const readinessBarProgress = computed(() => (
  props.dataReadiness?.status === 'initializing'
    ? initializationProgress.value
    : readinessCoverage.value
))
const showDataReadiness = computed(() => !(
  props.dataReadiness?.loading === false
  && props.dataReadiness?.ready === true
  && props.dataReadiness?.data_ready === true
  && props.dataReadiness?.status === 'ready'
  && !props.dataReadiness?.error
  && !props.dataReadiness?.blockers?.length
  && !props.dataReadiness?.warnings?.length
))
const manualButtonText = computed(() => {
  if (manualRunning.value) return props.manualCycle.stage_label || '本轮执行中…'
  if (dashboardRestartRequired.value) return '完整重启服务后再运行'
  if (deploymentBlocked.value) return '修复运行目录权限后再运行'
  if (initializationDisabled.value) return '启用日 K 缓存与初始化后再运行'
  if (!props.dataReadiness?.data_ready) {
    return props.dataReadiness?.status === 'initializing'
      ? '初始化完成后运行选股与交易策略'
      : '初始化数据并运行选股与交易策略'
  }
  return '手动运行选股与交易策略'
})
</script>

<template>
  <section class="sector-cloud" style="margin-bottom:18px">
    <div class="practice-account-head">
      <h3>模拟账户</h3>
      <div class="practice-account-actions">
        <button
          type="button"
          class="practice-manual-cycle-btn"
          :disabled="manualRunning || dashboardRestartRequired || deploymentBlocked || initializationDisabled"
          :aria-busy="manualRunning ? 'true' : undefined"
          :title="manualButtonText"
          @click="emit('manual-cycle')"
        >{{ manualRunning ? '处理中 · ' : '' }}{{ manualButtonText }}</button>
        <button
          type="button"
          class="practice-market-summary-btn"
          :disabled="marketSummaryGenerating"
          :aria-busy="marketSummaryGenerating ? 'true' : undefined"
          @click="emit('market-summary')"
        >{{ marketSummaryGenerating ? '正在生成盘面总结与评价…' : '生成此刻盘面总结与评价' }}</button>
      </div>
    </div>
    <div
      v-if="showDataReadiness"
      class="practice-data-readiness"
      :class="`is-${dataReadiness.status || 'unknown'}`"
      role="status"
    >
      <div class="practice-data-readiness-head">
        <strong>{{ dataReadiness.status_label || '正在检查市场数据' }}</strong>
        <span v-if="dataReadiness.kline?.requested_count && dataReadiness.status === 'initializing'">
          {{ dataReadiness.kline.completed_count || 0 }} / {{ dataReadiness.kline.requested_count }}
          · 进度 {{ initializationProgress.toFixed(1) }}%
        </span>
        <span v-else-if="dataReadiness.kline?.requested_count">
          {{ dataReadiness.kline.fresh_count || 0 }} / {{ dataReadiness.kline.requested_count }}
          · 覆盖率 {{ readinessCoverage.toFixed(1) }}%
        </span>
      </div>
      <div v-if="dataReadiness.kline?.requested_count" class="practice-data-progress" aria-hidden="true">
        <span :style="{ width: `${Math.min(100, readinessBarProgress)}%` }"></span>
      </div>
      <p v-if="dashboardRestartRequired">页面已更新，但后台仍在运行旧版本。请完整重启 NiuOne 服务，等待启动完成后刷新页面。</p>
      <p v-else-if="dataReadiness.error">就绪状态读取失败：{{ dataReadiness.error }}</p>
      <p v-else-if="dataReadiness.status === 'initializing'">首次部署或缓存过期时会在后台准备数据，页面可以继续浏览。</p>
      <p v-else-if="dataReadiness.blockers?.includes('runtime_storage_not_writable')">运行数据目录不可写，请检查目录权限后重启服务。</p>
      <p v-else-if="dataReadiness.blockers?.includes('kline_cache_disabled') || dataReadiness.blockers?.includes('kline_prewarm_disabled')">当前策略需要全市场日 K，请在设置页启用本地日 K 缓存与初始化后重启服务。</p>
      <p v-else-if="!dataReadiness.data_ready">完整市场数据尚未达到交易安全覆盖率；点击上方按钮会排队初始化，期间不会产生新交易决策。</p>
      <p v-if="dataReadiness.warnings?.includes('runtime_storage_not_persistent')">容器数据目录可能未持久化，重新部署后可能再次初始化。</p>
      <p v-if="dataReadiness.warnings?.includes('timezone_not_asia_shanghai')">服务时区不是北京时间，定时预热与交易时段可能偏移。</p>
    </div>
    <PracticeMarketSummary
      :summary="marketSummary"
      :generating="marketSummaryGenerating"
    />
    <div v-if="manualNoticeVisible" class="practice-manual-cycle-notice" role="status">
      {{ manualCycle.notice }}
    </div>
    <div v-if="manualFailureVisible" class="practice-manual-cycle-error" role="alert">
      <span>
        本轮执行失败<span v-if="manualCycle.error_code">（{{ manualCycle.error_code }}）</span>：{{ manualCycle.error }}
        <time v-if="manualCycle.finished_at"> · {{ String(manualCycle.finished_at).slice(11, 19) }}</time>
      </span>
      <button
        type="button"
        class="practice-manual-cycle-error-dismiss"
        aria-label="关闭本轮失败提示"
        @click="dismissedManualFailure = manualFailureKey"
      >×</button>
    </div>
    <div v-if="practice.trading_paused" style="background:var(--yellow-soft);border:1px solid var(--yellow-border);border-radius:8px;padding:10px 14px;margin:10px 0;display:flex;justify-content:space-between;align-items:center">
      <span style="color:var(--yellow-text);font-size:13px">新开仓已暂停：{{ practice.pause_reason || '风控触发' }}（{{ String(practice.pause_since || '').slice(11, 16) }}起，卖出风控继续运行）</span>
      <button type="button" style="background:var(--green-soft);color:var(--green-text);border:1px solid var(--green-border);border-radius:7px;padding:6px 12px;cursor:pointer;font-size:12px;font-weight:600" @click="emit('resume')">恢复交易</button>
    </div>
    <div class="practice-stats" style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:12px 0">
      <div class="inline-field"><div class="inline-label">初始资金</div><div class="inline-value">{{ formatPracticeAmount(practice.initial_cash) }}</div></div>
      <div class="inline-field"><div class="inline-label">总权益</div><div class="inline-value">{{ formatPracticeAmount(practice.total_equity) }}</div></div>
      <div class="inline-field"><div class="inline-label">现金</div><div class="inline-value">{{ formatPracticeAmount(practice.cash) }}</div></div>
      <div class="inline-field"><div class="inline-label">累计收益</div><div class="inline-value" :class="pnl >= 0 ? 'up' : 'down'">{{ formatPracticeAmount(practice.total_pnl) }} / {{ formatPracticeNumber(practice.total_pnl_pct) }}%</div></div>
    </div>
    <section
      class="practice-exit-review"
      role="status"
      aria-labelledby="practiceExitReviewTitle"
    >
      <div class="practice-exit-review-head">
        <div class="practice-exit-review-title">
          <strong id="practiceExitReviewTitle">卖后 5 日复盘</strong>
          <small>卖出后第 5 个交易日效果跟踪</small>
        </div>
        <span
          class="practice-exit-review-status"
          :data-state="feedbackPolicy.enabled ? (feedbackPolicy.status || 'enabled') : 'disabled'"
        >
          <i aria-hidden="true"></i>
          <span class="practice-exit-review-status-prefix">自动调参</span>
          {{ feedbackPolicy.enabled ? feedbackStatusLabel : '未启用' }}
        </span>
      </div>
      <template v-if="!exitReview.error">
        <dl class="practice-exit-review-metrics">
          <div class="practice-exit-review-metric is-primary">
            <dt>完整样本</dt>
            <dd>{{ exitReview.completed_5d_count || 0 }}</dd>
          </div>
          <div class="practice-exit-review-metric">
            <dt>卖飞</dt>
            <dd>{{ exitReview.sell_fly_5d_count || 0 }}</dd>
          </div>
          <div class="practice-exit-review-metric">
            <dt>避免续亏</dt>
            <dd>{{ exitReview.avoided_loss_5d_count || 0 }}</dd>
          </div>
          <div class="practice-exit-review-metric">
            <dt>换仓后悔</dt>
            <dd>{{ exitReview.replacement_regret_5d_count || 0 }}</dd>
          </div>
        </dl>
        <div
          v-if="exitReview.reentry_completed_5d_count || exitReview.avg_close_return_5d_pct != null || (feedbackPolicy.enabled && feedbackPolicy.action) || feedbackPolicy.parameters || feedbackPolicy.reason"
          class="practice-exit-review-footer"
        >
          <div
            v-if="exitReview.reentry_completed_5d_count || exitReview.avg_close_return_5d_pct != null || (feedbackPolicy.enabled && feedbackPolicy.action)"
            class="practice-exit-review-highlights"
          >
            <span v-if="exitReview.avg_close_return_5d_pct != null">
              原票均值
              <strong :class="Number(exitReview.avg_close_return_5d_pct) >= 0 ? 'up' : 'down'">
                {{ formatPracticeNumber(exitReview.avg_close_return_5d_pct) }}%
              </strong>
            </span>
            <span v-if="exitReview.reentry_completed_5d_count">
              再入 {{ exitReview.reentry_completed_5d_count }} · 放行 {{ exitReview.reentry_allowed_5d_count || 0 }}
            </span>
            <span v-if="feedbackPolicy.enabled && feedbackPolicy.action">
              本轮 {{ feedbackActionLabel }}
            </span>
          </div>
          <details
            v-if="feedbackPolicy.parameters || feedbackPolicy.reason"
            class="practice-exit-review-details"
          >
            <summary>调参详情</summary>
            <div v-if="feedbackPolicy.enabled && feedbackPolicy.parameters" class="practice-exit-review-parameters">
              <span>
                软退出
                <strong>
                  {{ feedbackPolicy.parameters.soft_exit_confirmations }} 次 ·
                  {{ formatPracticeNumber(Number(feedbackPolicy.parameters.soft_exit_reduce_ratio || 0) * 100, 0) }}%
                </strong>
              </span>
              <span>
                换仓优先级差
                <strong>{{ formatPracticeNumber(feedbackPolicy.parameters.replacement_priority_margin, 1) }} 分</strong>
              </span>
            </div>
            <p v-if="feedbackPolicy.reason">{{ feedbackPolicy.reason }}</p>
          </details>
        </div>
      </template>
      <div v-else class="practice-exit-review-error">
        盘后复盘暂不可用（{{ exitReview.error }}）
      </div>
    </section>
    <slot name="chart" />
    <PracticePositions
      :positions="practice.positions || []"
      :sold-stocks="practice.today_sold_stocks || []"
      :total-equity="Number(practice.total_equity || 0)"
      :strategy-meta="strategyMeta"
      :current-date="practice.current_date || practice.trading_calendar?.date || ''"
    >
      <template #candidate-entry><slot name="candidates" /></template>
    </PracticePositions>
    <slot name="activity" />
    <slot name="rule" />
    <div v-if="practice.last_error" class="empty" style="color:#f87171;margin-top:10px">模型/交易错误：{{ practice.last_error }}</div>
    <div v-if="error && !practice.last_error" class="empty" style="color:#f87171;margin-top:10px">模拟账户更新错误：{{ error }}</div>
  </section>
</template>

<style scoped>
.practice-data-readiness {
  margin: 10px 0;
  padding: 11px 13px;
  border: 1px solid var(--yellow-border);
  border-radius: 9px;
  background: var(--yellow-soft);
  color: var(--yellow-text);
  font-size: 12px;
}

.practice-exit-review {
  margin: 0 0 8px;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel2);
  color: var(--muted);
  font-size: 11px;
}

.practice-exit-review-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.practice-exit-review-title {
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.practice-exit-review-title strong {
  color: var(--text);
  font-size: 12px;
  line-height: 1.35;
}

.practice-exit-review-title small {
  color: var(--muted);
  font-size: 10px;
}

.practice-exit-review-status {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 20px;
  padding: 2px 7px;
  border: 1px solid var(--accent-border);
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent-text);
  font-size: 9.5px;
  line-height: 1.35;
  font-weight: 750;
  white-space: nowrap;
}

.practice-exit-review-status i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.practice-exit-review-status[data-state="active"] {
  border-color: var(--green-border);
  background: var(--green-soft);
  color: var(--green-text);
}

.practice-exit-review-status[data-state="learning"],
.practice-exit-review-status[data-state="cooldown"] {
  border-color: var(--yellow-border);
  background: var(--yellow-soft);
  color: var(--yellow-text);
}

.practice-exit-review-status[data-state="disabled"] {
  border-color: var(--line);
  background: var(--panel);
  color: var(--muted);
}

.practice-exit-review-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0;
  margin: 6px 0 0;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
}

.practice-exit-review-metric {
  min-width: 0;
  padding: 4px 6px;
  text-align: center;
}

.practice-exit-review-metric + .practice-exit-review-metric {
  border-left: 1px solid var(--line);
}

.practice-exit-review-metric dt {
  overflow: hidden;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.3;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.practice-exit-review-metric dd {
  display: block;
  margin: 1px 0 0;
  color: var(--text);
  font-size: 14px;
  line-height: 1.25;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}

.practice-exit-review-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 5px 10px;
  min-width: 0;
  margin-top: 5px;
}

.practice-exit-review-highlights {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 5px 10px;
  overflow: hidden;
}

.practice-exit-review-highlights > span {
  flex: 0 0 auto;
  white-space: nowrap;
}

.practice-exit-review-highlights strong.up {
  color: var(--red);
}

.practice-exit-review-highlights strong.down {
  color: var(--green);
}

.practice-exit-review-details {
  flex: 0 0 auto;
  min-width: 0;
}

.practice-exit-review-details summary {
  color: var(--accent-text);
  font-size: 10px;
  line-height: 1.4;
  font-weight: 700;
  cursor: pointer;
  white-space: nowrap;
}

.practice-exit-review-details[open] {
  flex: 1 0 100%;
  width: 100%;
  padding-top: 5px;
  border-top: 1px solid var(--line);
}

.practice-exit-review-parameters {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 5px;
}

.practice-exit-review-parameters > span {
  display: inline-flex;
  gap: 5px;
}

.practice-exit-review-parameters strong {
  color: var(--text);
  font-size: 10px;
}

.practice-exit-review-details p {
  margin: 4px 0 0;
  color: var(--muted);
  overflow-wrap: anywhere;
}

.practice-exit-review-error {
  margin-top: 9px;
  padding: 8px 9px;
  border: 1px solid var(--yellow-border);
  border-radius: 7px;
  background: var(--yellow-soft);
  color: var(--yellow-text);
}

@media (max-width: 720px) {
  .practice-exit-review {
    margin-bottom: 7px;
    padding: 7px 8px;
  }

  .practice-exit-review-head {
    gap: 7px;
  }

  .practice-exit-review-title small {
    display: none;
  }

  .practice-exit-review-status {
    margin-left: auto;
  }

  .practice-exit-review-metrics {
    margin-top: 5px;
  }

  .practice-exit-review-metric {
    padding: 4px 3px;
  }

  .practice-exit-review-metric dt {
    font-size: 9px;
  }

  .practice-exit-review-metric dd {
    font-size: 13px;
  }

  .practice-exit-review-footer {
    flex-wrap: wrap;
    margin-top: 4px;
  }

  .practice-exit-review-highlights {
    gap: 4px 8px;
  }

  .practice-exit-review-highlights > span:nth-child(n + 3) {
    display: none;
  }
}

@media (max-width: 360px) {
  .practice-exit-review-status-prefix {
    display: none;
  }
}

:global(html[data-theme="tongdaxin"]) .practice-exit-review {
  border-radius: 0;
  background: #050505;
}

:global(html[data-theme="tongdaxin"]) .practice-exit-review-metric,
:global(html[data-theme="tongdaxin"]) .practice-exit-review-details[open] {
  border-radius: 0;
  background: #0a0a0a;
}

.practice-data-readiness.is-not_ready {
  border-color: rgba(248, 113, 113, .45);
  background: rgba(248, 113, 113, .08);
  color: #fca5a5;
}

.practice-data-readiness.is-restart_required {
  border-color: rgba(248, 113, 113, .45);
  background: rgba(248, 113, 113, .08);
  color: #fca5a5;
}

.practice-data-readiness.is-ready {
  border-color: var(--green-border);
  background: var(--green-soft);
  color: var(--green-text);
}

.practice-data-readiness-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.practice-data-readiness p {
  margin: 7px 0 0;
}

.practice-data-progress {
  height: 5px;
  margin-top: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(148, 163, 184, .22);
}

.practice-data-progress span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: currentColor;
  transition: width .25s ease;
}
</style>
