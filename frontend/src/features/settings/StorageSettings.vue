<script setup lang="ts">
import { useSettings } from '../../stores/settings';
import { groups } from './fields';
import SettingField from './SettingField.vue';
import FieldGroup from './FieldGroup.vue';
const store = useSettings();
</script>
<template>
  <section class="card">
    <h2>文件保存方式</h2>
    <p>选择完成后的文件保存位置，配置作用于当前实例。</p>
    <div class="choices">
      <label
        v-for="[key, title, help] in [
          ['local', '本地保存', '文件保存在实例磁盘'],
          ['oss', '阿里云 OSS', '上传到 Bucket，同时保留本地副本'],
          ['test', '测试处理', '用于验证流程，不形成正式交付'],
        ]"
        :key="key"
        class="choice"
        :class="{ selected: store.draft.storage_mode === key }"
      >
        <input
          type="radio"
          name="storage_mode"
          :value="key"
          :checked="store.draft.storage_mode === key"
          @change="store.update('storage_mode', key!)"
        /><span
          ><strong>{{ title }}</strong
          ><small>{{ help }}</small></span
        >
      </label>
    </div>
  </section>
  <div class="storage-groups" :class="{ 'oss-first': store.draft.storage_mode === 'oss' }">
    <section class="card local-settings">
      <h2>本地目录</h2>
      <p>
        {{
          store.draft.storage_mode === 'test'
            ? '测试文件由系统管理，目录配置保留供其他保存方式使用。'
            : 'OSS 上传完成后也会保留本地副本，请同时设置空间预算。'
        }}
      </p>
      <div class="fields">
        <SettingField
          v-for="field in groups.local"
          :key="field.key"
          :field="field"
          :disabled="store.draft.storage_mode === 'test'"
        />
      </div>
    </section>
    <section class="card oss-settings">
      <h2>阿里云 OSS</h2>
      <p>可提前配置。选择 OSS 后启用上传；密钥保存后不回显，留空保留原值。</p>
      <div class="fields">
        <SettingField v-for="field in groups.oss" :key="field.key" :field="field" />
      </div>
      <details>
        <summary>临时凭据与上传性能</summary>
        <div class="fields">
          <SettingField v-for="field in groups.ossAdvanced" :key="field.key" :field="field" />
        </div>
      </details>
    </section>
  </div>
</template>
