<script setup>
import { computed, onMounted, ref } from 'vue'

const payload = ref(null)
const rawPrompt = ref('')
const activeDraft = ref(null)
const audits = ref([])
const phase = ref('loading')
const message = ref('')
const confirmed = ref(false)
const streamedOutput = ref('')

const activeVersion = computed(() => payload.value?.active_version || null)
const runtimeEnabled = computed(() => payload.value?.runtime_enabled === true)
const versions = computed(() => payload.value?.versions || [])
const dependencies = computed(() => {
  const grouped = activeDraft.value?.execution_plan?.required_features || {}
  const byKey = new Map()
  Object.values(grouped).flat().forEach(item => {
    if (item?.fact_key) byKey.set(item.fact_key, item)
  })
  return Array.from(byKey.values())
})
const canActivate = computed(() => (
  ['pending_confirmation', 'activating'].includes(activeDraft.value?.status)
    && confirmed.value
))

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...options,
  })
  const result = await response.json().catch(() => null)
  if (!response.ok || !result) throw new Error(result?.error || '请求失败')
  return result
}

async function requestEventStream(url, options = {}) {
  const response = await fetch(url, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...options,
  })
  if (!response.ok) {
    const result = await response.json().catch(() => null)
    throw new Error(result?.error || '请求失败')
  }
  if (!response.body) throw new Error('浏览器不支持模型流式输出')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let completedDraft = null
  let streamError = ''

  function consumeEvent(block) {
    if (!block.trim()) return
    let event = 'message'
    const dataLines = []
    block.split('\n').forEach(line => {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
    })
    if (!dataLines.length) return
    let data
    try {
      data = JSON.parse(dataLines.join('\n'))
    } catch {
      streamError = '模型流式响应无法解析'
      return
    }
    if (event === 'started') {
      message.value = '模型已接收策略，正在生成结构化规则…'
    } else if (event === 'delta') {
      streamedOutput.value += String(data?.text || '')
      message.value = `模型生成中，已接收 ${streamedOutput.value.length} 个字符…`
    } else if (event === 'reset') {
      streamedOutput.value = ''
      message.value = String(data?.message || '模型流式输出中断，正在自动重试一次…')
    } else if (event === 'complete') {
      completedDraft = data?.draft || null
    } else if (event === 'error') {
      streamError = String(data?.error || '文字策略细化失败')
    }
  }

  function consumeBuffer(flush = false) {
    buffer = buffer.replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      consumeEvent(buffer.slice(0, boundary))
      buffer = buffer.slice(boundary + 2)
      boundary = buffer.indexOf('\n\n')
    }
    if (flush && buffer.trim()) {
      consumeEvent(buffer)
      buffer = ''
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    consumeBuffer()
  }
  buffer += decoder.decode()
  consumeBuffer(true)
  if (streamError) throw new Error(streamError)
  if (!completedDraft) throw new Error('模型流式响应未正常完成')
  return completedDraft
}

async function refresh() {
  try {
    payload.value = await requestJson('/api/admin/prompt-strategies')
    if (!activeDraft.value) {
      activeDraft.value = (payload.value?.drafts || []).find(
        draft => ['pending_confirmation', 'activating', 'validation_failed'].includes(draft?.status),
      ) || null
    }
    phase.value = ['pending_confirmation', 'activating'].includes(activeDraft.value?.status)
      ? 'review'
      : 'idle'
    message.value = ''
  } catch (error) {
    phase.value = 'error'
    message.value = error instanceof Error ? error.message : '文字策略加载失败'
  }
}

