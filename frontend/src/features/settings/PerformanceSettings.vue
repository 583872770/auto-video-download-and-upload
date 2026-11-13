<script setup lang="ts">
import { useSettings } from '../../stores/settings';
import { groups } from './fields';
import FieldGroup from './FieldGroup.vue';
import ChannelsEditor from './ChannelsEditor.vue';
const store = useSettings();
</script>
<template>
  <FieldGroup title="运行与并发" :fields="groups.performance" />
  <FieldGroup title="渠道调度" :fields="groups.strategy" />
  <FieldGroup title="账号池容量" :fields="groups.pool" />
  <FieldGroup title="数据库连接预算" :fields="groups.database"
    ><p v-if="store.budget">
      已保存配置核算：连接预算 {{ store.budget.total }}，预计需求
      {{ store.budget.required_total }}，目标进程
      {{ store.budget.target_workers }}。修改后的核算在保存后更新。
    </p></FieldGroup
  >
  <ChannelsEditor />
</template>
