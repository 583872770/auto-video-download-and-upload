<script setup lang="ts">
import { computed } from 'vue';
import { useSettings } from '../../stores/settings';
import { constraints, type ConsoleConfig } from '../../generated/config';
import type { Field } from './fields';
const props = defineProps<{ field: Field; disabled?: boolean }>();
const store = useSettings();
const value = computed(() => store.draft[props.field.key]);
const range = computed(() => constraints[props.field.key]);
const configured = computed(
  () => props.field.password && store.draft[`${props.field.key}_configured` as keyof ConsoleConfig],
);
function change(event: Event) {
  const input = event.target as HTMLInputElement;
  const selected = props.field.options?.find(([key]) => String(key) === input.value);
  const next = selected
    ? selected[0]
    : range.value
      ? input.value === ''
        ? NaN
        : Number(input.value)
      : input.value;
  store.update(props.field.key, next as ConsoleConfig[typeof props.field.key]);
}
</script>

<template>
  <div class="field" :class="{ invalid: store.issues[field.key] }">
    <label :for="'setting-' + field.key">{{ field.label }}</label>
    <select
      v-if="field.options"
      :id="'setting-' + field.key"
      :name="field.key"
      :value="String(value)"
      :disabled="disabled"
      :aria-describedby="field.key + '-help'"
      @change="change"
    >
      <option v-for="[key, label] in field.options" :key="String(key)" :value="String(key)">
        {{ label }}
      </option>
    </select>
    <input
      v-else
      :id="'setting-' + field.key"
      :name="field.key"
      :value="value"
      :type="field.password ? 'password' : range ? 'number' : 'text'"
      :min="range?.[0]"
      :max="range?.[1]"
      :step="range ? 1 : undefined"
      :disabled="disabled"
      :placeholder="configured ? '已配置，留空保留' : field.placeholder"
      :autocomplete="field.password ? 'new-password' : 'off'"
      :aria-invalid="!!store.issues[field.key]"
      :aria-describedby="field.key + '-help'"
      @input="change"
    />
    <small :id="field.key + '-help'">{{ store.issues[field.key] || field.help }}</small>
  </div>
</template>
