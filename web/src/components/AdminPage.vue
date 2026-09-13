<script setup>
import { computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import AdminLogin from './AdminLogin.vue'
import AdminAppearanceSettings from './AdminAppearanceSettings.vue'
import AdminSettingsGroup from './AdminSettingsGroup.vue'
import AdminSettingsIndex from './AdminSettingsIndex.vue'
import { useAdminConfig } from '../composables/useAdminConfig.js'

document.title = '牛牛1号'
const { state, config, errorMessage, refresh, authenticate } = useAdminConfig()
const route = useRoute()
const groupSlug = computed(() => String(route.params.group || ''))
const isAppearanceSettings = computed(() => groupSlug.value === 'appearance')
const activeGroup = computed(() => (
  (config.value?.groups || []).find(group => group.slug === groupSlug.value) || null
))
let pendingConfig = null

function setTitle(title) {
  document.title = title === '设置' ? '牛牛1号 · 设置' : `牛牛1号 · ${title}`
}

watch(state, value => {
  if (value === 'login') setTitle('设置验证')
  else if (value === 'error') setTitle('设置加载失败')
})

watch([state, groupSlug], ([currentState, slug]) => {
  if (currentState !== 'ready' || !config.value) return
  if (!slug) {
    setTitle('设置')
    return
  }
  if (isAppearanceSettings.value) {
    setTitle('界面主题')
    return
  }
  setTitle(activeGroup.value?.name || '设置分组不存在')
}, { flush: 'post' })

watch(groupSlug, () => {
  if (!pendingConfig) return
  config.value = pendingConfig
  pendingConfig = null
})

function acceptUpdatedConfig(updated) {
  if (!updated || !Array.isArray(updated.items)) return
  if (!groupSlug.value) config.value = updated
  else pendingConfig = updated
}

function publishLastUpdated(date = new Date()) {
  const value = [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map(part => String(part).padStart(2, '0'))
    .join(':')
  window.dispatchEvent(new CustomEvent('niuone:last-updated', { detail: { value } }))
}

async function loadConfig() {
  if (await refresh()) publishLastUpdated()
}

async function authenticateAndRefresh(credential) {
  const authenticated = await authenticate(credential)
  if (authenticated) publishLastUpdated()
  return authenticated
}

onMounted(loadConfig)
</script>

<template>
  <main
    id="adminApp"
    class="admin-main"
    aria-live="polite"
    :aria-busy="state === 'loading' ? 'true' : 'false'"
  >
    <div v-if="state === 'loading'" class="admin-loading">设置加载中…</div>
    <AdminLogin v-else-if="state === 'login'" :authenticate="authenticateAndRefresh" />
    <div v-else-if="state === 'error'" class="errmsg">{{ errorMessage || '设置加载失败' }}</div>
    <AdminSettingsIndex
      v-else-if="state === 'ready' && !groupSlug && config"
      :config="config"
    />
    <AdminAppearanceSettings
      v-else-if="state === 'ready' && isAppearanceSettings && config"
    />
    <AdminSettingsGroup
      v-else-if="state === 'ready' && groupSlug && config"
      :key="groupSlug"
      :config="config"
      :slug="groupSlug"
      @config-updated="acceptUpdatedConfig"
    />
  </main>
</template>

<style src="../../../frontend/admin.css"></style>
<style src="../../../frontend/tongdaxin-theme.css"></style>
