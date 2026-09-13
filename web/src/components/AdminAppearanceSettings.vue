<script setup>
import { computed } from 'vue'
import { useTheme } from '../composables/useTheme.js'

const {
  cornerStyle,
  isTongdaxin,
  setStandardCornerStyle,
  setStandardTheme,
  setTongdaxinTheme,
  theme,
} = useTheme()

const standardColorOptions = [
  { value: 'light', label: '浅色', description: '适合明亮环境与日间查看' },
  { value: 'dark', label: '深色', description: '适合低光环境与长时间盯盘' },
]
const cornerOptions = [
  { value: 'rounded', label: '圆角', description: '柔和的卡片与控件边缘' },
  { value: 'square', label: '直角', description: '紧凑严肃的金融终端风格' },
]
const tongdaxinOptions = [
  {
    value: 'tongdaxin',
    label: '经典暗色',
    description: '黑底灰表头、红色网格与数字分色',
  },
  {
    value: 'tongdaxin-light',
    label: '浅色 · Windows 95',
    description: '银灰窗口、海军蓝标题栏与立体按钮',
  },
]

const appearanceSummary = computed(() => (
  isTongdaxin.value
    ? (theme.value === 'tongdaxin-light' ? '通达信浅色 · Windows 95' : '通达信经典暗色')
    : `${theme.value === 'dark' ? '深色' : '浅色'} · ${cornerStyle.value === 'rounded' ? '圆角' : '直角'}`
))
</script>

<template>
  <div class="settings-detail appearance-settings-page">
    <nav class="settings-breadcrumbs" aria-label="设置导航">
      <RouterLink class="settings-back-link" to="/admin">
        <span aria-hidden="true">←</span><span>全部设置</span>
      </RouterLink>
    </nav>
    <section class="settings-group appearance-settings-panel" aria-labelledby="appearanceSettingsTitle">
      <div class="settings-group-head appearance-settings-head">
        <div>
          <h2 id="appearanceSettingsTitle">界面主题</h2>
          <p class="settings-group-note">外观偏好即时生效，并保存在当前浏览器。</p>
        </div>
        <span class="appearance-current" aria-live="polite">{{ appearanceSummary }}</span>
      </div>
      <div class="appearance-settings-body">
        <div class="appearance-mode-layout">
          <section
            class="appearance-mode-section appearance-standard-section"
            :class="{ selected: !isTongdaxin }"
            aria-labelledby="standardAppearanceTitle"
          >
            <div class="appearance-mode-head">
              <div>
                <h3 id="standardAppearanceTitle">深浅色与边角样式</h3>
                <p>常规界面，可自由组合颜色和边角。</p>
              </div>
              <span class="appearance-mode-state">{{ isTongdaxin ? '未启用' : '已启用' }}</span>
            </div>
            <div class="appearance-settings-grid">
              <fieldset class="appearance-option-group appearance-theme-options">
                <legend>主题颜色</legend>
                <label
                  v-for="option in standardColorOptions"
                  :key="option.value"
                  class="appearance-option-card"
                  :class="{ selected: !isTongdaxin && theme === option.value }"
                >
                  <input
                    class="appearance-option-input"
                    type="radio"
                    name="appearance-color"
                    :value="option.value"
                    :checked="!isTongdaxin && theme === option.value"
                    @change="setStandardTheme(option.value)"
                  />
                  <span class="appearance-color-sample" :class="option.value" aria-hidden="true">
                    <i /><i />
                  </span>
                  <span class="appearance-option-copy">
                    <strong>{{ option.label }}</strong>
                    <small>{{ option.description }}</small>
                  </span>
                  <span class="appearance-option-check" aria-hidden="true">✓</span>
                </label>
              </fieldset>
              <fieldset class="appearance-option-group">
                <legend>边角样式</legend>
                <label
                  v-for="option in cornerOptions"
                  :key="option.value"
                  class="appearance-option-card"
                  :class="{ selected: !isTongdaxin && cornerStyle === option.value }"
                >
                  <input
                    class="appearance-option-input"
                    type="radio"
                    name="appearance-corners"
                    :value="option.value"
                    :checked="!isTongdaxin && cornerStyle === option.value"
                    @change="setStandardCornerStyle(option.value)"
                  />
                  <span class="appearance-corner-sample" :class="option.value" aria-hidden="true"><i /></span>
                  <span class="appearance-option-copy">
                    <strong>{{ option.label }}</strong>
                    <small>{{ option.description }}</small>
                  </span>
                  <span class="appearance-option-check" aria-hidden="true">✓</span>
                </label>
              </fieldset>
            </div>
          </section>
          <section
            class="appearance-mode-section appearance-tongdaxin-section"
            :class="{ selected: isTongdaxin }"
            aria-labelledby="tongdaxinAppearanceTitle"
          >
            <div class="appearance-mode-head">
              <div>
                <h3 id="tongdaxinAppearanceTitle">通达信模式</h3>
                <p>独立终端主题，不叠加常规颜色和边角样式。</p>
              </div>
              <span class="appearance-mode-state">{{ isTongdaxin ? '已启用' : '未启用' }}</span>
            </div>
            <fieldset class="appearance-option-group appearance-tongdaxin-options">
              <legend>终端配色</legend>
              <label
                v-for="option in tongdaxinOptions"
                :key="option.value"
                class="appearance-option-card appearance-tongdaxin-option"
                :class="{ selected: theme === option.value }"
              >
                <input
                  class="appearance-option-input"
                  type="radio"
                  name="appearance-mode"
                  :value="option.value"
                  :checked="theme === option.value"
                  @change="setTongdaxinTheme(option.value)"
                />
                <span class="appearance-color-sample" :class="option.value" aria-hidden="true"><i /><i /></span>
                <span class="appearance-option-copy">
                  <strong>{{ option.label }}</strong>
                  <small>{{ option.description }}</small>
                </span>
                <span class="appearance-option-check" aria-hidden="true">✓</span>
              </label>
            </fieldset>
          </section>
        </div>
      </div>
    </section>
  </div>
</template>
