<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import LastUpdated from './LastUpdated.vue'
import VersionStatus from './VersionStatus.vue'

const route = useRoute()
const settingsActive = computed(() => (
  route.path === '/admin' || route.path.startsWith('/admin/')
))
</script>

<template>
  <div class="header-actions">
    <a class="header-link" href="https://github.com/kunkundi/niuone" target="_blank" rel="noopener noreferrer" title="开源仓库" aria-label="打开 GitHub 开源仓库">
      <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
        <path d="M8 0C3.58 0 0 3.67 0 8.2c0 3.62 2.29 6.69 5.47 7.78.4.08.55-.18.55-.4 0-.2-.01-.85-.01-1.55-2.01.38-2.53-.5-2.69-.96-.09-.24-.48-.96-.82-1.15-.28-.15-.68-.52-.01-.53.63-.01 1.08.59 1.23.84.72 1.24 1.87.89 2.33.68.07-.53.28-.89.51-1.1-1.78-.21-3.64-.91-3.64-4.03 0-.89.31-1.62.82-2.19-.08-.21-.36-1.04.08-2.16 0 0 .67-.22 2.2.84A7.42 7.42 0 0 1 8 3.99c.68 0 1.36.09 2 .28 1.53-1.06 2.2-.84 2.2-.84.44 1.12.16 1.95.08 2.16.51.57.82 1.29.82 2.19 0 3.13-1.87 3.82-3.65 4.03.29.26.54.75.54 1.51 0 1.1-.01 1.98-.01 2.25 0 .22.15.48.55.4A8.12 8.12 0 0 0 16 8.2C16 3.67 12.42 0 8 0Z" />
      </svg>
      <span>GitHub</span>
    </a>
    <VersionStatus />
    <RouterLink
      class="settings-link"
      :class="{ active: settingsActive }"
      to="/admin"
      title="进入设置页"
      aria-label="进入设置页"
      :aria-current="settingsActive ? 'page' : undefined"
    >设置</RouterLink>
    <LastUpdated />
  </div>
</template>
