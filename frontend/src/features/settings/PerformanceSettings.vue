<script setup lang="ts">
import { useSettings } from '../../stores/settings';
import { groups } from './fields';
import FieldGroup from './FieldGroup.vue';
const store = useSettings();
</script>
<template>
  <FieldGroup title="下载与本地处理" :fields="groups.performance" />
  <section class="card">
    <h2>当前资源策略</h2>
    <p v-if="store.draft.download_auto_reduce">
      自动降低上限已开启：CPU、可用内存或 I/O 压力升高时减少进程名额，恢复后逐步增加。
    </p>
    <p v-else>自动降低上限已关闭：按常规上限运行，资源压力升高时不自动缩容。</p>
    <p>账号容量、内存容量预估、数据库连接容量与磁盘空间保护始终生效。以上修改保存后生效。</p>
    <p v-if="store.budget">
      数据库连接自动核算：当前预算 {{ store.budget.total }} 个连接， 目标下载任务
      {{ store.budget.target_workers }} 个，本地处理预留 {{ store.budget.processing }} 个连接。
      部署环境的数据库连接上限仍生效。
    </p>
  </section>
</template>
