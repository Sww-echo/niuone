<script setup>
import CategoryTabs from './CategoryTabs.vue'
import DashboardHeaderActions from './DashboardHeaderActions.vue'
import niuoneLogoUrl from '../assets/niuone-logo.png'
</script>

<template>
  <header class="dashboard-site-header">
    <div class="header-row">
      <h1 class="dashboard-brand">
        <img class="dashboard-brand-logo" :src="niuoneLogoUrl" alt="" aria-hidden="true" />
        <span>牛牛1号</span>
      </h1>
      <DashboardHeaderActions />
    </div>
    <CategoryTabs />
  </header>
</template>

<style src="../../../frontend/dashboard-header.css"></style>
<style src="../../../frontend/tongdaxin-theme.css"></style>
