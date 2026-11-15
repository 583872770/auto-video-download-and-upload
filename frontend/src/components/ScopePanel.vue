<script setup lang="ts">
import { computed } from 'vue';
const props = withDefaults(defineProps<{
  level: 'global' | 'instance' | 'task';
  purpose?: 'view' | 'configure' | 'operate';
  target: string;
  description: string;
}>(), { purpose: 'configure' });
const label = computed(() => props.purpose === 'view'
  ? ({ global: '全局视图', instance: '实例视图', task: '任务视图' })[props.level]
  : ({ global: '全局', instance: '实例', task: '任务' })[props.level] + (props.purpose === 'operate' ? '操作' : '配置'));
const verb = computed(() => ({ view: '查看范围', configure: '正在配置', operate: '操作对象' })[props.purpose]);
</script>
<template>
  <section class="scope-panel" :data-scope="level" :data-scope-purpose="purpose" :aria-label="label">
    <div class="scope-panel-copy">
      <span class="scope-panel-badge">{{ label }}</span>
      <div><strong>{{ verb }}：{{ target }}</strong><p>{{ description }}</p></div>
    </div>
    <div v-if="$slots.default" class="scope-panel-actions"><slot /></div>
  </section>
</template>
<style scoped>
.scope-panel { display:flex; align-items:center; justify-content:space-between; gap:20px; padding:15px 18px; margin:0 0 20px; border:1px solid #dce7e6; border-radius:12px; background:#f6faf9; color:#263e3b; }
.scope-panel-copy { display:flex; align-items:flex-start; gap:14px; min-width:0; }
.scope-panel-copy strong { display:block; font-size:14px; overflow-wrap:anywhere; }
.scope-panel-copy p { margin:4px 0 0; font-size:12px; line-height:1.6; color:#677b78; }
.scope-panel-badge { flex-shrink:0; font-size:11px; font-weight:650; padding:4px 8px; border-radius:6px; background:#e4f0ed; color:#24675b; white-space:nowrap; }
[data-scope="global"] { background:#f8f9fc; border-color:#e1e5ed; }
[data-scope="global"] .scope-panel-badge { background:#e9edf5; color:#4e607d; }
[data-scope="task"] { background:#f8fafc; border-color:#dfe8ef; }
[data-scope="task"] .scope-panel-badge { background:#e5eef7; color:#416482; }
.scope-panel-actions { flex-shrink:0; }
.scope-panel-actions :deep(label) { margin:0; }
.scope-panel-actions :deep(select) { max-width:280px; min-width:170px; }
@media(max-width:720px) { .scope-panel { align-items:stretch; flex-direction:column; gap:12px; padding:14px; } .scope-panel-actions :deep(select) { max-width:none; width:100%; } .scope-panel-copy { gap:10px; } }
</style>