async function refine() {
  if (!rawPrompt.value.trim() || phase.value === 'busy') return
  phase.value = 'busy'
  message.value = '正在创建草案并调用模型细化一次…'
  confirmed.value = false
  streamedOutput.value = ''
  try {
    const created = await requestJson('/api/admin/prompt-strategies/drafts', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-NiuOne-Action': '1',
      },
      body: JSON.stringify({ raw_prompt: rawPrompt.value }),
    })
    const refinedDraft = await requestEventStream(
      `/api/admin/prompt-strategies/drafts/${encodeURIComponent(created.draft.draft_id)}/refine`,
      {
        method: 'POST',
        headers: { 'X-NiuOne-Action': '1' },
      },
    )
    activeDraft.value = refinedDraft
    phase.value = refinedDraft.status === 'pending_confirmation' ? 'review' : 'error'
    const refinementMessage = refinedDraft.status === 'pending_confirmation'
      ? '细化与本地编译已完成。请核对规则、假设、歧义和依赖后再激活。'
      : `本地编译未通过：${(refinedDraft.validation_errors || []).join('；')}`
    await refresh()
    phase.value = ['pending_confirmation', 'activating'].includes(activeDraft.value?.status)
      ? 'review'
      : phase.value
    message.value = refinementMessage
  } catch (error) {
    phase.value = 'error'
    message.value = error instanceof Error ? error.message : '文字策略细化失败'
  }
}

async function activate() {
  if (!canActivate.value || phase.value === 'busy') return
  phase.value = 'busy'
  message.value = '正在冻结并激活结构化版本…'
  try {
    const result = await requestJson(
      `/api/admin/prompt-strategies/drafts/${encodeURIComponent(activeDraft.value.draft_id)}/activate`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-NiuOne-Action': '1',
        },
        body: JSON.stringify({
          confirmed_plan_sha256: activeDraft.value.plan_sha256,
        }),
      },
    )
    activeDraft.value = null
    confirmed.value = false
    rawPrompt.value = ''
    streamedOutput.value = ''
    await refresh()
    phase.value = 'idle'
    message.value = `版本 r${result.version.revision} 已激活；运行期将只执行冻结计划。`
  } catch (error) {
    phase.value = 'error'
    message.value = error instanceof Error ? error.message : '文字策略激活失败'
  }
}

async function loadAudits() {
  if (!activeVersion.value) return
  try {
    const result = await requestJson(
      `/api/admin/prompt-strategies/versions/${encodeURIComponent(activeVersion.value.version_id)}/evaluations?limit=50`,
    )
    audits.value = result.items || []
  } catch (error) {
    message.value = error instanceof Error ? error.message : '审计记录加载失败'
  }
}

onMounted(refresh)
</script>

