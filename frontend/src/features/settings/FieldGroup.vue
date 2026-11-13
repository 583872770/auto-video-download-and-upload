<script setup lang="ts">
import type { Field } from './fields';
import SettingField from './SettingField.vue';
defineProps<{ title: string; fields: Field[] }>();
</script>
<template>
  <section class="card">
    <h2>{{ title }}</h2>
    <slot />
    <div class="fields">
      <SettingField v-for="field in fields" :key="field.key" :field="field" />
    </div>
  </section>
</template>
