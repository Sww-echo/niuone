<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import {
  commonReasoningEfforts,
  reasoningCapabilityForModel,
} from '../utils/modelReasoning.js'

const props = defineProps({
  item: {
    type: Object,
    required: true,
  },
  reasoningModel: {
    type: String,
    default: '',
  },
  reasoningCapabilities: {
    type: Array,
    default: () => [],
  },
})
const emit = defineEmits(['field-change'])

const name = computed(() => String(props.item.name || ''))
const label = computed(() => String(props.item.label || props.item.name || '设置项'))
const fieldName = computed(() => `env__${name.value}`)
const value = computed(() => String(props.item.file_value ?? ''))
const kind = computed(() => String(props.item.kind || 'text'))
const reasoningValue = ref(value.value)
const normalizedReasoningModel = computed(() => String(props.reasoningModel || '').trim().toLowerCase())
const reasoningCapability = computed(() => reasoningCapabilityForModel(
  normalizedReasoningModel.value,
  props.reasoningCapabilities,
))
const reasoningOptions = computed(() => reasoningCapability.value?.accepted_efforts || [])
const commonReasoningOptions = computed(() => commonReasoningEfforts(props.reasoningCapabilities))
const reasoningListId = computed(() => `${fieldName.value}__options`)
const reasoningValueIsUnsupported = computed(() => (
  Boolean(reasoningValue.value)
  && Boolean(reasoningCapability.value)
  && !reasoningOptions.value.includes(reasoningValue.value)
))
const boolNoDefault = computed(() => Boolean(props.item.bool_no_default))
const boolValue = computed(() => {
  const normalized = (
    value.value.trim() || (boolNoDefault.value ? String(props.item.default ?? '').trim() : '')
  ).toLowerCase()
  if (['1', 'true', 'yes', 'on'].includes(normalized)) return '1'
  return normalized === '' ? '' : '0'
})
const apiMode = computed(() => (
  ['auto', 'responses', 'chat'].includes(value.value) ? value.value : 'auto'
))
const streamMode = computed(() => (
  ['auto', 'stream', 'non_stream'].includes(value.value) ? value.value : 'auto'
))
const playbackSpeed = computed(() => (
  ['0.5', '0.75', '1', '1.5', '2', '5', '10'].includes(value.value) ? value.value : '0.5'
))
const listValues = ref([
  ...(props.item.time_values || []),
])
const strategyOptions = computed(() => (
  kind.value === 'strategy_suite'
    ? (props.item.strategy_suite_options || [])
    : (props.item.strategy_source_options || [])
))
const selectedStrategies = computed(() => new Set(props.item.strategy_values || []))
const selectedUniverse = computed(() => new Set(props.item.stock_universe_values || []))
const newsSourceQuery = ref('')
const newsSourceValues = ref([...(props.item.news_source_values || [])])
let newsSourceServerValue = newsSourceValues.value.join(',')
const newsSourceOptions = computed(() => props.item.news_source_options || [])
const newsSourceGroups = computed(() => {
  const query = newsSourceQuery.value.trim().toLocaleLowerCase()
  const grouped = new Map()
  newsSourceOptions.value.forEach(option => {
    const searchable = [option.id, option.label, option.category_label]
      .map(entry => String(entry || '').toLocaleLowerCase())
      .join(' ')
    if (query && !searchable.includes(query)) return
    const category = String(option.category || 'other')
    if (!grouped.has(category)) {
      grouped.set(category, {
        id: category,
        label: String(option.category_label || '其他'),
        options: [],
      })
    }
    grouped.get(category).options.push(option)
  })
  return [...grouped.values()]
})
const textPreset = computed(() => kind.value === 'preset_strategy_text')
const textMaxChars = computed(() => Number(
  textPreset.value
    ? props.item.preset_strategy_max_chars
    : props.item.trade_discipline_max_chars,
) || 4000)

async function addListValue() {
  listValues.value.push('')
  await nextTick()
  emit('field-change')
}