<template>
  <section class="prompt-strategy-panel" aria-labelledby="promptStrategyTitle">
    <div class="prompt-strategy-head">
      <div>
        <div class="eyebrow">Prompt → 冻结规则 → 本地执行</div>
        <h2 id="promptStrategyTitle">文字策略闭环</h2>
        <p>模型只在创建阶段生成一个成功版本；传输或校验失败最多自动补偿一次。确认激活后，选股、买前复核、持仓监测和卖出均由本地规则引擎执行。</p>
      </div>
      <span v-if="activeVersion && runtimeEnabled" class="prompt-version-badge">运行中 r{{ activeVersion.revision }}</span>
      <span v-else-if="activeVersion" class="prompt-version-badge muted">已冻结，当前未运行</span>
      <span v-else class="prompt-version-badge muted">尚未激活</span>
    </div>

    <div v-if="activeVersion" class="prompt-active-card">
      <strong>{{ activeVersion.refined_spec?.name || '文字策略' }}</strong>
      <span>版本 {{ activeVersion.version_id }}</span>
      <span>计划指纹 {{ activeVersion.plan_sha256 }}</span>
      <span>引擎 {{ activeVersion.engine_version }}</span>
      <button type="button" class="secondary-button" @click="loadAudits">查看最近审计</button>
    </div>

    <label class="prompt-input-label" for="promptStrategyInput">用自然语言描述完整策略</label>
    <textarea
      id="promptStrategyInput"
      v-model="rawPrompt"
      rows="5"
      maxlength="8000"
      placeholder="例如：KDJ 的 J 值低于 0 时从全市场选股并买入，J 值高于 15 时全部卖出，单票使用账户权益 10%，不加仓。"
      :disabled="phase === 'busy'"
    ></textarea>
    <div class="prompt-create-row">
      <span>{{ rawPrompt.length }}/8000</span>
      <button
        type="button"
        class="save-button"
        :disabled="!rawPrompt.trim() || phase === 'busy'"
        @click="refine"
      >{{ phase === 'busy' ? '处理中…' : 'AI 细化一次' }}</button>
    </div>
    <p class="prompt-status" :class="phase" role="status">{{ message }}</p>

    <div v-if="streamedOutput || phase === 'busy'" class="prompt-model-output">
      <div class="prompt-model-output-head">
        <h3>模型实时输出</h3>
        <span v-if="phase === 'busy'">生成中</span>
        <span v-else>已结束</span>
      </div>
      <pre>{{ streamedOutput || '等待模型返回内容…' }}</pre>
    </div>

    <div v-if="activeDraft" class="prompt-review">
      <h3>待确认的结构化规则</h3>
      <dl>
        <div><dt>名称</dt><dd>{{ activeDraft.refined_spec?.name }}</dd></div>
        <div><dt>说明</dt><dd>{{ activeDraft.refined_spec?.description || '—' }}</dd></div>
        <div><dt>执行模式</dt><dd>{{ activeDraft.refined_spec?.execution_mode }}</dd></div>
        <div><dt>候选上限</dt><dd>过滤后 {{ activeDraft.refined_spec?.candidate_limit }} 只</dd></div>
        <div><dt>计划指纹</dt><dd>{{ activeDraft.plan_sha256 }}</dd></div>
      </dl>
      <div class="prompt-review-columns">
        <div>
          <h4>采用的解释与假设</h4>
          <ul><li v-for="item in (activeDraft.refined_spec?.assumptions || [])" :key="item">{{ item }}</li></ul>
          <p v-if="!activeDraft.refined_spec?.assumptions?.length">无</p>
        </div>
        <div>
          <h4>仍需关注的歧义</h4>
          <ul><li v-for="item in (activeDraft.refined_spec?.ambiguities || [])" :key="item">{{ item }}</li></ul>
          <p v-if="!activeDraft.refined_spec?.ambiguities?.length">无</p>
        </div>
      </div>
      <div>
        <h4>实际依赖的特征</h4>
        <div class="prompt-dependencies">
          <code v-for="item in dependencies" :key="item.fact_key">{{ item.fact_key }}</code>
        </div>
      </div>
      <details>
        <summary>查看完整结构化规则与执行计划</summary>
        <pre>{{ JSON.stringify({ strategy_spec: activeDraft.refined_spec, execution_plan: activeDraft.execution_plan }, null, 2) }}</pre>
      </details>
      <label class="prompt-confirm">
        <input v-model="confirmed" type="checkbox">
        <span>我已核对结构化规则、假设、歧义、仓位和退出条件；确认后该版本不可修改。</span>
      </label>
      <button type="button" class="save-button" :disabled="!canActivate || phase === 'busy'" @click="activate">
        确认并激活冻结版本
      </button>
    </div>

    <details v-if="audits.length" class="prompt-audits" open>
      <summary>最近 {{ audits.length }} 条执行审计</summary>
      <div v-for="item in audits" :key="item.evaluation_id" class="prompt-audit-row">
        <span>{{ item.evaluated_at }}</span>
        <code>{{ item.code }}</code>
        <span>{{ item.stage }}</span>
        <b :class="item.status">{{ item.status }}</b>
        <small>{{ item.audit_sha256 }}</small>
      </div>
    </details>

    <details v-if="versions.length" class="prompt-versions">
      <summary>历史版本（{{ versions.length }}）</summary>
      <div v-for="version in versions" :key="version.version_id" class="prompt-version-row">
        <b>r{{ version.revision }} · {{ version.status }}</b>
        <span>{{ version.refined_spec?.name }}</span>
        <code>{{ version.plan_sha256 }}</code>
      </div>
    </details>
  </section>
</template>

