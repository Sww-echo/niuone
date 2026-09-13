<script setup>
import { computed } from 'vue'

const props = defineProps({
  config: {
    type: Object,
    required: true,
  },
})

const appearanceGroup = Object.freeze({
  slug: 'appearance',
  name: '界面主题',
  summary: '在常规外观与互斥的通达信模式之间切换。',
  item_count: 2,
})

const groups = computed(() => {
  const entries = Array.isArray(props.config.groups) ? [...props.config.groups] : []
  if (entries.some(group => group.slug === appearanceGroup.slug)) return entries
  const aboutIndex = entries.findIndex(group => group.slug === 'about')
  entries.splice(aboutIndex < 0 ? entries.length : aboutIndex, 0, appearanceGroup)
  return entries
})
</script>

<template>
  <div class="settings-index">
    <nav class="settings-grid" aria-label="设置分组">
      <RouterLink
        v-for="group in groups"
        :key="group.slug"
        class="settings-card"
        :to="`/admin/settings/${group.slug}`"
        :aria-label="`进入${group.name}设置`"
        :title="group.summary || '维护该分组的业务配置。'"
      >
        <span class="settings-card-copy">
          <span class="settings-card-title">{{ group.name }}</span>
          <span class="settings-card-summary">{{ group.summary || '维护该分组的业务配置。' }}</span>
          <span class="settings-card-meta">{{ Number(group.item_count || 0) }} 项设置</span>
        </span>
        <span class="settings-card-arrow" aria-hidden="true">›</span>
      </RouterLink>
    </nav>
  </div>
</template>
