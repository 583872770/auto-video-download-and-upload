<script setup lang="ts">
import { diskBytes, type DiskDiagnostic } from '../features/disk/model';
defineProps<{ diagnostic: DiskDiagnostic; expanded?: boolean }>();
function time(value: number) {
  return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });
}
</script>
<template>
  <section class="disk-diagnosis" :class="diagnostic.severity" aria-label="磁盘空间诊断">
    <p v-if="Date.now() / 1000 - diagnostic.sampled_at > 90" class="disk-pressure">
      此为上次采样记录，当前空间状态待更新；请确认实例在线。
    </p>
    <div class="disk-heading">
      <strong>{{ diagnostic.headline }}</strong
      ><b>{{ diagnostic.used_percent }}%</b>
    </div>
    <div
      class="disk-meter"
      role="meter"
      aria-label="实际磁盘使用率"
      :aria-valuenow="diagnostic.used_percent"
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <i :style="{ width: Math.min(100, diagnostic.used_percent) + '%' }"></i>
    </div>
    <div class="disk-readings">
      <span
        >实际剩余<strong>{{ diskBytes(diagnostic.free_bytes) }}</strong></span
      >
      <span
        >磁盘总量<strong>{{ diskBytes(diagnostic.total_bytes) }}</strong></span
      >
      <span
        >尚未写入的预留<strong>{{ diskBytes(diagnostic.unwritten_reserved_bytes) }}</strong></span
      >
      <span
        >等待资源任务<strong>{{ diagnostic.waiting_tasks }}</strong></span
      >
    </div>
    <p class="disk-note">
      临时总预算：已占用及预留 {{ diskBytes(diagnostic.budget.committed_bytes) }} /
      {{ diskBytes(diagnostic.budget.limit_bytes) }}。
      <template v-if="diagnostic.budget.category_label"
        >{{ diagnostic.budget.category_label }}分类：{{
          diskBytes(diagnostic.budget.category_committed_bytes)
        }}
        / {{ diskBytes(diagnostic.budget.category_limit_bytes) }}。</template
      >
    </p>
    <p v-if="diagnostic.pressure.requested_bytes != null" class="disk-pressure">
      最近一次申请新增预留 {{ diskBytes(diagnostic.pressure.requested_bytes) }}，当时可分配
      {{ diskBytes(diagnostic.pressure.allocatable_bytes) }}。
      <span v-if="diagnostic.pressure.kind === 'budget'"
        >受临时文件预算限制，请检查分类预算与总预算。</span
      >
    </p>
    <details :open="expanded || undefined">
      <summary>占用来源与恢复条件</summary>
      <div class="disk-sources">
        <div v-for="source in diagnostic.sources" :key="source.key" :title="source.note">
          <span
            >{{ source.label
            }}<small v-if="source.same_filesystem === false">其他磁盘，不计入本盘合计</small></span
          >
          <strong>{{ diskBytes(source.used_bytes) }}</strong>
          <small v-if="source.retention_days != null">保留 {{ source.retention_days }} 天</small>
          <small v-else-if="source.reclaimable_bytes != null"
            >可回收约 {{ diskBytes(source.reclaimable_bytes) }}</small
          >
        </div>
      </div>
      <p v-if="diagnostic.host_status !== 'ok'" class="disk-note">
        主机分类{{
          diagnostic.host_status === 'stale' ? '采集已过期' : '尚未采集'
        }}，未知占用显示“待检测”，包含在未分类占用中。
      </p>
      <p v-else class="disk-note">
        主机分类采集于
        {{ time(diagnostic.host_sampled_at!) }}；分类与磁盘用量采样时间不同，可能存在差值。
      </p>
      <p class="disk-note">
        低于 {{ diskBytes(diagnostic.minimum_free_bytes) }} 触发保护，恢复水位
        {{
          diskBytes(diagnostic.resume_free_bytes)
        }}。预留未写入磁盘，不重复计入使用率；任务还需满足临时预算与其他执行条件。
      </p>
      <p v-if="diagnostic.retention.status === 'ok'" class="disk-retention">
        测试临时文件：已结束超过 10 分钟仍残留
        {{ diagnostic.retention.completed_count || 0 }} 项（{{
          diskBytes(diagnostic.retention.completed_bytes || 0)
        }}）， 执行或待核对 {{ diagnostic.retention.pending_count || 0 }} 项；最近清理失败
        {{ diagnostic.retention.failed_count || 0 }} 项。
      </p>
      <p v-else class="disk-note">测试临时文件清理状态待更新。</p>
      <p v-if="diagnostic.retention.untracked_overdue" class="disk-pressure">
        测试目录还有约 {{ diskBytes(diagnostic.retention.untracked_bytes) }} 未归属文件，已持续超过
        10 分钟，需要核对；不会自动删除归属不明的文件。
      </p>
      <p v-if="diagnostic.retention.failed_count" class="disk-pressure">
        清理已连续 {{ diagnostic.retention.failure_checks }} 轮遇到异常（约每 30 秒检查）：{{
          diagnostic.retention.failure_reasons?.join('；') || '请查看近期实例日志'
        }}
      </p>
      <small class="disk-note">空间采样 {{ time(diagnostic.sampled_at) }}</small>
    </details>
  </section>
</template>
<style scoped>
.disk-diagnosis {
  background: #f8fafc;
  border: 1px solid #e3eaf2;
  border-radius: 9px;
  padding: 15px;
  margin: 12px 0;
  color: #475569;
  font-size: 12px;
}
.disk-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  color: #334155;
}
.disk-heading b {
  font-size: 20px;
  font-variant-numeric: tabular-nums;
}
.disk-meter {
  height: 6px;
  background: #e5eaf1;
  border-radius: 6px;
  overflow: hidden;
  margin: 10px 0 16px;
}
.disk-meter i {
  display: block;
  height: 100%;
  background: #299682;
}
.warning .disk-meter i {
  background: #d99a31;
}
.critical .disk-meter i,
.blocked .disk-meter i {
  background: #dc5757;
}
.disk-readings {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 14px;
}
.disk-readings span {
  display: grid;
  gap: 5px;
  color: #718096;
  font-size: 11px;
}
.disk-readings strong {
  color: #334155;
  font-size: 15px;
  font-weight: 600;
}
.disk-pressure {
  background: #fff4e5;
  border-radius: 5px;
  padding: 10px;
  line-height: 1.7;
  color: #996821;
}
summary {
  cursor: pointer;
  color: #3565aa;
  padding: 5px 0;
}
.disk-sources {
  margin: 8px 0;
}
.disk-sources > div {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 4px 12px;
  padding: 9px 0;
  border-bottom: 1px solid #e6ebf2;
}
.disk-sources small {
  color: #8592a5;
  font-size: 10px;
  font-weight: 400;
}
.disk-sources span small {
  display: block;
  margin-top: 3px;
}
.disk-note,
.disk-retention {
  font-size: 11px;
  color: #7b889a;
  line-height: 1.7;
  margin: 10px 0 0;
}
.disk-retention {
  color: #6d7887;
}
</style>
