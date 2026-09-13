<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import DashboardHeader from './components/DashboardHeader.vue'

const route = useRoute()
const showDashboardHeader = computed(() => route.meta.dashboardHeader === true)
</script>

<template>
  <DashboardHeader v-if="showDashboardHeader" />
  <RouterView />
</template>
