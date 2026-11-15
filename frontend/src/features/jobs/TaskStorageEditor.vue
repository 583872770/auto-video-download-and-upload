<script setup lang="ts">
import { computed } from 'vue';
import type { DeliverySpec } from '../delivery/model';
const storage = defineModel<DeliverySpec['storage']>({ required: true });
const modes = [
  ['local', '本地保存', '保存到执行实例的磁盘'],
  ['oss', 'OSS 上传', '上传云端，同时保留本地副本'],
  ['test', '测试处理', '使用测试临时空间，不形成正式交付'],
];
const directory = computed(() =>
  [
    storage.value.path.replace(/[\\/]+$/, ''),
    storage.value.dir.replace(/^[\\/]+|[\\/]+$/g, ''),
    storage.value.shard_downloads ? '{视频ID}' : '',
  ]
    .filter(Boolean)
    .join('/'),
);
const cloudFields = [
  ['oss_endpoint', 'Endpoint'],
  ['oss_bucket', 'Bucket 名称'],
  ['oss_prefix', '对象前缀'],
  ['oss_access_key_id', 'AccessKey ID'],
] as const;
</script>
<template>
  <section class="task-storage-editor">
    <div class="section-heading">
      <div>
        <h3>本任务文件保存</h3>
        <p class="muted">本任务独立配置；执行实例提供磁盘和网络，保存规则随任务执行。</p>
      </div>
      <span class="badge">任务配置</span>
    </div>
    <div class="storage-mode-options">
      <label
        v-for="[value, title, help] in modes"
        :key="value"
        class="choice"
        :class="{ selected: storage.storage_mode === value }"
        ><input
          v-model="storage.storage_mode"
          type="radio"
          name="task-storage-mode"
          :value="value"
        /><span
          ><strong>{{ title }}</strong
          ><small>{{ help }}</small></span
        ></label
      >
    </div>
    <div v-if="storage.storage_mode !== 'test'" class="form-grid">
      <label
        >素材根目录<input
          v-model="storage.path"
          name="task-storage-path"
          required
          maxlength="4096"
          placeholder="例如 /data/videos"
        /><small>此路径位于实际执行实例；相对路径以该实例工作目录为基准。</small></label
      >
      <label
        >任务子目录<input
          v-model="storage.dir"
          name="task-storage-dir"
          maxlength="4096"
          placeholder="例如 客户A/素材采集"
        /><small>填写根目录下的相对路径，不包含 ..。</small></label
      >
      <label
        >文件组织方式<select v-model="storage.shard_downloads" name="task-storage-shard">
          <option :value="true">按视频 ID 分目录</option>
          <option :value="false">直接保存到任务目录</option>
        </select></label
      >
    </div>
    <div v-if="storage.storage_mode === 'oss'" class="task-oss-settings">
      <h4>本任务 OSS 目标</h4>
      <div class="form-grid">
        <label v-for="[key, label] in cloudFields" :key="key"
          >{{ label
          }}<input
            v-model="storage[key]"
            :name="'task-' + key"
            :required="key !== 'oss_prefix'"
            maxlength="4096"
        /></label>
        <label
          >AccessKey Secret<input
            v-model="storage.oss_access_key_secret"
            name="task-oss-secret"
            type="password"
            autocomplete="new-password"
            :required="!storage.oss_access_key_secret_configured"
            :placeholder="
              storage.oss_access_key_secret_configured ? '已配置，留空保留' : '填写本任务的 Secret'
            "
        /></label>
        <label
          >STS Token（可选）<input
            v-model="storage.oss_security_token"
            name="task-oss-token"
            type="password"
            autocomplete="new-password"
            :placeholder="
              storage.oss_security_token_configured ? '已配置，留空保留' : '使用临时凭据时填写'
            "
        /></label>
        <label
          >同时上传文件数<input
            v-model.number="storage.oss_upload_limit"
            name="task-oss-limit"
            type="number"
            min="1"
            max="8"
            required
          /><small>本任务在同一实例上的所有视频共用此额度。</small></label
        >
        <label
          >每文件上传线程<input
            v-model.number="storage.oss_upload_threads"
            name="task-oss-threads"
            type="number"
            min="1"
            max="8"
            required
        /></label>
        <label
          >分片大小（MiB）<input
            v-model.number="storage.oss_part_size_mb"
            name="task-oss-part"
            type="number"
            min="4"
            max="64"
            required
        /></label>
      </div>
    </div>
    <div class="storage-preview" aria-label="任务保存位置预览">
      <span>保存位置预览</span
      ><code v-if="storage.storage_mode !== 'test'">{{ directory }}/文件</code
      ><strong v-else>执行实例的测试临时空间 · 完成后自动清理</strong>
      <code v-if="storage.storage_mode === 'oss'"
        >oss://{{ storage.oss_bucket || '{Bucket}' }}/{{
          [storage.oss_prefix, storage.shard_downloads ? '{视频ID}' : ''].filter(Boolean).join('/')
        }}/文件</code
      >
      <small>视频、音频和各语言字幕共用目录规则。已有文件保留原位置，修改规则不搬迁文件。</small>
    </div>
  </section>
</template>
<style scoped>
.task-storage-editor {
  margin-top: 26px;
  padding-top: 22px;
  border-top: 1px solid #e5ebe9;
}
.storage-mode-options {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 16px 0;
}
.storage-mode-options .choice {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 14px;
}
.storage-mode-options strong,
.storage-mode-options small {
  display: block;
}
.storage-mode-options small {
  color: #73807e;
  font-size: 12px;
  margin-top: 5px;
}
.task-oss-settings {
  padding-top: 12px;
}
.storage-preview {
  display: grid;
  gap: 8px;
  background: #f5f8fa;
  border: 1px solid #e2e9ee;
  border-radius: 10px;
  padding: 15px;
  margin-top: 18px;
  font-size: 13px;
}
.storage-preview code {
  overflow-wrap: anywhere;
  white-space: normal;
}
.storage-preview small {
  color: #6a7b78;
  line-height: 1.6;
}
@media (max-width: 720px) {
  .storage-mode-options {
    grid-template-columns: 1fr;
  }
}
</style>
