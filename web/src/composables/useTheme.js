import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const THEME_STORAGE_KEY = 'niuone-dashboard-theme-v1'
const STANDARD_THEME_STORAGE_KEY = 'niuone-dashboard-standard-theme-v1'
const CORNER_STORAGE_KEY = 'niuone-dashboard-corners-v1'
const SUPPORTED_THEMES = new Set(['light', 'dark', 'tongdaxin', 'tongdaxin-light'])
const STANDARD_THEMES = new Set(['light', 'dark'])

function isTongdaxinTheme(value) {
  return value === 'tongdaxin' || value === 'tongdaxin-light'
}

function documentTheme() {
  const value = document.documentElement.dataset.theme || ''
  if (
    value === 'tongdaxin'
    && document.documentElement.dataset.tongdaxinPalette === 'light'
  ) {
    return 'tongdaxin-light'
  }
  return SUPPORTED_THEMES.has(value) ? value : 'light'
}

function storedStandardTheme() {
  try {
    const value = localStorage.getItem(STANDARD_THEME_STORAGE_KEY) || ''
    return STANDARD_THEMES.has(value) ? value : ''
  } catch (error) {
    return ''
  }
}

function storedCornerStyle() {
  try {
    const value = localStorage.getItem(CORNER_STORAGE_KEY) || ''
    return value === 'rounded' || value === 'square' ? value : ''
  } catch (error) {
    return ''
  }
}

function documentCornerStyle() {
  return document.documentElement.dataset.corners === 'rounded' ? 'rounded' : 'square'
}

const initialTheme = documentTheme()
const theme = ref(initialTheme)
const standardTheme = ref(
  STANDARD_THEMES.has(initialTheme)
    ? initialTheme
    : (storedStandardTheme() || 'light'),
)
const cornerStyle = ref(documentCornerStyle())

export function useTheme() {
  function applyTheme(nextTheme, persist = false) {
    const normalized = SUPPORTED_THEMES.has(nextTheme) ? nextTheme : 'light'
    theme.value = normalized
    document.documentElement.dataset.theme = isTongdaxinTheme(normalized)
      ? 'tongdaxin'
      : normalized
    if (isTongdaxinTheme(normalized)) {
      document.documentElement.dataset.tongdaxinPalette = normalized === 'tongdaxin-light'
        ? 'light'
        : 'dark'
    } else {
      delete document.documentElement.dataset.tongdaxinPalette
    }
    if (STANDARD_THEMES.has(normalized)) {
      standardTheme.value = normalized
    }
    if (persist) {
      try {
        localStorage.setItem(THEME_STORAGE_KEY, normalized)
        if (STANDARD_THEMES.has(normalized)) {
          localStorage.setItem(STANDARD_THEME_STORAGE_KEY, normalized)
        } else {
          localStorage.setItem(STANDARD_THEME_STORAGE_KEY, standardTheme.value)
        }
      } catch (error) {
        // A blocked storage API must not prevent the visible theme change.
      }
    }
  }

  function applyCornerStyle(nextStyle, persist = false) {
    const normalized = nextStyle === 'rounded' ? 'rounded' : 'square'
    cornerStyle.value = normalized
    document.documentElement.dataset.corners = normalized
    if (persist) {
      try {
        localStorage.setItem(CORNER_STORAGE_KEY, normalized)
      } catch (error) {
        // A blocked storage API must not prevent the visible corner change.
      }
    }
  }

  function setTheme(nextTheme) {
    applyTheme(nextTheme, true)
  }

  function setCornerStyle(nextStyle) {
    applyCornerStyle(nextStyle, true)
  }

  function setStandardTheme(nextTheme) {
    applyTheme(nextTheme === 'light' ? 'light' : 'dark', true)
  }

  function setStandardCornerStyle(nextStyle) {
    if (isTongdaxinTheme(theme.value)) {
      applyTheme(standardTheme.value, true)
    }
    applyCornerStyle(nextStyle, true)
  }

  function setTongdaxinTheme(nextTheme = 'tongdaxin') {
    applyTheme(nextTheme === 'tongdaxin-light' ? 'tongdaxin-light' : 'tongdaxin', true)
  }

  function handleStorage(event) {
    if (
      event.key === THEME_STORAGE_KEY
      && SUPPORTED_THEMES.has(event.newValue)
    ) {
      applyTheme(event.newValue)
    }
    if (
      event.key === CORNER_STORAGE_KEY
      && (event.newValue === 'rounded' || event.newValue === 'square')
    ) {
      applyCornerStyle(event.newValue)
    }
    if (
      event.key === STANDARD_THEME_STORAGE_KEY
      && STANDARD_THEMES.has(event.newValue)
    ) {
      standardTheme.value = event.newValue
    }
  }

  onMounted(() => {
    applyTheme(documentTheme())
    applyCornerStyle(storedCornerStyle() || documentCornerStyle())
    window.addEventListener('storage', handleStorage)
  })

  onBeforeUnmount(() => {
    window.removeEventListener('storage', handleStorage)
  })

  return {
    cornerStyle: computed(() => cornerStyle.value),
    isSquare: computed(() => cornerStyle.value === 'square'),
    isTongdaxin: computed(() => isTongdaxinTheme(theme.value)),
    setCornerStyle,
    setStandardCornerStyle,
    setStandardTheme,
    setTheme,
    setTongdaxinTheme,
    standardTheme: computed(() => standardTheme.value),
    theme: computed(() => theme.value),
  }
}
