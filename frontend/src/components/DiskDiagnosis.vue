<script setup lang="ts">
import { computed } from 'vue';
import { diskBytes, type DiskDiagnostic } from '../features/disk/model';
const props = defineProps<{
  diagnostic: DiskDiagnostic;
  expanded?: boolean;
  historical?: boolean;
}>();
const decision = computed(() => props.diagnostic.download);
const stale = computed(() => Date.now() / 1000 - props.diagnostic.sampled_at > 90);
function time(value: number) {
  return new Date(value * 1000).toLocaleString('zh-CN', { hour12: false });
}
</script>
<template>
  <section
    class="disk-diagnosis"
    :class="[diagnostic.severity, decision.impact]"
    aria-label="磁盘空间诊断"
  >
    <p v-if="historical" class="disk-note">
      告警记录：以下是当时的空间快照，当前状态请查看“空间与日志”。
    </p>
    <p v-else-if="stale" class="disk-stale" role="status">
      空间采样已过期，暂不能确认是否影响下载。请刷新或确认实例在线。
    </p>
    <div class="disk-heading">
      <span class="impact-label">{{ historical ? '当时对下载的影响' : '是否影响下载' }}</span>
      <strong>{{ stale && !historical ? '当前状态待更新' : decision.impact_text }}</strong>
      <small
        v-if="
          !historical &&
          !stale &&
          diagnostic.waiting_tasks &&
          decision.impact === 'new_downloads_paused'
        "
        >已有 {{ diagnostic.waiting_tasks }} 个任务等待空间；已在执行的任务继续接受空间检查。</small
      >
    </div>
    <dl class="disk-answer">
      <div>
        <dt>具体原因</dt>
        <dd>{{ decision.reason }}</dd>
      </div>
      <div>
        <dt>还差多少</dt>
        <dd>
          <strong>{{ decision.shortfall_text }}</strong>
          <small v-if="decision.request_bytes != null && decision.impact !== 'unaffected'"
            >最近等待任务预计还需 {{ diskBytes(decision.request_bytes) }}。{{
              decision.request_note
            }}</small
          >
        </dd>
      </div>
      <div>
        <dt>怎么处理</dt>
        <dd>
          <p v-for="action in decision.actions" :key="action">{{ action }}</p>
        </dd>
      </div>
      <div>
        <dt>何时继续</dt>
        <dd>{{ decision.resume_text }}</dd>
      </div>
    </dl>
    <div class="disk-readings">
      <span
        >磁盘实际可用<strong>{{ diskBytes(diagnostic.free_bytes) }}</strong
        ><small>全盘已用 {{ diagnostic.used_percent }}%</small></span
      >
      <span
        >临时文件额度已分配<strong
          >{{ diskBytes(diagnostic.budget.committed_bytes) }} /
          {{ diskBytes(diagnostic.budget.limit_bytes) }}</strong
        ><small>含已写入文件和预留；不是全盘使用率</small></span
      >
    </div>
    <details class="disk-details" :open="expanded || undefined">
      <summary>查看额度明细与预留规则</summary>
      <p class="disk-note">
        实际文件占用磁盘；预留只是为正在执行的任务记账，不会提前生成大文件。以下各分类包含在总额度中，不能再与总计相加。
      </p>
      <div class="disk-quotas">
        <article
          v-for="row in decision.constraints"
          :key="row.key"
          :class="{ limiting: row.limiting, total: row.key === 'total' }"
        >
          <div class="quota-heading">
            <strong>{{ row.label }}</strong
            ><b>{{
              row.limiting ? '当前受限' : row.remaining_bytes ? '额度充足' : '额度已分配完'
            }}</b>
          </div>
          <div class="quota-values">
            <span
              >实际文件 <b>{{ diskBytes(row.used_bytes) }}</b></span
            ><span
              >另行预留 <b>{{ diskBytes(row.reserved_bytes) }}</b></span
            ><span
              >设置上限 <b>{{ diskBytes(row.limit_bytes) }}</b></span
            ><span
              >还能分配 <b>{{ diskBytes(row.remaining_bytes) }}</b></span
            >
          </div>
          <p v-if="row.shortfall_bytes" class="quota-gap">
            此项需释放或增加约 {{ diskBytes(row.shortfall_bytes) }} 额度。
          </p>
        </article>
      </div>
      <p v-if="decision.physical_shortfall_bytes" class="quota-gap">
        磁盘实际可用空间还需增加 {{ diskBytes(decision.physical_shortfall_bytes) }}。
      </p>
      <p v-if="decision.reservation_shortfall_bytes" class="quota-gap">
        扣除其他执行的预留和最低保留空间后，本次申请还差
        {{ diskBytes(decision.reservation_shortfall_bytes) }} 可分配空间。
      </p>
      <p class="disk-note">
        测试模式：每个视频的一次执行先共同预留 1
        GiB，视频、音频、字幕、封面等共用；超过后按需增加，合并或转换时另考虑临时峰值。正式保存模式起始预留
        16 MiB。处理完成并登记统计后释放测试临时文件及其额度。
      </p>
      <p class="disk-note">
        磁盘总量 {{ diskBytes(diagnostic.total_bytes) }}；可用低于
        {{ diskBytes(diagnostic.minimum_free_bytes) }} 时保护，恢复到
        {{ diskBytes(diagnostic.resume_free_bytes) }}
        后重新允许启动新下载。提高临时额度不会增加磁盘实际容量。
      </p>
    </details>
    <details class="disk-details">
      <summary>查看磁盘文件来源与清理情况</summary>
      <div class="disk-sources">
        <div v-for="source in diagnostic.sources" :key="source.key" :title="source.note">
          <span
            >{{ source.label
            }}<small v-if="source.same_filesystem === false"
              >位于其他磁盘，不计入本盘合计</small
            ></span
          ><strong>{{ diskBytes(source.used_bytes) }}</strong
          ><small v-if="source.retention_days != null">保留 {{ source.retention_days }} 天</small
          ><small v-else-if="source.reclaimable_bytes != null"
            >可回收约 {{ diskBytes(source.reclaimable_bytes) }}</small
          >
        </div>
      </div>
      <p class="disk-note">
        “清理可回收文件”处理本实例可安全删除的临时文件及过期日志；主机上的数据库、部署文件和 Docker
        镜像需另行处理。
      </p>
      <p v-if="diagnostic.host_status !== 'ok'" class="disk-note">
        主机分类{{
          diagnostic.host_status === 'stale' ? '采集已过期' : '尚未采集'
        }}，未知项目显示“待检测”。
      </p>
      <p v-else class="disk-note">
        主机分类采集于 {{ time(diagnostic.host_sampled_at!) }}；不同分类采样时间不同，可能存在差值。
      </p>
      <p v-if="diagnostic.retention.status === 'ok'" class="disk-note">
        测试临时文件：已结束超过 10 分钟仍残留
        {{ diagnostic.retention.completed_count || 0 }} 项（{{
          diskBytes(diagnostic.retention.completed_bytes || 0)
        }}）；执行或待核对 {{ diagnostic.retention.pending_count || 0 }} 项；最近清理失败
        {{ diagnostic.retention.failed_count || 0 }} 项。
      </p>
      <p v-else class="disk-note">测试文件清理状态待更新。</p>
      <p v-if="diagnostic.retention.pending_processing_count" class="disk-note">
        其中 {{ diagnostic.retention.pending_processing_count }} 项（{{
          diskBytes(diagnostic.retention.pending_processing_bytes || 0)
        }}）仍保留本地处理计划；失败测试请在“失败测试处理”中选择恢复或放弃并清理。
      </p>
      <p v-if="diagnostic.retention.untracked_overdue" class="disk-note">
        有
        {{ diskBytes(diagnostic.retention.untracked_bytes) }}
        未归属文件，需人工核对，安全清理不会直接删除。
      </p>
      <p v-if="diagnostic.retention.failed_count" class="quota-gap">
        最近清理失败：{{
          diagnostic.retention.failure_reasons?.join('；') || '请查看近期日志'
        }}；已连续 {{ diagnostic.retention.failure_checks }} 轮，后台约每 30 秒重试检查。
      </p>
    </details>
    <small class="disk-note"
      >{{ historical ? '记录采样' : '空间采样' }} {{ time(diagnostic.sampled_at) }}</small
    >
  </section>
