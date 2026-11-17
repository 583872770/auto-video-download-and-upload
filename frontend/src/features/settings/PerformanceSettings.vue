<script setup lang="ts">
import { useSettings } from '../../stores/settings';
import { groups } from './fields';
import FieldGroup from './FieldGroup.vue';
const store = useSettings();
</script>
<template>
  <FieldGroup title="下载与本地处理" :fields="groups.performance" />
  <section class="card">
    <h2>自动资源保护</h2>
    <p>
      账号池提供任务容量，服务器持续检查 CPU、内存和 I/O
      压力；压力升高时减少新任务，恢复后逐步增加。
    </p>
    <p v-if="store.budget">
      数据库连接自动核算：当前预算 {{ store.budget.total }} 个连接， 目标下载任务
      {{ store.budget.target_workers }} 个，本地处理预留 {{ store.budget.processing }} 个连接。
      部署环境的数据库连接上限仍生效。
    </p>
  </section>
</template>