<style scoped>
.prompt-strategy-panel { margin-top: 24px; padding: 24px; border: 1px solid var(--line, #d9e0e8); border-radius: 18px; background: var(--panel, #fff); }
.prompt-strategy-head { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }
.prompt-strategy-head h2 { margin: 4px 0 8px; }
.prompt-strategy-head p { margin: 0; max-width: 760px; color: var(--muted, #64748b); line-height: 1.6; }
.prompt-version-badge { white-space: nowrap; padding: 6px 10px; border-radius: 999px; background: #dcfce7; color: #166534; font-weight: 700; }
.prompt-version-badge.muted { background: #eef2f7; color: #64748b; }
.prompt-active-card { display: grid; gap: 6px; margin: 20px 0; padding: 16px; border-radius: 12px; background: color-mix(in srgb, var(--accent, #2563eb) 7%, transparent); overflow-wrap: anywhere; }
.prompt-active-card span { font-size: .86rem; color: var(--muted, #64748b); }
.prompt-input-label { display: block; margin: 20px 0 8px; font-weight: 700; }
textarea { box-sizing: border-box; width: 100%; resize: vertical; border: 1px solid var(--line, #cbd5e1); border-radius: 12px; padding: 13px 14px; color: inherit; background: transparent; font: inherit; line-height: 1.6; }
.prompt-create-row { display: flex; justify-content: flex-end; align-items: center; gap: 14px; margin-top: 10px; }
.prompt-create-row span { color: var(--muted, #64748b); font-size: .82rem; }
.prompt-status { min-height: 1.5em; color: var(--muted, #64748b); }
.prompt-status.error { color: #b91c1c; }
.prompt-model-output { margin-top: 16px; }
.prompt-model-output-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.prompt-model-output-head h3 { margin: 0 0 8px; }
.prompt-model-output-head span { color: var(--muted, #64748b); font-size: .82rem; }
.prompt-model-output pre { min-height: 96px; margin-top: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.prompt-review { margin-top: 22px; padding-top: 22px; border-top: 1px solid var(--line, #d9e0e8); }
.prompt-review dl { display: grid; gap: 8px; }
.prompt-review dl div { display: grid; grid-template-columns: 100px 1fr; gap: 12px; }
.prompt-review dt { color: var(--muted, #64748b); }
.prompt-review dd { margin: 0; overflow-wrap: anywhere; }
.prompt-review-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.prompt-review-columns > div { padding: 14px; border-radius: 12px; background: color-mix(in srgb, var(--line, #cbd5e1) 25%, transparent); }
.prompt-dependencies { display: flex; flex-wrap: wrap; gap: 8px; }
.prompt-dependencies code { padding: 5px 8px; border-radius: 8px; background: #eef2ff; color: #3730a3; overflow-wrap: anywhere; }
details { margin-top: 16px; }
summary { cursor: pointer; font-weight: 700; }
pre { max-height: 440px; overflow: auto; padding: 14px; border-radius: 10px; background: #0f172a; color: #e2e8f0; font-size: .78rem; }
.prompt-confirm { display: flex; gap: 10px; align-items: flex-start; margin: 18px 0 12px; line-height: 1.5; }
.secondary-button { justify-self: start; margin-top: 6px; }
.prompt-audit-row, .prompt-version-row { display: grid; grid-template-columns: 150px 80px 80px 70px minmax(160px, 1fr); gap: 10px; align-items: center; padding: 9px 0; border-bottom: 1px solid var(--line, #e2e8f0); font-size: .84rem; overflow-wrap: anywhere; }
.prompt-audit-row b.true { color: #15803d; }
.prompt-audit-row b.false { color: #b91c1c; }
.prompt-audit-row b.unknown { color: #a16207; }
.prompt-version-row { grid-template-columns: 120px minmax(140px, 1fr) minmax(220px, 2fr); }
@media (max-width: 720px) {
  .prompt-strategy-panel { padding: 18px; }
  .prompt-strategy-head { display: grid; }
  .prompt-review-columns { grid-template-columns: 1fr; }
  .prompt-audit-row, .prompt-version-row { grid-template-columns: 1fr; gap: 3px; }
}
</style>