async function removeListValue(index) {
  listValues.value.splice(index, 1)
  await nextTick()
  emit('field-change')
}

async function resetNewsSources() {
  newsSourceValues.value = [...(props.item.news_source_default_values || [])]
  await nextTick()
  emit('field-change')
}

function newsSourceIntervalText(value) {
  const seconds = Number(value || 0)
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} 分钟`
  return `${seconds} 秒`
}

watch(
  () => (props.item.news_source_values || []).join(','),
  nextValue => {
    if (newsSourceValues.value.join(',') === newsSourceServerValue) {
      newsSourceValues.value = nextValue ? nextValue.split(',') : []
    }
    newsSourceServerValue = nextValue
  },
)
</script>

<template>
  <input
    v-if="item.secret"
    type="password"
    :name="fieldName"
    :aria-label="label"
    :placeholder="item.file_state || '未设置'"
    autocomplete="new-password"
  >

  <select
    v-else-if="kind === 'bool'"
    :name="fieldName"
    :aria-label="label"
    :value="boolValue"
  >
    <option v-if="!boolNoDefault" value="">默认</option>
    <option value="1">启用</option>
    <option value="0">停用</option>
  </select>

  <template v-else-if="kind === 'api_mode'">
    <select :name="fieldName" :aria-label="label" :value="apiMode">
      <option value="auto">自动</option>
      <option value="responses">Responses API（搜索工具）</option>
      <option value="chat">Chat Completions（兼容模式）</option>
    </select>
    <div class="config-meta">自动模式下，Grok 4.3/4.5、MiMo 2.5 和常用 Qwen Responses 型号使用 Responses API，其他模型保持 Chat Completions</div>
  </template>

  <template v-else-if="kind === 'reasoning_effort'">
    <select
      v-if="reasoningCapability"
      v-model="reasoningValue"
      :name="fieldName"
      :aria-label="label"
    >
      <option value="">留空（使用模型默认）</option>
      <option
        v-if="reasoningValueIsUnsupported"
        :value="reasoningValue"
        disabled
      >{{ reasoningValue }}（当前模型不支持）</option>
      <option v-for="effort in reasoningOptions" :key="effort" :value="effort">
        {{ effort }}
      </option>
    </select>
    <input
      v-else
      v-model="reasoningValue"
      type="text"
      :name="fieldName"
      :aria-label="label"
      :list="normalizedReasoningModel ? reasoningListId : null"
      maxlength="64"
      placeholder="留空使用默认，例如 high、max、enabled"
      autocomplete="off"
      autocapitalize="none"
      spellcheck="false"
    >
    <datalist v-if="normalizedReasoningModel && !reasoningCapability" :id="reasoningListId">
      <option v-for="effort in commonReasoningOptions" :key="effort" :value="effort" />
    </datalist>
    <div v-if="reasoningCapability" class="config-meta">
      已知常见模型会按本地能力表校验。已识别 {{ reasoningModel }}：{{ reasoningOptions.length ? `可选 ${reasoningOptions.join('、')}` : '固定思考模式，仅可留空' }}；留空使用模型默认
    </div>
    <div v-else-if="normalizedReasoningModel" class="config-meta">未匹配本地能力表，已列出常见候选值；仍可填写网关自定义强度并手动测试</div>
    <div v-else class="config-meta">填写模型名称后会列出该模型全部可选思考强度；留空不发送参数</div>
  </template>

  <template v-else-if="kind === 'stream_mode'">
    <select :name="fieldName" :aria-label="label" :value="streamMode">
      <option value="auto">自动（推荐）</option>
      <option value="stream">流式</option>
      <option value="non_stream">非流式</option>
    </select>
    <div class="config-meta">自动模式默认使用非流式；如果网关明确要求 stream=true，会自动切换为流式。流式内容会在后端拼接完整后再校验和使用</div>
  </template>

  <template v-else-if="kind === 'playback_speed'">
    <select :name="fieldName" :aria-label="label" :value="playbackSpeed">
      <option v-for="speed in ['0.5', '0.75', '1', '1.5', '2', '5', '10']" :key="speed" :value="speed">
        {{ speed }}x
      </option>
    </select>
    <div class="config-meta">控制资金流页面首次播放和重播速度</div>
  </template>

  <template v-else-if="kind === 'cron_time' || kind === 'time'">
    <input type="time" :name="fieldName" :aria-label="label" :value="value">
    <div class="config-meta">
      北京时间<span v-if="kind === 'cron_time' && item.day_label"> · {{ item.day_label }}</span>
    </div>
  </template>

  <template v-else-if="kind === 'time_list'">
    <div
      class="time-list-control"
      data-time-list
      :data-field-name="fieldName"
      data-input-type="time"
      :data-input-label="label"
    >
      <input type="hidden" :name="fieldName" value="">
      <div class="time-list-grid" data-time-list-items>
        <div v-for="(entry, index) in listValues" :key="index" class="time-list-item">
          <input
            type="time"
            :name="fieldName"
            :aria-label="`${label} ${index + 1}`"
            v-model="listValues[index]"
          >
          <button
            type="button"
            class="time-list-remove"
            data-time-list-remove
            aria-label="删除时间点"
            @click.stop="removeListValue(index)"
          >x</button>
        </div>
      </div>
      <button
        type="button"
        class="time-list-add"
        data-time-list-add
        aria-label="添加时间点"
        @click.stop="addListValue"
      >+</button>
    </div>
    <div class="config-meta">北京时间</div>
  </template>

  <template v-else-if="kind === 'news_sources'">
    <div class="news-source-picker" data-news-source-picker>
      <input type="hidden" :name="fieldName" value="">
      <div class="news-source-toolbar">
        <input
          v-model="newsSourceQuery"
          type="search"
          :aria-label="`搜索${label}`"
          placeholder="搜索来源名称"
          autocomplete="off"
          @input.stop
        >
        <span class="news-source-count" aria-live="polite">
          已选择 <b>{{ newsSourceValues.length }}</b> / {{ newsSourceOptions.length }}
        </span>
        <button type="button" class="news-source-reset" @click.stop="resetNewsSources">
          恢复默认来源
        </button>
      </div>
      <div class="news-source-groups" role="group" :aria-label="label">
        <section
          v-for="group in newsSourceGroups"
          :key="group.id"
          class="news-source-group"
          :data-news-source-category="group.id"
        >
          <div class="news-source-options">
            <label
              v-for="option in group.options"
              :key="option.id"
              class="news-source-option"
              :class="{selected: newsSourceValues.includes(option.id)}"
            >
              <input
                v-model="newsSourceValues"
                type="checkbox"
                :name="fieldName"
                :value="option.id"
                :aria-label="`${label}：${option.label || option.id}`"
              >
              <span class="news-source-option-copy">
                <strong>{{ option.label || option.id }}</strong>
                <small>上游约 {{ newsSourceIntervalText(option.interval_seconds) }}更新</small>
              </span>
            </label>
          </div>
        </section>
        <div v-if="!newsSourceGroups.length" class="news-source-empty">
          没有匹配的数据源
        </div>
      </div>
    </div>
    <div class="config-meta">至少选择一项；来源越多，首次刷新耗时和上游请求量越大</div>
  </template>

  <template v-else-if="kind === 'stock_universe'">
    <div class="strategy-multi-control">
      <input type="hidden" :name="fieldName" value="">
      <label
        v-for="option in (item.stock_universe_options || [])"
        :key="option.id"
        class="strategy-option"
        :style="{'--strategy-color': option.color || '#94a3b8'}"
      >
        <input
          type="checkbox"
          :name="fieldName"
          :value="option.id"
          :checked="selectedUniverse.has(option.id)"
          :aria-label="`${label}：${option.label || option.id}`"
        >
        <span class="strategy-option-main">
          <span class="strategy-option-title"><span class="strategy-option-dot" />{{ option.label || option.id }}</span>
          <span class="strategy-option-desc">{{ option.desc || '' }}</span>
        </span>
      </label>
    </div>
    <div class="config-meta">至少选择一项；ST 为跨板块独立范围，卖出已有持仓不受此设置限制</div>
  </template>

  <template v-else-if="kind === 'strategy_source' || kind === 'strategy_suite'">
    <div class="strategy-multi-control">
      <div
        v-for="option in strategyOptions"
        :key="option.id"
        class="strategy-option-row"
        :style="{'--strategy-color': option.color || '#94a3b8'}"
      >
        <label class="strategy-option">
          <input
            type="radio"
            :name="fieldName"
            :value="option.id"
            :checked="value === option.id"
            :aria-label="`${label}：${option.label || option.id}`"
            data-strategy-source-toggle
          >
          <span class="strategy-option-main">
            <span class="strategy-option-title"><span class="strategy-option-dot" />{{ option.label || option.id }}</span>
            <span class="strategy-option-desc">{{ option.desc || '' }}</span>
          </span>
        </label>
        <RouterLink
          v-if="kind === 'strategy_suite'"
          class="strategy-backtest-link"
          :to="`/admin/backtest/${encodeURIComponent(option.id)}`"
          :aria-label="`回测${option.label || option.id}`"
          @click.stop
        >回测</RouterLink>
      </div>
    </div>
    <div class="config-meta">每轮只启用一套完整策略；候选、买入、卖出和仓位规则互不混用</div>
  </template>

  <template v-else-if="kind === 'preset_strategy_text' || kind === 'trade_discipline_text'">
    <textarea
      :class="textPreset ? 'preset-strategy-textarea' : 'trade-discipline-textarea'"
      :name="fieldName"
      :aria-label="label"
      :maxlength="textMaxChars"
      spellcheck="false"
      :placeholder="textPreset ? '例如：只做主线强趋势回踩，买入后跌破5日线离场。' : '留空时使用内置交易纪律'"
      :value="value"
    />
    <div class="config-meta">
      {{ textPreset ? '激活后由买卖决策模型优化为选股、买入、卖出和仓位规则' : '直接写入买卖决策模型 prompt 的“必须遵守”段' }}
    </div>
  </template>

  <template v-else-if="kind === 'strategy_multi' || kind === 'strategy_single'">
    <div class="strategy-multi-control">
      <input type="hidden" :name="fieldName" value="">
      <label
        v-for="option in (item.strategy_options || [])"
        :key="option.id"
        class="strategy-option"
        :style="{'--strategy-color': option.color || '#94a3b8'}"
      >
        <input
          :type="kind === 'strategy_single' ? 'radio' : 'checkbox'"
          :name="fieldName"
          :value="option.id"
          :checked="selectedStrategies.has(option.id)"
          :aria-label="`${label}：${option.label || option.id}`"
        >
        <span class="strategy-option-main">
          <span class="strategy-option-title"><span class="strategy-option-dot" />{{ option.label || option.id }}</span>
          <span class="strategy-option-desc">{{ option.desc || '' }}</span>
        </span>
      </label>
    </div>
    <div class="config-meta">每次只启用一个内置策略</div>
  </template>

  <template v-else-if="kind === 'context_length' || kind === 'max_tokens'">
    <input
      type="text"
      :name="fieldName"
      :aria-label="label"
      :value="value"
      :placeholder="kind === 'context_length' ? '默认 128000；例如 128K、1M 或 1000000' : '默认 4096；例如 2048 或 8192'"
      inputmode="numeric"
    >
    <div class="config-meta">
      {{ kind === 'context_length' ? '默认 128000 tokens；填写后保存为数字 tokens' : '默认 4096 tokens；按所选接口映射为兼容的输出长度参数' }}
    </div>
  </template>

  <input
    v-else
    :type="kind === 'int' ? 'number' : 'text'"
    :name="fieldName"
    :aria-label="label"
    :value="value"
    :min="kind === 'int' && item.min ? item.min : null"
    :max="kind === 'int' && item.max ? item.max : null"
  >
</template>