</template>
<style scoped>
.disk-diagnosis {
  background: #f8fafc;
  border: 1px solid #dfe7ee;
  border-radius: 10px;
  padding: 18px;
  margin: 12px 0;
  color: #45576c;
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.disk-heading {
  display: grid;
  gap: 4px;
  padding-bottom: 14px;
  border-bottom: 1px solid #e2e9ef;
}
.impact-label {
  font-size: 11px;
  color: #6d7e8f;
}
.disk-heading strong {
  font-size: 18px;
  color: #187767;
  line-height: 1.5;
}
.new_downloads_paused .disk-heading strong,
.some_tasks_waiting .disk-heading strong {
  color: #a65315;
}
.disk-heading small,
.disk-note {
  color: #6d7e8f;
  font-size: 11px;
}
.disk-answer {
  margin: 14px 0;
  display: grid;
  gap: 12px;
}
.disk-answer > div {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr);
  gap: 12px;
}
dt {
  font-weight: 600;
  color: #52657b;
}
dd {
  margin: 0;
}
dd small {
  display: block;
  margin-top: 4px;
  color: #6d7e8f;
}
dd p {
  margin: 0 0 4px;
}
.disk-readings {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  padding: 12px;
  background: #fff;
  border-radius: 7px;
}
.disk-readings span {
  display: grid;
  align-content: start;
  gap: 4px;
}
.disk-readings strong {
  font-size: 14px;
  color: #33485f;
}
.disk-readings small {
  font-size: 10px;
  color: #6d7e8f;
}
.disk-details {
  margin-top: 14px;
  border-top: 1px solid #e2e9ef;
  padding-top: 12px;
}
summary {
  cursor: pointer;
  color: #306590;
  font-weight: 600;
}
.disk-quotas {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}
.disk-quotas article {
  padding: 10px;
  background: #fff;
  border: 1px solid #e4eaf0;
  border-radius: 6px;
}
.disk-quotas article.limiting {
  border-color: #e5b579;
  background: #fff9ef;
}
.quota-heading {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}
.quota-heading > b {
  font-weight: 400;
  color: #658078;
}
.limiting .quota-heading > b,
.quota-gap {
  color: #a65315;
}
.quota-values {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 12px;
  margin-top: 8px;
  color: #6d7e8f;
  font-size: 11px;
}
.quota-values b {
  color: #45576c;
  font-weight: 500;
}
.quota-gap {
  margin: 6px 0;
  font-size: 11px;
}
.disk-note {
  margin: 10px 0 0;
  display: block;
}
.disk-sources > div {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 4px 10px;
  padding: 8px 0;
  border-bottom: 1px solid #e5ebf0;
}
.disk-sources small {
  font-size: 10px;
  color: #6d7e8f;
}
.disk-sources span small {
  display: block;
}
.disk-stale {
  padding: 10px;
  background: #fff1d8;
  color: #89551c;
}
@media (max-width: 600px) {
  .disk-diagnosis {
    padding: 12px;
  }
  .disk-heading strong {
    font-size: 16px;
  }
  .disk-answer > div {
    grid-template-columns: 1fr;
    gap: 3px;
  }
  .quota-values {
    grid-template-columns: 1fr;
  }
}
</style>
