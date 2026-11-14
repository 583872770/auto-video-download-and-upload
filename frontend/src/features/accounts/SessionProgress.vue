<script setup lang="ts">
import type { RuntimeSlot } from '../workbench/contracts';
import { at, downloadLabels } from './contracts';
defineProps<{ session?: RuntimeSlot; online?: boolean }>();
</script>
<template>
  <div v-if="session && online !== false" class="notice">
    <p>
      {{ downloadLabels[session.state] || session.state }} · 容量 {{ session.active }} /
      {{ session.limit }}（配置 {{ session.configured_limit }}） · 启动间隔至少
      {{ session.interval }} 秒
    </p>
    <p v-if="session.cooldown_until">
      冷却截止 {{ at(session.cooldown_until) }}，到期后重新检查准入。
    </p>
    <template v-if="session.pacing"
      ><p>
        {{
          session.pacing.recovery
            ? '低频恢复观察'
            : session.pacing.target_reached
              ? '已达到设置目标'
              : '稳定观察'
        }}
        · 当前实际间隔至少 {{ session.pacing.interval }} 秒，配置下限
        {{ session.pacing.configured_minimum }} 秒
      </p>
      <p>
        有效成功 {{ session.pacing.success_count }} / 至少
        {{ session.pacing.success_target }} 次；观察
        {{ Math.floor(session.pacing.observed_seconds) }} /
        {{ session.pacing.observation_seconds }} 秒
      </p>
      <progress
        :value="Math.min(session.pacing.success_count, session.pacing.success_target)"
        :max="session.pacing.success_target || 1"
        aria-label="有效成功进度"
      /><progress
        :value="Math.min(session.pacing.observed_seconds, session.pacing.observation_seconds)"
        :max="session.pacing.observation_seconds || 1"
        aria-label="观察时间进度"
      />
      <p>
        两项门槛满足后的有效成功才检查下一档
        {{ session.pacing.next_interval }} 秒。时间到不会独立触发调整，保存配置不清除账号保护。
      </p></template
    >
  </div>
  <p v-else class="muted">
    {{ online === false ? '所属实例离线，运行状态待确认。' : '暂无新鲜调度状态。' }}
  </p>
</template>
